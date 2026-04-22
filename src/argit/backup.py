"""argit backup — sanitize, snapshot, optionally commit/push.

Phases (per tech-spec-01-mvp.md §Task 11.1):
1. Pre-flight (full)
2. Unspecified-files walk
3. Sanitize
4. Whole-file secrets
5. Data copy
6. SQLite snapshots (.backup)
7. Blob sync
8. --commit (stage + commit)
9. --push (push)
10. First-backup DR-readiness hint
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import click

from .errors import ArgitError
from .manifest import Item, Manifest, SanitizeFile, expand_items_for_backup, expand_globbed_item, load_manifest
from .passwrap import PassWrap
from .sanitize import sanitize as run_sanitize
from .shared import (
    EXIT_OK,
    acquire_lock,
    check_no_partial_state,
    in_progress_marker,
    matches_exclude,
    run_preflight,
)

VERSION_CHECK_TIMEOUT_SEC = 5


# ---------- helpers ----------

def _emit(dry: bool, msg: str) -> None:
    click.echo(("would: " if dry else "✓ ") + msg)


def _warn(msg: str) -> None:
    click.echo("! " + msg, err=True)


def _walk_relative(root: Path) -> Iterable[Path]:
    """Yield every file and symlink under `root`, plus any EMPTY directory
    (one containing no files anywhere in its subtree). Empty dirs are
    reported so a freshly-created `future-plugin/` fires the
    unspecified-files warning even before it has content."""
    if not root.is_dir():
        return
    files_by_dir: dict[Path, int] = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() or p.is_symlink():
            yield rel
            # Mark every parent as "has content"
            for parent in rel.parents:
                files_by_dir[parent] = files_by_dir.get(parent, 0) + 1
        elif p.is_dir():
            files_by_dir.setdefault(rel, 0)
    for dir_path, count in files_by_dir.items():
        if count == 0 and str(dir_path) != ".":
            yield dir_path


def _is_under(rel: Path, prefix: str) -> bool:
    """Path covers a path or directory prefix (`source` ending with `/`)."""
    if prefix.endswith("/"):
        prefix_clean = prefix.rstrip("/")
        return str(rel).startswith(prefix_clean + "/") or str(rel) == prefix_clean
    return str(rel) == prefix


def _glob_pattern_matches(rel_str: str, pattern: str) -> bool:
    """Component-wise match: `*` is a single-component wildcard (regex [^/]+).

    Whole-source `*` patterns must NOT cross `/`. Pattern components must
    equal the path components one-for-one (or be `*`). Trailing-slash dir
    patterns match the directory prefix — `agents/*/` covers `agents/main/`
    and everything under it.
    """
    dir_pat = pattern.endswith("/")
    pat_clean = pattern.rstrip("/")
    pat_parts = pat_clean.split("/")
    rel_parts = rel_str.split("/")
    if dir_pat:
        if len(rel_parts) < len(pat_parts):
            return False
        for pp, rp in zip(pat_parts, rel_parts):
            if pp == "*":
                continue
            if pp != rp:
                return False
        return True
    if len(rel_parts) != len(pat_parts):
        return False
    for pp, rp in zip(pat_parts, rel_parts):
        if pp == "*":
            continue
        if pp != rp:
            return False
    return True


def _covered_by_items(rel: Path, items: list[Item]) -> bool:
    rel_str = str(rel)
    for it in items:
        if it.is_globbed:
            if _glob_pattern_matches(rel_str, it.source):
                return True
        else:
            if _is_under(rel, it.source):
                return True
    return False


def _covered_by_sanitize(rel: Path, sf: list[SanitizeFile]) -> bool:
    s = str(rel)
    for f in sf:
        if s == f.file:
            return True
    return False


def _check_chmod(path: Path, mode: str) -> None:
    path.chmod(int(mode, 8))


def _version_check(manifest: Manifest) -> None:
    """Probe `openclaw --version`, warn on mismatch (never fail)."""
    if shutil.which("openclaw") is None:
        return
    try:
        cp = subprocess.run(
            ["openclaw", "--version"],
            capture_output=True, text=True, timeout=VERSION_CHECK_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired:
        _warn(f"openclaw --version timed out after {VERSION_CHECK_TIMEOUT_SEC}s; skipping version check")
        return
    if cp.returncode != 0:
        _warn(f"Could not probe OpenClaw version (`openclaw --version` exit {cp.returncode}). Skipping version-mismatch check.")
        return
    raw = (cp.stdout + cp.stderr).strip().split()
    if not raw:
        _warn("openclaw --version produced no output; skipping comparison")
        return
    token = raw[0].lstrip("v")
    if not _version_parseable(token):
        _warn(f"openclaw --version returned '{token}' (unparseable); skipping comparison")
        return
    cmp = _version_cmp(token, manifest.agent_version)
    if cmp > 0:
        _warn(f"OpenClaw {token} newer than manifest {manifest.agent_version}. New fields may not be backed up. Check for an updated argit release.")
    elif cmp < 0:
        _warn(f"OpenClaw {token} older than manifest {manifest.agent_version}. Fields defined in the manifest but missing in this install will be skipped silently.")


_VER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:-[0-9A-Za-z.]+)?$")


def _version_parseable(s: str) -> bool:
    return bool(_VER_RE.match(s))


def _version_cmp(a: str, b: str) -> int:
    """Component-wise compare of dotted-numeric versions (ignore -<build> suffix)."""
    def parts(v: str) -> list[int]:
        head = v.split("-", 1)[0]
        return [int(x) for x in head.split(".") if x.isdigit()]
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# ---------- main entry ----------

def run_backup(repo_root: Path, *, commit: bool, push: bool, strict: bool, dry_run: bool) -> int:
    if push:
        commit = True

    check_no_partial_state(repo_root, "backup")
    pre = run_preflight(repo_root, require_manifest=True, require_gpg_id=True)
    manifest = load_manifest(repo_root)
    source_root = manifest.expanded_source_root()

    _version_check(manifest)

    secrets_dir = repo_root / "secrets"
    pass_wrap = PassWrap(secrets_dir)

    secrets_before = set(pass_wrap.ls())

    with acquire_lock(repo_root):
        # 2. Unspecified-files walk — READ ONLY; failures here leave no
        # partial state, so we stay outside the in-progress marker.
        unspecified: list[str] = []
        for rel in _walk_relative(source_root):
            if matches_exclude(rel, manifest.exclude):
                continue
            if _covered_by_items(rel, manifest.items):
                continue
            if _covered_by_sanitize(rel, manifest.sanitize):
                continue
            unspecified.append(str(rel))
        if unspecified:
            if strict:
                raise ArgitError(
                    "unspecified files in source_root (--strict):\n  " + "\n  ".join(unspecified),
                    "install a plugin manifest or extend this one",
                )
            for p in unspecified:
                _warn(f"not backed up: {p} — not in manifest")

        # Track B: expand globbed items once at the boundary so phases 4-7
        # iterate concrete items only. Zero-match globs are warned per-item
        # and dropped (AC-B4). Runtime duplicate detection across the
        # expanded set fires inside expand_items_for_backup (AC-INT5).
        concrete_items: list[Item] = []
        for it in manifest.items:
            expanded = expand_globbed_item(
                it, source_root, manifest.agent_type, manifest.exclude,
            )
            if it.is_globbed and len(expanded) == 0:
                _warn(f"globbed item '{it.source}' matched nothing — skipping")
            concrete_items.extend(expanded)
        # Second pass: duplicate detection on the flattened list.
        seen_sources: dict[tuple[str, str], Item] = {}
        for exp in concrete_items:
            key = (exp.source, exp.kind)
            if key in seen_sources:
                prev = seen_sources[key]
                raise ArgitError(
                    f"runtime duplicate: concrete (source='{exp.source}', kind='{exp.kind}') "
                    f"expanded from two items — one ({prev.origin}), one ({exp.origin})",
                    "disambiguate by removing the conflicting overlay or bundled item",
                )
            seen_sources[key] = exp

        # The marker enters HERE — right before the first mutating phase.
        # Preflight, unspecified-files walk, and version-check are all read-
        # only; failing in those should not force the operator to manually
        # delete `.argit/in-progress` before retrying.
        with in_progress_marker(repo_root):
            # 3. Sanitize
            for sf in manifest.sanitize:
                src_file = source_root / sf.file
                if not src_file.is_file():
                    _warn(f"sanitize source missing: {sf.file} (skipping)")
                    continue
                if dry_run:
                    _emit(True, f"sanitize {sf.file} → {sf.target} ({len(sf.rules)} rules)")
                    continue
                try:
                    config = json.loads(src_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ArgitError(
                        f"sanitize source {sf.file} is not valid JSON: {exc.msg} (line {exc.lineno})",
                        "fix the JSON or remove the rule",
                    ) from exc
                sanitized, extracted, skipped = run_sanitize(config, sf.rules)
                target_path = repo_root / sf.target
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(
                    json.dumps(sanitized, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                _check_chmod(target_path, sf.mode)
                for pp, val in extracted.items():
                    pass_wrap.insert(pp, val)
                for sr in skipped:
                    _warn(f"sanitize path '{sr.path}' not present in {sf.file} (skipping rule)")
                _emit(False, f"sanitize: {sf.file} ({len(extracted)}/{len(sf.rules)} rules → pass)")

            # 4. Whole-file secrets
            for it in [i for i in concrete_items if i.kind == "secret"]:
                src = source_root / it.source
                if not src.is_file():
                    _warn(f"secret source missing: {it.source} (skipping)")
                    continue
                if dry_run:
                    _emit(True, f"pass insert {it.pass_path} ← {it.source}")
                    continue
                pass_wrap.insert(it.pass_path, src.read_text(encoding="utf-8"))
                _emit(False, f"secret: {it.source} → pass")

            # 5. Data copy
            for it in [i for i in concrete_items if i.kind == "data"]:
                src = source_root / it.source
                tgt = repo_root / it.target
                if it.is_dir_source:
                    if not src.is_dir():
                        _warn(f"data source missing: {it.source} (skipping)")
                        continue
                    if dry_run:
                        _emit(True, f"copytree {it.source} → {it.target}")
                        continue
                    tgt.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src, tgt, dirs_exist_ok=True, symlinks=True)
                else:
                    if not src.is_file():
                        _warn(f"data source missing: {it.source} (skipping)")
                        continue
                    if dry_run:
                        _emit(True, f"copy {it.source} → {it.target}")
                        continue
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, tgt)
                    _check_chmod(tgt, it.mode)
                _emit(False, f"data: {it.source} → {it.target}")

            # 6. SQLite snapshots (.backup)
            for it in [i for i in concrete_items if i.kind == "sqlite"]:
                src = source_root / it.source
                if not src.is_file():
                    _warn(f"sqlite source missing: {it.source} (skipping)")
                    continue
                tgt = repo_root / it.target
                if dry_run:
                    _emit(True, f"sqlite3 .backup {it.source} → {it.target}")
                    continue
                tgt.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    cp = subprocess.run(
                        ["sqlite3", str(src), f".backup '{tmp_path}'"],
                        capture_output=True, text=True, timeout=30,
                    )
                    if cp.returncode != 0:
                        raise ArgitError(
                            f"sqlite3 .backup failed for {it.source}: {cp.stderr.strip() or cp.stdout.strip()}",
                            "verify the source is a valid SQLite DB; check WAL state",
                        )
                    shutil.copy2(tmp_path, tgt)
                    _check_chmod(tgt, it.mode)
                finally:
                    tmp_path.unlink(missing_ok=True)
                _emit(False, f"sqlite: {it.source} → {it.target}")

            # 7. Blob sync
            for it in [i for i in concrete_items if i.kind == "blob"]:
                src = source_root / it.source
                tgt = repo_root / it.target
                if not src.is_dir():
                    _warn(f"blob source missing: {it.source} (skipping)")
                    continue
                if dry_run:
                    _emit(True, f"copytree {it.source} → {it.target} (LFS)")
                    continue
                tgt.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, tgt, dirs_exist_ok=True, symlinks=True)
                _emit(False, f"blob: {it.source} → {it.target} (LFS)")

            # last-backup metadata (written before --commit so the recorded
            # git sha is the parent of the new commit; cross-instance warning
            # cares about hostname/manifest, sha is informational).
            last_backup = repo_root / ".argit" / "last-backup.json"
            iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            git_sha = _current_git_sha(repo_root)
            if not dry_run:
                last_backup.parent.mkdir(parents=True, exist_ok=True)
                last_backup.write_text(
                    json.dumps({
                        "timestamp": iso,
                        "hostname": socket.gethostname(),
                        "manifest": manifest.filename,
                        "git_sha": git_sha,
                    }, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            else:
                _emit(True, f"write {last_backup.relative_to(repo_root)}")

            # 8. --commit
            if commit:
                _git_commit(repo_root, manifest, concrete_items, iso, dry_run)

            # 9. --push
            if push:
                _git_push(repo_root, dry_run)

    secrets_after = set(pass_wrap.ls()) if not dry_run else secrets_before
    new_secrets = secrets_after - secrets_before

    if commit and push:
        _emit(False, "backup complete; committed and pushed")
    elif commit:
        _emit(False, "backup complete; committed locally")
    else:
        click.echo("backup complete. Run `git add -A && git commit -m 'backup' && git push` to commit.")

    if new_secrets:
        click.echo("Run `argit doctor` to verify DR-readiness (recipient count, key imports, upstream config).")

    return EXIT_OK


def _current_git_sha(repo_root: Path) -> str | None:
    cp = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=10, check=False,
    )
    if cp.returncode != 0:
        # Empty repo (no commits yet) — first backup case.
        return None
    return cp.stdout.strip()


def _git_commit(
    repo_root: Path, manifest: Manifest, concrete_items: list[Item], iso: str, dry: bool,
) -> None:
    """Stage manifest's managed paths + secrets/ + .argit/last-backup.json; commit.

    `concrete_items` — the post-glob-expansion item list, so `it.target`
    is always a concrete path (never contains `*`).
    """
    paths_to_add: list[str] = []
    for it in concrete_items:
        if it.kind == "secret":
            continue  # secret is in secrets/, added below
        if it.target:
            paths_to_add.append(it.target)
    for sf in manifest.sanitize:
        paths_to_add.append(sf.target)
    paths_to_add.append("secrets/")
    paths_to_add.append(".argit/last-backup.json")

    if dry:
        _emit(True, f"git add: {paths_to_add}")
        _emit(True, f"git commit -m 'argit backup {iso}'")
        return

    cp = subprocess.run(
        ["git", "add", "--", *paths_to_add],
        cwd=str(repo_root), capture_output=True, text=True, timeout=60,
    )
    if cp.returncode != 0:
        raise ArgitError(
            f"git add failed: {cp.stderr.strip() or cp.stdout.strip()}",
            "inspect with `git status`",
        )

    # Detect "no changes" via diff --cached
    cp_diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if cp_diff.returncode == 0:
        _emit(False, "git: no changes to commit")
        return

    cp = subprocess.run(
        ["git", "commit", "-m", f"argit backup {iso}"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=60,
    )
    if cp.returncode != 0:
        raise ArgitError(
            f"git commit failed: {cp.stderr.strip() or cp.stdout.strip()}",
            "inspect with `git status`",
        )
    _emit(False, "git: committed")


def _git_push(repo_root: Path, dry: bool) -> None:
    if dry:
        _emit(True, "git push")
        return
    cp = subprocess.run(
        ["git", "push"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if cp.returncode != 0:
        stderr = (cp.stderr or "").strip()
        if "no upstream branch" in stderr or "no upstream configured" in stderr.lower():
            cp_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_root), capture_output=True, text=True,
            )
            br = cp_branch.stdout.strip() or "main"
            raise ArgitError(
                f"git push: no upstream configured for branch {br}",
                f"git push -u origin {br}",
            )
        raise ArgitError(
            f"git push failed: {stderr}",
            "verify remote auth — see README §Troubleshooting",
        )
    _emit(False, "git: pushed")

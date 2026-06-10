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
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
import click

from .errors import ArgitError
from .manifest import Item, Manifest, SanitizeFile, expand_items_for_backup, load_manifest
from .passwrap import PassWrap
from .sanitize import sanitize as run_sanitize
from .shared import (
    EXIT_OK,
    VERSION_CHECK_TIMEOUT_SEC,
    acquire_lock,
    check_no_partial_state,
    covered_by_items,
    covered_by_sanitize,
    in_progress_marker,
    matches_exclude,
    run_preflight,
    version_cmp,
    version_parseable,
    walk_relative,
)

GIT_REMOTE_INFO_FILENAME = ".argit-git-remote-info.md"


# ---------- helpers ----------

def _emit(dry: bool, msg: str) -> None:
    click.echo(("would: " if dry else "✓ ") + msg)


def _warn(msg: str) -> None:
    click.echo("! " + msg, err=True)


def _check_chmod(path: Path, mode: str) -> None:
    path.chmod(int(mode, 8))


def _redact_remote_url(url: str) -> str:
    """Remove credential-bearing URL userinfo before writing remote hints.

    `https://token@github.com/org/repo.git` and
    `https://user:token@github.com/org/repo.git` both become
    `https://<redacted>@github.com/org/repo.git`. SCP-like SSH remotes such
    as `git@github.com:org/repo.git` are left untouched.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.netloc or "@" not in parts.netloc:
        return url
    host = parts.hostname or parts.netloc.rsplit("@", 1)[-1]
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"<redacted>@{host}", parts.path, parts.query, parts.fragment))


def _run_git_metadata(repo: Path, args: list[str]) -> str | None:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


def _nested_git_repos(src: Path) -> list[Path]:
    repos: list[Path] = []
    for git_path in sorted(src.rglob(".git")):
        if git_path.is_dir() or git_path.is_file():
            repos.append(git_path.parent)
    return repos


def _nested_git_remote_info(src: Path, repo: Path) -> str:
    rel = repo.relative_to(src).as_posix() or "."
    remotes_raw = _run_git_metadata(repo, ["remote", "-v"])
    branch = _run_git_metadata(repo, ["branch", "--show-current"])
    head = _run_git_metadata(repo, ["rev-parse", "HEAD"])
    status = _run_git_metadata(repo, ["status", "--short"])

    remote_lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    if remotes_raw:
        for line in remotes_raw.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name, url = parts[0], _redact_remote_url(parts[1])
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            remote_lines.append(f"- `{name}`: `{url}`")
    if not remote_lines:
        remote_lines.append("- No remotes found or Git metadata could not be read.")

    status_lines = [f"- `{line}`" for line in status.splitlines()] if status else ["- Clean or unavailable."]
    branch_text = branch or "(detached or unavailable)"
    head_text = head or "(unavailable)"
    return "\n".join([
        "# Nested Git Remote Info",
        "",
        "Argit does not back up nested `.git/` directories inside blob items.",
        "This file records enough information to recreate the repository metadata manually.",
        "",
        f"- Nested path: `{rel}`",
        f"- Branch: `{branch_text}`",
        f"- HEAD: `{head_text}`",
        "",
        "## Remotes",
        "",
        *remote_lines,
        "",
        "## Status At Backup",
        "",
        *status_lines,
        "",
        "## Manual Rehydrate Sketch",
        "",
        "Review the restored working tree before running commands that may overwrite files.",
        "",
        "```sh",
        "git init",
        "git remote add <name> <url>",
        "git fetch <name>",
        "# git checkout <branch-or-sha>",
        "```",
        "",
    ])


def _ignore_git_dirs(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if name == ".git"}


def _chmod_and_retry(func, path: str, _exc_info) -> None:
    Path(path).chmod(0o700)
    func(path)


def _remove_stale_nested_git_dirs(tgt: Path) -> None:
    if not tgt.exists():
        return
    for git_path in sorted(tgt.rglob(".git"), key=lambda p: len(p.parts), reverse=True):
        if git_path.is_symlink():
            git_path.unlink()
        elif git_path.is_dir():
            shutil.rmtree(git_path, onerror=_chmod_and_retry)
        else:
            git_path.chmod(0o600)
            git_path.unlink()


def _copy_blob_tree(src: Path, tgt: Path) -> None:
    nested_repos = _nested_git_repos(src)
    _remove_stale_nested_git_dirs(tgt)
    tgt.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, tgt, dirs_exist_ok=True, symlinks=True, ignore=_ignore_git_dirs)
    for repo in nested_repos:
        rel = repo.relative_to(src)
        info_dir = tgt / rel
        info_dir.mkdir(parents=True, exist_ok=True)
        (info_dir / GIT_REMOTE_INFO_FILENAME).write_text(
            _nested_git_remote_info(src, repo),
            encoding="utf-8",
        )


def _warn_on_bundled_drift(repo_root: Path, manifest: Manifest) -> None:
    """Hash-check the bundled manifest at backup time. Warn (don't block) when
    the on-disk content doesn't match any catalog entry — i.e., the manifest
    has been hand-edited or shipped from an unknown source.

    Reuses setup's `_classify_drift` to keep a single source of truth on the
    catalog format + canonical-hash comparison. Best-effort: silent on every
    failure mode that isn't a clean `operator_modified` classification.
    `clean` and `stale_bundle` are silent here — the latter is operator
    not-yet-upgraded, surfaced by `argit setup`'s upgrade flow, not by
    backup.
    """
    if not manifest.filename:
        return
    bundled_path = repo_root / ".argit" / "manifest" / manifest.filename
    if not bundled_path.is_file():
        return
    # Lazy import: setup.py pulls a wider import graph (gpg, click prompts,
    # etc.) that backup doesn't otherwise pay for. Lazy keeps backup's
    # cold-import time tight.
    try:
        from .setup import _classify_drift, _load_hash_catalog
    except ImportError:
        return
    # Explicit empty-catalog short-circuit. `_classify_drift` returns
    # `operator_modified` when the catalog is empty/missing (pre-Track-A
    # install, packaging glitch, missing wheel asset). Without this guard
    # every backup would emit a noisy false-positive in those cases —
    # exactly the "trains operators to ignore warnings" failure we want
    # to avoid.
    try:
        catalog = _load_hash_catalog()
    except ArgitError:
        return  # malformed catalog — silent
    if not catalog:
        return  # no catalog shipped — silent
    try:
        kind, _rev = _classify_drift(bundled_path)
    except ArgitError:
        return  # unreadable bundled, invalid JSON — silent
    if kind == "operator_modified":
        # Wording is neutral: `operator_modified` covers hand-edits AND
        # legitimate operator customization AND custom forks. Don't pretend
        # we know it was the agent. Operator/agent reads the warning and
        # decides whether the local edit was intentional.
        _warn(
            f"bundled manifest hash mismatch — {manifest.filename} doesn't "
            f"match any known bundled revision. Run `argit setup` to inspect "
            f"drift; if the local edit was intentional, move it into "
            f"`<basename>.manifest.local.json` overlay."
        )


def _version_check(manifest: Manifest) -> None:
    """Probe `openclaw --version`, warn on mismatch (never fail).

    Keeps per-failure-mode diagnostics (timeout, non-zero exit, empty output,
    no parseable token) so operators see WHY the version comparison was
    skipped — versus a silent miss. Selection-side uses `probe_agent_version`
    in shared.py which collapses all failures to None.
    """
    if manifest.agent_type != "openclaw":
        return
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
    token = next(
        (t for t in (candidate.lstrip("v") for candidate in raw) if version_parseable(t)),
        None,
    )
    if token is None:
        _warn(f"openclaw --version returned '{' '.join(raw)}' (no parseable version token); skipping comparison")
        return
    cmp = version_cmp(token, manifest.agent_version)
    if cmp > 0:
        _warn(f"OpenClaw {token} newer than manifest {manifest.agent_version}. New fields may not be backed up. Check for an updated argit release.")
    elif cmp < 0:
        _warn(f"OpenClaw {token} older than manifest {manifest.agent_version}. Fields defined in the manifest but missing in this install will be skipped silently.")


# ---------- main entry ----------

def run_backup(repo_root: Path, *, commit: bool, push: bool, strict: bool, dry_run: bool) -> int:
    if push:
        commit = True

    check_no_partial_state(repo_root, "backup")
    pre = run_preflight(repo_root, require_manifest=True, require_gpg_id=True)
    manifest = load_manifest(repo_root)
    source_root = manifest.expanded_source_root()

    _version_check(manifest)
    _warn_on_bundled_drift(repo_root, manifest)

    secrets_dir = repo_root / "secrets"
    pass_wrap = PassWrap(secrets_dir)

    secrets_before = set(pass_wrap.ls())

    with acquire_lock(repo_root):
        # 2. Unspecified-files walk — READ ONLY; failures here leave no
        # partial state, so we stay outside the in-progress marker.
        unspecified: list[str] = []
        for rel in walk_relative(source_root):
            if matches_exclude(rel, manifest.exclude):
                continue
            if covered_by_items(rel, manifest.items):
                continue
            if covered_by_sanitize(rel, manifest.sanitize):
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
        # iterate concrete items only. Zero-match globs warn per-item (AC-B4);
        # runtime duplicates across the expanded set raise (AC-INT5).
        concrete_items = expand_items_for_backup(manifest, source_root, warn=_warn)

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
                _copy_blob_tree(src, tgt)
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

            # Auto-emit review report when uncovered files exist. Same `iso`
            # is reused so the report filename, last-backup.json timestamp,
            # and (when --commit) the commit message all share one moment.
            # Failed phases above propagate exceptions and skip this write
            # → no orphan review for a partial backup.
            review_path: Path | None = None
            if unspecified:
                from .review import _detect_overlay_present, generate_review, write_review
                report = generate_review(
                    unspecified, iso, manifest.filename,
                    overlay_present=_detect_overlay_present(repo_root, manifest),
                )
                if report is not None:
                    if dry_run:
                        _emit(True, f"write .argit/reviews/{iso}.md ({len(unspecified)} findings)")
                    else:
                        review_path = write_review(repo_root, report, iso)
                        _emit(False, f"review: .argit/reviews/{iso}.md ({len(unspecified)} findings)")

            # 8. --commit
            if commit:
                _git_commit(repo_root, manifest, concrete_items, iso, dry_run, review_path=review_path)

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
    *, review_path: Path | None = None,
) -> None:
    """Stage manifest's managed paths + secrets/ + .argit/last-backup.json; commit.

    `concrete_items` — the post-glob-expansion item list, so `it.target`
    is always a concrete path (never contains `*`).

    `review_path` — when auto-emit wrote a review report this run, its
    relative path is staged alongside backup state so the audit trail in
    the commit shows what argit flagged at the moment of backup.
    """
    paths_to_add: list[str] = []
    for it in concrete_items:
        if it.kind == "secret":
            continue  # secret is in secrets/, added below
        # Stage only targets that were actually written. An item whose source
        # is absent on this agent is skipped during copy (§4-7), so its target
        # never exists; staging it anyway makes `git add` abort the whole backup
        # with "pathspec '<target>' did not match any files". This happens
        # whenever the live agent lacks a path the bundled manifest declares
        # (e.g. a 2026.5.4 manifest against a 2026.5.7 Hermes build).
        if it.target and (repo_root / it.target).exists():
            paths_to_add.append(it.target)
    for sf in manifest.sanitize:
        paths_to_add.append(sf.target)
    paths_to_add.append("secrets/")
    paths_to_add.append(".argit/last-backup.json")
    if review_path is not None:
        paths_to_add.append(str(review_path.relative_to(repo_root)))

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

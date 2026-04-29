"""argit restore — re-inject secrets, rehydrate, verify.

Phases (per tech-spec-01-mvp.md §Task 12.1):
1. Pre-flight
2. Lifecycle running-check (detect_running / stop)
3. Target resolution + (overwrite|merge|refuse)
4. Sanitized-config restore
5. Whole-file secrets restore
6. Data restore
7. SQLite restore
8. Blob restore + LFS-pointer detection
9. Permissions on source_root
10. Verify (placeholder leak / pass paths / sqlite integrity / mode match)
11. Lifecycle start (skip when --target is foreign)
12. Cross-instance identity warning (BEFORE phase 3)
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import click

from .errors import ArgitError
from .manifest import Item, Lifecycle, Manifest, expand_items_for_restore, load_manifest
from .passwrap import PassWrap
from .sanitize import find_placeholders, reinject
from .shared import (
    EXIT_FIRST_TOUCH,
    EXIT_OK,
    EXIT_VERIFY_FAIL,
    acquire_lock,
    check_no_partial_state,
    in_progress_marker,
    run_preflight,
)

LFS_POINTER_PREFIX = "version https://git-lfs.github.com/"


def _emit(dry: bool, msg: str) -> None:
    click.echo(("would: " if dry else "✓ ") + msg)


def _warn(msg: str) -> None:
    click.echo("! " + msg, err=True)


def _exec_log(argv: list[str]) -> None:
    click.echo(f"→ exec: {argv}", err=True)


def _resolve_paths(manifest_source_root: str, target_override: str | None) -> tuple[Path, Path, bool]:
    """Returns (target, manifest_source_root_resolved, is_scratch_target)."""
    msr = Path(manifest_source_root).expanduser().resolve()
    if target_override:
        tgt = Path(target_override).expanduser().resolve()
    else:
        tgt = msr
    return tgt, msr, (tgt != msr)


def _detect_running(life: Lifecycle, dry: bool) -> bool:
    if life.detect_running is None:
        return False
    cmd = life.detect_running.command
    _exec_log(cmd)
    if dry:
        return False
    cp = subprocess.run(
        cmd, capture_output=True, text=True, timeout=life.detect_running.timeout_sec,
    )
    return cp.returncode == life.detect_running.running_exit_code


def _stop_and_wait(life: Lifecycle, dry: bool) -> None:
    """Run lifecycle.stop, then poll detect_running up to timeout_sec."""
    if life.stop is None:
        return
    _exec_log(life.stop.command)
    if dry:
        return
    cp = subprocess.run(
        life.stop.command, capture_output=True, text=True, timeout=life.stop.timeout_sec,
    )
    if cp.returncode != 0:
        _warn(f"lifecycle.stop exited {cp.returncode}: {cp.stderr.strip()}")
    if life.detect_running is None:
        return
    deadline = time.monotonic() + life.stop.timeout_sec
    interval = life.stop.poll_interval_ms / 1000.0
    while time.monotonic() < deadline:
        cp_p = subprocess.run(
            life.detect_running.command, capture_output=True, text=True,
            timeout=life.detect_running.timeout_sec,
        )
        if cp_p.returncode != life.detect_running.running_exit_code:
            return
        time.sleep(interval)
    raise ArgitError(
        f"agent did not stop within {life.stop.timeout_sec}s after lifecycle.stop",
        "stop it manually, then retry",
    )


def _start_after_restore(life: Lifecycle, dry: bool) -> None:
    if life.start is None:
        return
    _exec_log(life.start.command)
    if dry:
        return
    cp = subprocess.run(life.start.command, capture_output=True, text=True, timeout=life.start.timeout_sec)
    if cp.returncode != 0:
        cmd_str = " ".join(life.start.command)
        _warn(
            f"Could not auto-start agent: `{cmd_str}` exited {cp.returncode}. "
            f"Start manually, then run `argit doctor`."
        )


def _check_chmod(path: Path, mode: str) -> None:
    path.chmod(int(mode, 8))


def _file_starts_with(path: Path, prefix: str) -> bool:
    try:
        with path.open("rb") as fp:
            head = fp.read(len(prefix.encode("utf-8")))
        return head.startswith(prefix.encode("utf-8"))
    except OSError:
        return False


def _maybe_warn_cross_instance(repo_root: Path, dry: bool, yes: bool) -> None:
    last = repo_root / ".argit" / "last-backup.json"
    if not last.is_file():
        return
    try:
        body = json.loads(last.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    other = body.get("hostname")
    if not other:
        return
    here = socket.gethostname()
    if other == here:
        return
    msg = (
        f"This backup was taken on host '{other}'. Restoring on '{here}' creates a second "
        f"agent with the same identity/device.json. In the Blink ecosystem, duplicate device "
        f"IDs cause pairing conflicts. Proceed only if migrating (old host is retired); press "
        f"Enter to continue, Ctrl-C to abort. Pass --yes to suppress."
    )
    click.echo(msg, err=True)
    if dry or yes:
        return
    try:
        click.get_text_stream("stdin").readline()
    except KeyboardInterrupt:
        raise click.exceptions.Abort()


def _confirm_destructive(target: Path, yes: bool, dry: bool) -> None:
    msg = (
        f"About to recursively remove `{target}` — all contents will be lost, including any files "
        f"not covered by the manifest. Press Enter to continue, Ctrl-C to abort."
    )
    if yes or dry:
        return
    click.echo(msg, err=True)
    try:
        click.get_text_stream("stdin").readline()
    except KeyboardInterrupt:
        raise click.exceptions.Abort()


def run_restore(repo_root: Path, *, target: str | None, overwrite: bool, merge: bool, yes: bool,
                force: bool, skip_lifecycle: bool, dry_run: bool) -> int:
    check_no_partial_state(repo_root, "restore")
    pre = run_preflight(repo_root, require_manifest=True, require_gpg_id=True)
    manifest = load_manifest(repo_root)

    target_path, manifest_src, is_scratch = _resolve_paths(manifest.source_root, target)

    pass_wrap = PassWrap(repo_root / "secrets")

    _maybe_warn_cross_instance(repo_root, dry_run, yes)

    with acquire_lock(repo_root):
        # 2. Lifecycle running-check. Detect_running is read-only; lifecycle.stop
        # mutates the agent process (not the repo/target). Failures here leave
        # the working tree and target dir unchanged, so the partial-state
        # marker is not needed yet.
        if not skip_lifecycle and manifest.lifecycle is not None:
            life = manifest.lifecycle
            running = _detect_running(life, dry_run)
            if running:
                if force:
                    _warn("Restoring while agent is running — corruption possible")
                elif life.stop is not None:
                    _stop_and_wait(life, dry_run)
                else:
                    raise ArgitError(
                        f"agent is running but manifest has no lifecycle.stop",
                        "stop it manually, then retry, or pass --force to proceed anyway",
                    )

        # The marker enters HERE — right before the first mutation of the
        # target directory. Preflight, lifecycle detect/stop, and target
        # resolution are non-destructive to the backup repo and target dir.
        with in_progress_marker(repo_root):
            # 3. Target resolution
            if target_path.exists() and any(target_path.iterdir()):
                if overwrite:
                    _confirm_destructive(target_path, yes, dry_run)
                    if dry_run:
                        _emit(True, f"rmtree {target_path}")
                    else:
                        shutil.rmtree(target_path, ignore_errors=False)
                elif merge:
                    pass  # leave; overlay
                else:
                    raise ArgitError(
                        f"Target `{target_path}` exists and is non-empty",
                        "Use --overwrite to remove it first, --merge to overlay, or --target <dir> for a scratch restore.",
                    )

            if dry_run:
                _emit(True, f"mkdir -p {target_path}")
            else:
                target_path.mkdir(parents=True, exist_ok=True)

            # Track B: expand globbed items once. Non-secret globs enumerate
            # via repo-filesystem (AC-B7); secret globs enumerate from the
            # pass store. Runtime duplicate detection is internal to the
            # helper (AC-INT5). Zero-match globs produce no concrete items
            # for that entry and emit a restore-time warning via `_warn` —
            # expected when the repo is legitimately missing matches.
            concrete_items = expand_items_for_restore(
                manifest, repo_root, pass_entries=pass_wrap.ls(), warn=_warn,
            )

            # Track what we wrote, for verify.
            written_files: list[tuple[Path, str]] = []
            # Pass paths we deliberately skipped because the secret was never
            # backed up (source was absent at backup time). Verify must not
            # re-flag these — restore already warned, and the absence is
            # legitimate, not corruption.
            skipped_secret_pass_paths: set[str] = set()

            # 4. Sanitized-config restore
            for sf in manifest.sanitize:
                src_committed = repo_root / sf.target
                dst = target_path / sf.file
                if not src_committed.is_file():
                    _warn(f"sanitize repo source missing: {sf.target} (skipping)")
                    continue
                if dry_run:
                    _emit(True, f"reinject {sf.target} → {dst}")
                    continue
                sanitized = json.loads(src_committed.read_text(encoding="utf-8"))
                try:
                    full = reinject(sanitized, pass_wrap.show)
                except ArgitError as exc:
                    raise exc
                except Exception as exc:
                    raise ArgitError(
                        f"reinject failed for {sf.file}: {exc}",
                        "run `argit doctor` to audit secrets/",
                    ) from exc
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(json.dumps(full, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                _check_chmod(dst, sf.mode)
                written_files.append((dst, sf.mode))
                _emit(False, f"restore: {sf.file}")

            # 5. Whole-file secrets restore
            for it in [i for i in concrete_items if i.kind == "secret"]:
                dst = target_path / it.source
                if dry_run:
                    _emit(True, f"pass show {it.pass_path} → {dst}")
                    continue
                if not pass_wrap.has(it.pass_path):
                    _warn(f"pass entry missing: {it.pass_path} (skipping {it.source})")
                    skipped_secret_pass_paths.add(it.pass_path)
                    continue
                value = pass_wrap.show(it.pass_path)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(value, encoding="utf-8")
                _check_chmod(dst, it.mode)
                written_files.append((dst, it.mode))
                _emit(False, f"secret: {it.source}")

            # 6. Data restore
            for it in [i for i in concrete_items if i.kind == "data"]:
                src = repo_root / it.target
                dst = target_path / it.source.rstrip("/") if it.is_dir_source else target_path / it.source
                if it.is_dir_source:
                    if not src.is_dir():
                        _warn(f"data repo source missing: {it.target} (skipping)")
                        continue
                    if dry_run:
                        _emit(True, f"copytree {it.target} → {dst}")
                        continue
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
                else:
                    if not src.is_file():
                        _warn(f"data repo source missing: {it.target} (skipping)")
                        continue
                    if dry_run:
                        _emit(True, f"copy {it.target} → {dst}")
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    _check_chmod(dst, it.mode)
                    written_files.append((dst, it.mode))
                _emit(False, f"data: {it.source}")

            # 7. SQLite restore
            for it in [i for i in concrete_items if i.kind == "sqlite"]:
                src = repo_root / it.target
                dst = target_path / it.source
                if not src.is_file():
                    _warn(f"sqlite repo source missing: {it.target} (skipping)")
                    continue
                if dry_run:
                    _emit(True, f"copy sqlite snapshot {it.target} → {dst}")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                _check_chmod(dst, it.mode)
                written_files.append((dst, it.mode))
                _emit(False, f"sqlite: {it.source}")

            # 8. Blob restore. Pre-scan repo-side blob sources for LFS pointers
            # BEFORE copying anything — failing mid-loop would leave partial
            # state in the target.
            blob_items = [i for i in concrete_items if i.kind == "blob"]
            for it in blob_items:
                src = repo_root / it.target
                if not src.is_dir():
                    continue
                for f in src.rglob("*"):
                    if f.is_file() and _file_starts_with(f, LFS_POINTER_PREFIX):
                        raise ArgitError(
                            f"{f} is a git-lfs pointer (not real content)",
                            "run `git lfs pull` then retry `argit restore`",
                        )
            for it in blob_items:
                src = repo_root / it.target
                dst = target_path / it.source.rstrip("/")
                if not src.is_dir():
                    _warn(f"blob repo source missing: {it.target} (skipping)")
                    continue
                if dry_run:
                    _emit(True, f"copytree blobs {it.target} → {dst}")
                    continue
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
                _emit(False, f"blob: {it.source}")

            # 9. Permissions on source_root (= target dir)
            if not dry_run:
                _check_chmod(target_path, manifest.source_root_mode)
            else:
                _emit(True, f"chmod {manifest.source_root_mode} {target_path}")

            # 10. Verify
            verify_failures: list[str] = []
            if not dry_run:
                # (a) no leftover ${pass:} placeholders in restored sanitize files
                for sf in manifest.sanitize:
                    dst = target_path / sf.file
                    if not dst.is_file():
                        continue
                    try:
                        body = json.loads(dst.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        verify_failures.append(f"{dst}: not parseable JSON")
                        continue
                    leftovers = find_placeholders(body)
                    for path_, pp in leftovers:
                        verify_failures.append(f"{dst}: leftover placeholder at {path_} (pass: {pp})")

                # (b) every kind: secret pass path resolves — but skip items
                # the restore loop already warned about as legitimately
                # absent (source missing at backup → no pass entry written).
                for it in [i for i in concrete_items if i.kind == "secret"]:
                    if it.pass_path in skipped_secret_pass_paths:
                        continue
                    if not pass_wrap.has(it.pass_path):
                        verify_failures.append(f"pass path missing: {it.pass_path}")

                # (c) every SQLite file opens cleanly
                for it in [i for i in concrete_items if i.kind == "sqlite"]:
                    dst = target_path / it.source
                    if not dst.is_file():
                        continue
                    cp = subprocess.run(
                        ["sqlite3", str(dst), "PRAGMA integrity_check;"],
                        capture_output=True, text=True, timeout=30,
                    )
                    out = (cp.stdout or "").strip()
                    if cp.returncode != 0 or out != "ok":
                        verify_failures.append(f"sqlite integrity_check failed for {dst}: {out or cp.stderr}")

                # (d) modes match manifest
                for path, expected_mode in written_files:
                    if not path.is_file():
                        continue
                    actual_int = path.stat().st_mode & 0o7777
                    expected_int = int(expected_mode, 8)
                    if actual_int != expected_int:
                        verify_failures.append(
                            f"mode mismatch on {path}: expected 0{oct(expected_int)[2:]}, got 0{oct(actual_int)[2:]}"
                        )

                # (e) check for LFS pointers in blob outputs (extra safety)
                for it in [i for i in concrete_items if i.kind == "blob"]:
                    dst = target_path / it.source.rstrip("/")
                    if not dst.is_dir():
                        continue
                    for f in dst.rglob("*"):
                        if f.is_file() and _file_starts_with(f, LFS_POINTER_PREFIX):
                            verify_failures.append(f"LFS pointer in restored blob: {f} — run `git lfs pull` then retry")

            if verify_failures:
                click.echo("✗ verify failed:", err=True)
                for f in verify_failures:
                    click.echo(f"  - {f}", err=True)
                return EXIT_VERIFY_FAIL

            # 11. Lifecycle start (skip on scratch target)
            if (not skip_lifecycle) and (not is_scratch) and manifest.lifecycle is not None:
                _start_after_restore(manifest.lifecycle, dry_run)

    _emit(False, "restore complete; verify ok")
    return EXIT_OK

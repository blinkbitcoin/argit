"""Shared utilities: platform check, fcntl lock, in-progress marker,
pre-flight checks, constants.

Most of this is used by both `cli.py` (preflight) and each subcommand
module. Kept in one place to avoid circular imports (cli imports every
subcommand; each subcommand needs preflight).
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .errors import ArgitError


def matches_exclude(rel: Path, patterns: list[str]) -> bool:
    """Match `rel` against a manifest exclude pattern.

    Pattern semantics (intentionally looser than shell glob):
    - Trailing `/` → directory prefix; matches the directory and everything under it.
    - `*` matches across path separators (so `*.sqlite-wal` matches
      `tasks/runs.sqlite-wal`). This deviates from POSIX `fnmatch` but matches
      operator intent for manifest-author-friendly patterns.

    Hoisted from backup.py (pre-Track-B) so expand_globbed_item in manifest.py
    can share the logic without creating a backup.py → manifest.py cycle.
    """
    s = str(rel)
    for pat in patterns:
        if pat.endswith("/") and (s + "/").startswith(pat):
            return True
        if fnmatch.fnmatch(s, pat):
            return True
        if pat.endswith("/") and s.startswith(pat):
            return True
    return False

IT_BACKUP_FPR = "1107BD74F292CD3EAB0CF59D49F2D3353A88D34E"
IT_BACKUP_UID = "IT Backup <a@blinkbtc.com>"

EXIT_OK = 0
EXIT_FIRST_TOUCH = 1
EXIT_USAGE = 2  # click emits this
EXIT_VERIFY_FAIL = 3
EXIT_LOCK_CONTENTION = 4
EXIT_PARTIAL_STATE = 5

LOCK_TIMEOUT_SEC = 5
REQUIRED_PYTHON = (3, 10)


# ---------- platform ----------

def require_supported_platform() -> None:
    if sys.platform not in ("linux", "darwin"):
        raise ArgitError(
            f"unsupported platform: {sys.platform}",
            "argit MVP targets Linux and macOS only; Windows is out of scope",
        )


def require_python() -> None:
    if sys.version_info[:2] < REQUIRED_PYTHON:
        raise ArgitError(
            f"Python {sys.version.split()[0]} is older than required {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
            "install Python 3.10+: brew install python@3.12 (Mac) / apt install python3.12 (Debian)",
        )


# ---------- binary-on-path ----------

_INSTALL_HINTS = {
    "gpg": ("brew install gnupg", "apt install gnupg"),
    "pass": ("brew install pass", "apt install pass"),
    "sqlite3": ("brew install sqlite", "apt install sqlite3"),
    "git": ("brew install git", "apt install git"),
    "git-lfs": ("brew install git-lfs && git lfs install", "apt install git-lfs && git lfs install"),
}


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        mac, deb = _INSTALL_HINTS.get(name, (f"brew install {name}", f"apt install {name}"))
        raise ArgitError.cmd_not_found(name, mac, deb)


# ---------- git-lfs filter configured ----------

def check_lfs_filter_configured() -> None:
    """Effective git-config (system→global→local→worktree). Empty when missing."""
    try:
        cp = subprocess.run(
            ["git", "config", "--get", "filter.lfs.clean"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError as exc:
        raise ArgitError.cmd_not_found("git", "brew install git", "apt install git") from exc
    if cp.returncode != 0 or not cp.stdout.strip():
        raise ArgitError(
            "git-lfs filter is not configured (filter.lfs.clean missing)",
            "run `git lfs install` to register the clean/smudge filters",
        )


# ---------- git-repo-cwd ----------

def require_git_repo(repo_root: Path) -> None:
    if not (repo_root / ".git").exists():
        raise ArgitError(
            f"cwd is not a git repo ({repo_root})",
            "run `git init` first, then `argit setup`",
        )


# ---------- .gpg-id ----------

_HEX_FPR_RE = re.compile(r"^[A-Fa-f0-9]{8,40}$")


def read_gpg_id(secrets_dir: Path) -> list[str]:
    gpg_id = secrets_dir / ".gpg-id"
    if not gpg_id.is_file():
        raise ArgitError(
            f"{gpg_id} not found (pass store not initialized)",
            f"run: cd {secrets_dir.name} && PASSWORD_STORE_DIR=. pass init <agent-fpr> {IT_BACKUP_FPR}",
        )
    lines = [ln.strip() for ln in gpg_id.read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise ArgitError(
            f"{gpg_id} is empty",
            f"run: cd {secrets_dir.name} && PASSWORD_STORE_DIR=. pass init <agent-fpr> {IT_BACKUP_FPR}",
        )
    return lines


# ---------- locks + markers ----------

IN_PROGRESS = Path(".argit/in-progress")
LOCK_FILE = Path(".argit/lock")


@contextlib.contextmanager
def acquire_lock(repo_root: Path) -> Iterator[None]:
    path = repo_root / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = path.open("a+")
    deadline = time.monotonic() + LOCK_TIMEOUT_SEC
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                fp.close()
                other = path.read_text().strip() or "<unknown>"
                err = ArgitError(
                    f"another argit process holds {path}",
                    f"wait for it to finish or investigate (`ps {other}`)",
                )
                err.exit_code = EXIT_LOCK_CONTENTION  # type: ignore[attr-defined]
                raise err
            time.sleep(0.1)
    try:
        # Write pid AND truncate in one shot — order: write, then truncate at
        # current position. Avoids an empty-file window for a contender's
        # `path.read_text()`.
        content = f"{os.getpid()}\n"
        fp.seek(0)
        fp.write(content)
        fp.truncate(fp.tell())
        fp.flush()
        yield
    finally:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        finally:
            fp.close()


def in_progress_path(repo_root: Path) -> Path:
    return repo_root / IN_PROGRESS


def check_no_partial_state(repo_root: Path, cmd: str) -> None:
    p = in_progress_path(repo_root)
    if p.exists():
        err = ArgitError(
            f"a previous {cmd} interrupted — working tree may be partially updated",
            f"inspect with `git status`, delete {p} when satisfied, then retry",
        )
        err.exit_code = EXIT_PARTIAL_STATE  # type: ignore[attr-defined]
        raise err


@contextlib.contextmanager
def in_progress_marker(repo_root: Path) -> Iterator[None]:
    p = in_progress_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{os.getpid()}\n")
    try:
        yield
    except BaseException:
        # Keep the marker on failure so the next run sees it.
        raise
    else:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


# ---------- PreflightResult ----------

@dataclass
class PreflightResult:
    repo_root: Path
    source_root: Path | None = None
    gpg_id_recipients: list[str] = field(default_factory=list)
    remote_url: str | None = None
    hostname: str = field(default_factory=socket.gethostname)
    manifest_path: Path | None = None


def run_preflight(repo_root: Path, *, require_manifest: bool, require_gpg_id: bool) -> PreflightResult:
    require_python()
    require_supported_platform()
    for b in ("gpg", "pass", "sqlite3", "git", "git-lfs"):
        require_binary(b)
    check_lfs_filter_configured()
    require_git_repo(repo_root)

    result = PreflightResult(repo_root=repo_root)

    if require_manifest:
        from .manifest import find_manifest_file
        result.manifest_path = find_manifest_file(repo_root)
    if require_gpg_id:
        result.gpg_id_recipients = read_gpg_id(repo_root / "secrets")

    cp = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_root),
        capture_output=True, text=True, timeout=10,
    )
    if cp.returncode == 0:
        result.remote_url = cp.stdout.strip()
    return result

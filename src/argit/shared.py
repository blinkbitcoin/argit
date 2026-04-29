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
from typing import TYPE_CHECKING, Iterable, Iterator

from .errors import ArgitError

if TYPE_CHECKING:
    from .manifest import Item, SanitizeFile


def matches_exclude(rel: Path, patterns: list[str]) -> bool:
    """Match `rel` against a manifest exclude pattern.

    Pattern semantics (intentionally looser than shell glob):
    - Trailing `/` → directory prefix; matches the directory and everything under it.
    - `*` matches across path separators (so `*.sqlite-wal` matches
      `tasks/runs.sqlite-wal`). This deviates from POSIX `fnmatch` but matches
      operator intent for manifest-author-friendly patterns.
    - The two above compose: `agents/*/sessions/` matches
      `agents/main/sessions/foo.json` AND `agents/erbot/sessions/bar.jsonl`.
      Same for `memory/lancedb.bak*/` et al.

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
        # Glob + directory-prefix composition: the three checks above handle
        # (literal-dir-prefix) OR (glob-exact-match) but neither catches a
        # pattern like `agents/*/sessions/` against a deeper path like
        # `agents/main/sessions/foo.json` — fnmatch requires the path to also
        # end in `/`, and literal startswith can't expand the `*`. Treat a
        # trailing-slash pattern as a dir-prefix that allows anything below.
        if pat.endswith("/") and fnmatch.fnmatch(s, pat + "*"):
            return True
        # Also: Path normalizes `foo/` → `foo`, so a caller asking whether
        # the directory itself is excluded passes no trailing slash. Match
        # the pattern minus its trailing `/` so the directory entry itself
        # is covered too.
        if pat.endswith("/") and fnmatch.fnmatch(s, pat.rstrip("/")):
            return True
    return False


# ---------- coverage helpers ----------
# Lifted from backup.py so review.py can share the same coverage check
# without duplicating logic. Mirrors the existing matches_exclude precedent.


def walk_relative(root: Path) -> Iterable[Path]:
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


def is_under(rel: Path, prefix: str) -> bool:
    """Path covers a path or directory prefix (`source` ending with `/`)."""
    if prefix.endswith("/"):
        prefix_clean = prefix.rstrip("/")
        return str(rel).startswith(prefix_clean + "/") or str(rel) == prefix_clean
    return str(rel) == prefix


def glob_pattern_matches(rel_str: str, pattern: str) -> bool:
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


def covered_by_items(rel: Path, items: list[Item]) -> bool:
    rel_str = str(rel)
    for it in items:
        if it.is_globbed:
            if glob_pattern_matches(rel_str, it.source):
                return True
        else:
            if is_under(rel, it.source):
                return True
    return False


def covered_by_sanitize(rel: Path, sf: list[SanitizeFile]) -> bool:
    s = str(rel)
    for f in sf:
        if s == f.file:
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
VERSION_CHECK_TIMEOUT_SEC = 5


# ---------- version parsing ----------

_VER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:-[0-9A-Za-z.]+)?$")


def version_parseable(s: str) -> bool:
    return bool(_VER_RE.match(s))


def version_cmp(a: str, b: str) -> int:
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


def probe_agent_version(binary: str) -> str | None:
    """Run `<binary> --version` and return the first parseable version token.
    Returns None on every failure mode (binary missing, timeout, non-zero exit,
    no parseable token) — caller decides how to react. Never raises.
    """
    if shutil.which(binary) is None:
        return None
    try:
        cp = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True,
            timeout=VERSION_CHECK_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if cp.returncode != 0:
        return None
    raw = (cp.stdout + cp.stderr).strip().split()
    if not raw:
        return None
    return next(
        (t for t in (c.lstrip("v") for c in raw) if version_parseable(t)),
        None,
    )


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


def _working_tree_is_clean(repo_root: Path) -> bool:
    """`git status --porcelain` empty → no staged, unstaged, or untracked
    changes. Used to decide whether an interrupted command left any state
    behind; if not, the marker is safe to auto-clear."""
    try:
        cp = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False  # cannot verify → keep the marker
    if cp.returncode != 0:
        return False
    return cp.stdout.strip() == ""


def check_no_partial_state(repo_root: Path, cmd: str) -> None:
    """Block execution when a previous run was interrupted mid-mutation.

    If the marker exists but the working tree is demonstrably clean (no
    staged/unstaged/untracked changes), the previous run exited before
    committing any state — typically an operator Ctrl-C during a hung
    pinentry prompt. In that case, auto-clear the marker and continue.
    When the tree is dirty, keep the hard error so the operator inspects
    first.
    """
    p = in_progress_path(repo_root)
    if not p.exists():
        return
    if _working_tree_is_clean(repo_root):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
        return
    err = ArgitError(
        f"a previous {cmd} interrupted — working tree has uncommitted changes",
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

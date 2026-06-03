"""argit doctor — non-mutating status report.

Runs every pre-flight check with a non-raising wrapper, accumulates results,
prints one line per check (✓/✗ + remediation), exits 0 if all pass, 1 otherwise.
Also runs additional checks: push-auth probe and recipient count on .gpg-id.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import click

from . import path_conventions
from .errors import ArgitError
from .gpgwrap import GpgWrap
from .manifest import Manifest, find_manifest_file, load_manifest
from .shared import (
    IT_BACKUP_FPR,
    REQUIRED_PYTHON,
    check_lfs_filter_configured,
    read_gpg_id,
    require_binary,
)


CheckFn = Callable[[], None]


def _check(name: str, fn: CheckFn) -> tuple[str, bool, str | None]:
    try:
        fn()
        return (name, True, None)
    except ArgitError as exc:
        return (name, False, exc.remediation)
    except Exception as exc:  # surface unexpected errors as failed checks
        return (name, False, f"unexpected error: {exc}")


def _check_python() -> None:
    if sys.version_info[:2] < REQUIRED_PYTHON:
        raise ArgitError(
            f"Python {sys.version.split()[0]} < {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
            "install Python 3.10+: brew install python@3.12 / apt install python3.12",
        )


def _check_git_repo(repo_root: Path) -> CheckFn:
    def _fn() -> None:
        if not (repo_root / ".git").exists():
            raise ArgitError(f"cwd is not a git repo ({repo_root})", "run `git init` first, then `argit setup`")
    return _fn


def _check_manifest(repo_root: Path) -> CheckFn:
    def _fn() -> None:
        find_manifest_file(repo_root)
    return _fn


def _check_gitattributes(repo_root: Path, manifest: Manifest | None) -> CheckFn:
    def _fn() -> None:
        if manifest is None:
            raise ArgitError(
                ".gitattributes LFS check needs a loadable manifest",
                "run `argit setup` to install a manifest first",
            )
        patterns = path_conventions.lfs_patterns(manifest.agent_type)
        ga = repo_root / ".gitattributes"
        body = ga.read_text(encoding="utf-8") if ga.is_file() else ""
        missing = [pattern for pattern in patterns if pattern not in body]
        if missing:
            raise ArgitError(
                f".gitattributes missing LFS line(s) for {missing}",
                "run `argit setup`",
            )
    return _fn


def _check_recipient_keys_present(_gpg: GpgWrap, repo_root: Path) -> CheckFn:
    def _fn() -> None:
        gpg_id = repo_root / "secrets" / ".gpg-id"
        if not gpg_id.is_file():
            return
        for fpr in read_gpg_id(repo_root / "secrets"):
            if not _gpg.is_key_imported(fpr):
                raise ArgitError(
                    f"recipient {fpr} from .gpg-id not in keyring",
                    "import its public key or re-run argit setup",
                )
    return _fn


def _check_personal_key(_gpg: GpgWrap) -> CheckFn:
    def _fn() -> None:
        personal = _gpg.list_personal_keys(exclude_fpr=IT_BACKUP_FPR)
        if len(personal) == 0:
            raise ArgitError(
                "no personal GPG key found",
                "create one: gpg --full-generate-key (RSA 4096, no expiry)",
            )
    return _fn


def _check_gpg_id(repo_root: Path) -> CheckFn:
    def _fn() -> None:
        gpg_id = repo_root / "secrets" / ".gpg-id"
        if not gpg_id.is_file():
            raise ArgitError(
                f"{gpg_id} not found (pass store not initialized)",
                "run `argit setup`",
            )
        lines = [ln.strip() for ln in gpg_id.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            raise ArgitError(
                f"{gpg_id} is empty",
                "cd secrets && PASSWORD_STORE_DIR=. pass init <agent-fpr> <backup-fpr>",
            )
    return _fn


def _classify_manifest_drift(repo_root: Path) -> tuple[str, bool, str | None]:
    """Informational bundled-vs-in-repo drift row. NEVER flips doctor's exit
    code — drift (stale bundle / operator-modified) is a condition to report,
    not a broken-health failure. Returns ok=True always; the remediation
    string carries the drift detail so it prints under the ✓.

    For a machine-readable channel use `argit drift --json` (issue #25); this
    shares the same `collect_drift` core so the two never disagree.
    """
    from .drift import collect_drift

    try:
        payload = collect_drift(repo_root)
    except ArgitError:
        # agent-type mismatch / no bundled manifests — surfaced by other rows.
        return ("manifest drift", True, None)
    state = payload["state"]
    if state == "clean":
        return ("manifest up-to-date", True, None)
    if state == "no_manifest":
        return ("manifest drift", True, None)  # manifest-missing row already covers this
    if state == "stale_bundle":
        if payload["revisions_behind"] is not None:
            gap = f"rev {payload['repo_revision']} → {payload['bundled_revision']}"
        else:  # cross-version-family: revision numbers aren't comparable
            gap = f"{payload['manifest_file']} → {payload['bundled_manifest_file']}"
        return (
            "manifest drift",
            True,
            f"stale bundle: {gap} available — run `argit setup` to upgrade",
        )
    return (
        "manifest drift",
        True,
        f"operator-modified ({payload['manifest_file']}) — extensions belong in "
        "`.manifest.local.json`; see MANIFEST.md §Overlay",
    )


def _classify_gpg_id(repo_root: Path) -> tuple[str, bool, str | None]:
    """Recipient-count classification — produces an informational ✓/✗ line."""
    try:
        recipients = read_gpg_id(repo_root / "secrets")
    except ArgitError:
        return ("recipient count", False, "run `argit setup`")
    if len(recipients) >= 2:
        return ("recipient count", True, None)
    return (
        "recipient count",
        False,
        f"expected >=2 recipients (agent + backup); got {len(recipients)}. Add a backup recipient: "
        "cd secrets && PASSWORD_STORE_DIR=. pass init <agent-fpr> <backup-fpr>",
    )


def _push_auth_probe(repo_root: Path) -> tuple[str, bool, str | None]:
    """Three cases: no remote / works / fails."""
    cp = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=10,
    )
    if cp.returncode != 0:
        return ("push auth", True, None)  # no remote → ✓ (skipped)
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "SSH_ASKPASS": "/bin/true",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=5",
    })
    try:
        cp2 = subprocess.run(
            ["git", "push", "--dry-run", "--porcelain", "origin", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10, env=env,
        )
    except subprocess.TimeoutExpired:
        return ("push auth", False, "push --dry-run timed out — see README §Troubleshooting")
    if cp2.returncode == 0:
        return ("push auth", True, None)
    return (
        "push auth",
        False,
        "push auth not configured — see README §Troubleshooting (gh auth login, SSH key, or deploy-key pattern)",
    )


def run_doctor(repo_root: Path) -> int:
    gpg = GpgWrap()
    results: list[tuple[str, bool, str | None]] = []

    # Load manifest once — used by the gitattributes check (needs agent_type)
    # and the lifecycle-preview at the bottom. Manifest-missing surfaces as
    # its own failed check ("manifest in .argit/manifest/"); the gitattributes
    # check then cleanly reports its own failure via the None branch.
    try:
        manifest = load_manifest(repo_root)
    except ArgitError:
        manifest = None

    results.append(_check(f"python ≥ {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}", _check_python))
    for b in ("gpg", "pass", "sqlite3", "git", "git-lfs"):
        results.append(_check(f"{b} on PATH", lambda b=b: require_binary(b)))
    results.append(_check("git-lfs filter configured", check_lfs_filter_configured))
    results.append(_check("cwd is git repo", _check_git_repo(repo_root)))
    results.append(_check("manifest in .argit/manifest/", _check_manifest(repo_root)))
    results.append(_check(".gitattributes has LFS line", _check_gitattributes(repo_root, manifest)))
    results.append(_check(".gpg-id recipient keys present", _check_recipient_keys_present(gpg, repo_root)))
    results.append(_check("personal GPG key present", _check_personal_key(gpg)))
    results.append(_check("secrets/.gpg-id present + non-empty", _check_gpg_id(repo_root)))
    results.append(_classify_manifest_drift(repo_root))
    results.append(_classify_gpg_id(repo_root))
    results.append(_push_auth_probe(repo_root))

    failed = [r for r in results if not r[1]]
    for name, ok, remediation in results:
        if ok:
            click.echo(f"✓ {name}")
            # An ok row may still carry detail (informational rows like
            # manifest drift) — print it as a note, not a remediation.
            if remediation:
                click.echo(f"  ℹ {remediation}")
        else:
            click.echo(f"✗ {name}")
            if remediation:
                click.echo(f"  → {remediation}")

    # Lifecycle preview (informational only). Reuses the manifest loaded at
    # the top of run_doctor so we don't parse it twice.
    m = manifest
    if m is not None and m.lifecycle is not None:
        click.echo("\nLifecycle commands argit would execute on `argit restore`:")
        for label, cmd in (
            ("detect_running", m.lifecycle.detect_running),
            ("stop", m.lifecycle.stop),
            ("start", m.lifecycle.start),
        ):
            if cmd is not None:
                click.echo(f"  {label}: {cmd.command}")

    return 1 if failed else 0

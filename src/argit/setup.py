"""argit setup — one-time bootstrapping inside an existing git-init'd repo.

Idempotent. See tech-spec-01-mvp.md §Task 9.1 for the canonical sequence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import click

from . import path_conventions
from .errors import ArgitError
from .gpgwrap import GpgWrap
from .hashing import canonical_hash
from .manifest import parse_filename
from .shared import (
    IT_BACKUP_FPR,
    IT_BACKUP_UID,
    acquire_lock,
    check_lfs_filter_configured,
    in_progress_marker,
    probe_agent_version,
    require_binary,
    require_git_repo,
    require_python,
    require_supported_platform,
    version_cmp,
)

PASS_INIT_TIMEOUT_SEC = 30


def _emit(dry_run: bool, action: str) -> None:
    """Emit a status line.

    `dry_run=True` → `would: ` prefix (the action would be performed).
    `dry_run=False` → `✓ ` prefix (the action either was performed or was
    already satisfied — both are OK from the operator's POV).
    """
    prefix = "would: " if dry_run else "✓ "
    click.echo(f"{prefix}{action}")


def _already(message: str) -> None:
    """Status line for a no-op check ("already done") — distinguishable from
    `would:` (dry-run will-do) and `✓` (just-did).
    """
    click.echo(f"= {message}")


def _bundled_manifest_path(agent_type: str = "openclaw", agent_version: str | None = None) -> Path:
    """Return the bundled manifest to use.

    `agent_version=None`: latest available across all versions (highest
    `agent_version`, then highest `revision` within that). Used when no live
    agent version is detectable.

    `agent_version="2026.4.26"`: best-fit — highest `agent_version` ≤
    requested, then highest revision. So an operator running OpenClaw
    2026.4.26 with no exact-match manifest gets the most recent older one
    (e.g. 2026.4.14-7) rather than something targeting a future schema.

    Older revisions remain shipped for the hash catalog (drift detection)
    and audit trail.

    Raises if no manifest exists at or below `agent_version` (caller can
    fall back to None to install latest, or fail loudly).
    """
    pkg = resources.files("argit.manifest_templates")
    prefix = f"{agent_type}-"
    suffix = ".manifest.json"
    candidates: list[tuple[str, int, Path]] = []
    for entry in pkg.iterdir():
        name = entry.name
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        body = name[len(prefix):-len(suffix)]
        ver, _, rev_str = body.rpartition("-")
        if not ver or not rev_str:
            continue
        try:
            rev = int(rev_str)
        except ValueError:
            continue
        candidates.append((ver, rev, Path(str(entry))))
    if not candidates:
        raise ArgitError(
            f"no bundled manifests found for {agent_type}",
            "verify the argit installation; manifest templates should ship inside the package",
        )
    pool = candidates if agent_version is None else [
        c for c in candidates if version_cmp(c[0], agent_version) <= 0
    ]
    if not pool:
        raise ArgitError(
            f"no bundled manifest found for {agent_type} at or below version {agent_version}",
            "all shipped manifests target a newer agent — upgrade the agent or downgrade argit",
        )
    # Sort by (agent_version, revision); version_cmp gives the dotted-numeric
    # ordering, revision is a plain int tiebreak.
    pool.sort(key=lambda c: ([int(p) for p in c[0].split("-", 1)[0].split(".") if p.isdigit()], c[1]))
    return pool[-1][2]


def _all_bundled_manifest_paths() -> list[Path]:
    """Every bundled manifest revision — used by QS4 (manifest-update) to
    compute the known-hash catalog."""
    pkg = resources.files("argit.manifest_templates")
    return sorted(Path(str(e)) for e in pkg.iterdir() if e.name.endswith(".manifest.json"))


def _bundled_it_key_path() -> Path:
    res = resources.files("argit.keys").joinpath("it-backup-pubkey.asc")
    return Path(str(res))


def _load_hash_catalog() -> dict[str, str]:
    """Load the shipped hash catalog.

    Returns `{filename: hex_digest}`. Empty dict if the catalog is missing
    (pre-Track-A argit installs, build-time glitches) — callers treat this
    as "no catalog" and degrade gracefully.
    """
    # Address via the `argit` package with a path suffix rather than via
    # `argit.manifest_templates` — the latter works in source trees but is
    # not guaranteed resolvable after `pip install` when manifest_templates/
    # has no __init__.py (not an importable subpackage under setuptools'
    # packages.find). Joining from the known-importable `argit` package is
    # stable across source + installed wheels. (The three pre-existing
    # `resources.files("argit.manifest_templates")` sites in this file
    # predate Track A and are out of scope for this PR — worth a follow-up.)
    candidate = Path(str(resources.files("argit").joinpath("manifest_templates/hashes.json")))
    if not candidate.is_file():
        return {}
    try:
        body = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArgitError(
            f"hash catalog hashes.json is malformed: {exc.msg} (line {exc.lineno})",
            "reinstall argit; the shipped catalog is required for drift detection",
        ) from exc
    if not isinstance(body, dict):
        raise ArgitError(
            "hash catalog hashes.json must be a JSON object",
            "reinstall argit; the shipped catalog is corrupt",
        )
    return {str(k): str(v) for k, v in body.items()}


def _classify_drift(repo_manifest_path: Path) -> tuple[str, int | None]:
    """Hash-only drift classifier — does NOT invoke load_manifest.

    Decoupling from the parser is load-bearing (F2): a pre-spec-02 or
    otherwise grammar-incompatible manifest in the repo must still
    classify so Track A's upgrade path is reachable.

    Returns:
      ("clean", None)             — hash matches the current bundled manifest
      ("stale_bundle", N)         — hash matches catalog entry for revision N,
                                     where N is older than the current bundled
      ("operator_modified", None) — hash matches nothing in the catalog
    """
    digest = canonical_hash(repo_manifest_path)
    catalog = _load_hash_catalog()
    if not catalog:
        # No catalog shipped → every manifest classifies as operator-modified
        # (the safest default — leave untouched).
        return ("operator_modified", None)

    # Build reverse lookup: hex_digest → (filename, revision)
    by_digest: dict[str, tuple[str, int]] = {}
    for name, h in catalog.items():
        try:
            _, _, rev = parse_filename(name)
        except ArgitError:
            continue  # catalog entry with non-conforming name — skip
        by_digest[h] = (name, rev)

    match = by_digest.get(digest)
    if match is None:
        return ("operator_modified", None)

    # Is this the current bundled? Compare against the highest-revision
    # catalog entry for the same agent_type/agent_version.
    match_name, match_rev = match
    try:
        match_type, match_ver, _ = parse_filename(match_name)
    except ArgitError:
        return ("operator_modified", None)

    same_family_revs = []
    for name in catalog:
        try:
            t, v, r = parse_filename(name)
        except ArgitError:
            continue
        if t == match_type and v == match_ver:
            same_family_revs.append(r)
    latest = max(same_family_revs) if same_family_revs else match_rev

    if match_rev == latest:
        return ("clean", None)
    return ("stale_bundle", match_rev)


def _cleanup_stale_upgrade_files(manifest_dir: Path, yes: bool, dry_run: bool) -> None:
    """Remove or warn about stray `*.manifest.json.new` files from a crashed
    upgrade.

    Zero-byte: genuine interrupted write signature → always remove.
    Non-zero: could be an operator backup copy → requires --yes (F15).
    """
    if not manifest_dir.is_dir():
        return
    for new_file in sorted(manifest_dir.glob("*.manifest.json.new")):
        try:
            size = new_file.stat().st_size
        except OSError:
            continue
        if size == 0:
            if dry_run:
                _emit(True, f"remove stale upgrade artifact: {new_file.name}")
            else:
                new_file.unlink()
                _already(f"removed stale upgrade artifact: {new_file.name}")
        else:
            if yes:
                if dry_run:
                    _emit(True, f"remove stale upgrade artifact: {new_file.name} (--yes, non-zero content)")
                else:
                    new_file.unlink()
                    _already(f"removed stale upgrade artifact: {new_file.name}")
            else:
                click.echo(
                    f"! stray .new file may be an interrupted write or an operator "
                    f"backup: {new_file.name} — leaving in place (pass --yes to auto-remove)",
                    err=True,
                )


def _ensure_manifest(
    repo_root: Path, dry_run: bool, *, agent_type: str = "openclaw",
    agent_version: str | None = None,
) -> bool:
    manifest_dir = repo_root / ".argit" / "manifest"
    bundled = _bundled_manifest_path(agent_type=agent_type, agent_version=agent_version)
    target = manifest_dir / bundled.name
    # Skip if ANY manifest revision is already present. Copying the bundled
    # file alongside an existing different revision would create two manifests
    # in `.argit/manifest/` and break the "exactly one manifest per repo"
    # invariant on the next argit invocation.
    existing = sorted(manifest_dir.glob("*.manifest.json")) if manifest_dir.is_dir() else []
    if existing:
        names = ", ".join(p.name for p in existing)
        if any(p.name == bundled.name for p in existing):
            _already(f"manifest already present: {target.relative_to(repo_root)}")
        else:
            _already(
                f"manifest already present at older revision ({names}); "
                f"bundled is {bundled.name}. Upgrade not auto-applied (see "
                "https://github.com/blinkbitcoin/argit/issues/1)."
            )
        return False
    if dry_run:
        _emit(True, f"copy bundled manifest → {target.relative_to(repo_root)}")
        return True
    manifest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled, target)
    _emit(False, f"copied manifest → {target.relative_to(repo_root)}")
    return True


def _ensure_gitignore(repo_root: Path, dry_run: bool) -> None:
    """Append `.argit/in-progress` and `.argit/lock` to .gitignore (idempotent)."""
    gi = repo_root / ".gitignore"
    needed = [".argit/in-progress", ".argit/lock"]
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    missing = [n for n in needed if n not in existing_lines]
    if not missing:
        _already(".gitignore already lists transient state")
        return
    if dry_run:
        _emit(True, f"append to .gitignore: {missing}")
        return
    sep = "" if existing.endswith("\n") or existing == "" else "\n"
    block = sep + "\n".join(missing) + "\n"
    gi.write_text(existing + block, encoding="utf-8")
    _emit(False, f"appended to .gitignore: {missing}")


def _read_agent_type(bundled_manifest: Path) -> str:
    """Read agent_type from a bundled manifest file without invoking the full parser.

    The full parser chains through load_manifest (strict validation). For the
    LFS-line format step we only need agent_type, so we parse JSON directly —
    keeps setup.py's _ensure_gitattributes decoupled from manifest grammar
    evolution.
    """
    body = json.loads(bundled_manifest.read_text(encoding="utf-8"))
    agent_type = body.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        raise ArgitError(
            f"bundled manifest {bundled_manifest.name} missing or has invalid agent_type",
            "verify the argit installation; manifest templates should ship inside the package",
        )
    return agent_type


def _ensure_gitattributes(repo_root: Path, agent_type: str, dry_run: bool) -> None:
    expected = {
        pattern: line
        for pattern, line in zip(
            path_conventions.lfs_patterns(agent_type),
            path_conventions.lfs_lines(agent_type),
            strict=True,
        )
    }
    ga = repo_root / ".gitattributes"
    existing = ga.read_text(encoding="utf-8") if ga.exists() else ""
    crlf = "\r\n" in existing
    eol = "\r\n" if crlf else "\n"
    exact: set[str] = set()
    differing_lines: dict[str, list[str]] = {pattern: [] for pattern in expected}
    for raw in existing.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.split()[0]
        if pattern in expected:
            if line == expected[pattern]:
                exact.add(pattern)
            else:
                differing_lines[pattern].append(line)
    if len(exact) == len(expected):
        _already(".gitattributes already has the LFS lines")
        return
    for pattern, lines in differing_lines.items():
        if pattern not in exact and lines:
            click.echo(
                f"! .gitattributes has a different filter for `{pattern}` — review manually:\n  {lines}",
                err=True,
            )
    missing_lines = [line for pattern, line in expected.items() if pattern not in exact and not differing_lines[pattern]]
    if not missing_lines:
        return
    if dry_run:
        _emit(True, f"append to .gitattributes: {missing_lines}")
        return
    needs_leading_nl = bool(existing) and not existing.endswith(("\n", "\r\n"))
    block = (eol if needs_leading_nl else "") + eol.join(missing_lines) + eol
    ga.write_text(existing + block, encoding="utf-8")
    _emit(False, "appended LFS line(s) to .gitattributes")


def _ensure_secrets_dir(repo_root: Path, dry_run: bool) -> None:
    secrets = repo_root / "secrets"
    if secrets.is_dir():
        _already(f"secrets/ already exists")
        return
    if dry_run:
        _emit(True, f"mkdir secrets/")
        return
    secrets.mkdir(parents=True, exist_ok=True)
    _emit(False, f"created secrets/")


def _import_it_key(gpg: GpgWrap, repo_root: Path, dry_run: bool, yes: bool) -> bool:
    """Import the bundled IT backup public key + set ownertrust to Full.

    `set_ownertrust` runs UNCONDITIONALLY (idempotent — re-setting to the
    same level is a no-op). If the key was imported via a previous argit
    version that didn't set trust, or via a non-argit channel, the trust
    level stays Unknown and GPG prompts "Use this key anyway? (y/N)" on
    every encrypt — which hangs `pass insert` when there's no tty. Always
    asserting the trust level fixes those hosts on next `argit setup`.
    """
    newly_imported = not gpg.is_key_imported(IT_BACKUP_FPR)
    if dry_run:
        if newly_imported:
            _emit(True, f"import IT backup key (fpr {IT_BACKUP_FPR}, uid '{IT_BACKUP_UID}')")
        else:
            _already(f"IT backup key already imported (fpr {IT_BACKUP_FPR})")
        _emit(True, f"set ownertrust Full (4) on {IT_BACKUP_FPR}")
        return newly_imported
    if newly_imported and not yes:
        click.echo(
            f"Importing IT backup key (fpr {IT_BACKUP_FPR}, uid '{IT_BACKUP_UID}') "
            f"into your GPG keyring. Press Enter to continue, Ctrl-C to abort."
        )
        try:
            click.get_text_stream("stdin").readline()
        except KeyboardInterrupt:
            raise click.exceptions.Abort()
    # Mutation outside the backup repo — guard with the in-progress marker so
    # an interrupted import surfaces on the next run.
    with in_progress_marker(repo_root):
        if newly_imported:
            asc = _bundled_it_key_path()
            gpg.import_key(asc)
        # GPG ownertrust numerics: 4=Full, 5=Ultimate. The IT key is an
        # external vendor key — Full, not Ultimate (Ultimate is for keys the
        # operator controls). The tech-spec's "(5)" for Full inverts GPG's
        # actual convention; we honor the intent (Full, not Ultimate).
        gpg.set_ownertrust(IT_BACKUP_FPR, 4)
    if newly_imported:
        _emit(False, f"imported IT backup key + set ownertrust Full")
    else:
        _already(
            f"IT backup key already imported (fpr {IT_BACKUP_FPR}) — re-asserted ownertrust Full"
        )
    return newly_imported


def _detect_agent_key(gpg: GpgWrap, agent_key: str | None) -> str:
    # Read-only inspection — runs in --dry-run too (pre-flight contract).
    personal = gpg.list_personal_keys(exclude_fpr=IT_BACKUP_FPR)
    if agent_key:
        target = agent_key.replace(" ", "").upper()
        for k in personal:
            if k.fpr.upper().endswith(target):
                return k.fpr
        # Allow specifying a key that includes the IT key (advanced use)
        if target == IT_BACKUP_FPR.upper():
            return IT_BACKUP_FPR
        raise ArgitError(
            f"--agent-key {agent_key} not found in your GPG keyring",
            "list available keys: gpg --list-keys --with-colons | grep ^fpr",
        )
    if len(personal) == 0:
        raise ArgitError(
            "no personal GPG key found",
            "create one: gpg --full-generate-key (RSA 4096, no expiry recommended)",
        )
    if len(personal) > 1:
        bullets = "\n  ".join(
            f"{k.fpr}  ({(k.uids or ['<no uid>'])[0]})" for k in personal
        )
        raise ArgitError(
            f"multiple personal GPG keys found ({len(personal)}); pass --agent-key <fpr>:\n  {bullets}",
            "argit setup --agent-key <fpr>",
        )
    return personal[0].fpr


def _run_pass_init(repo_root: Path, agent_fpr: str, dry_run: bool) -> None:
    """Initialize the repo-local pass store with the agent + IT backup
    recipients. Previously this was a copy-paste hint; argit now runs the
    command directly, now that preflight guarantees `pass`, `gpg`, and
    git identity are all usable.

    Idempotent: if `secrets/.gpg-id` already lists the expected
    recipients, return a no-op status line.
    """
    secrets_dir = repo_root / "secrets"
    gpg_id = secrets_dir / ".gpg-id"
    cmd_display = (
        f"cd secrets && PASSWORD_STORE_DIR=. pass init {agent_fpr} {IT_BACKUP_FPR}"
    )
    if gpg_id.is_file():
        _already(f"pass store already initialized ({gpg_id.relative_to(repo_root)})")
        return
    if dry_run:
        _emit(True, f"run: {cmd_display}")
        return
    env = {**os.environ, "PASSWORD_STORE_DIR": "."}
    try:
        subprocess.run(
            ["pass", "init", agent_fpr, IT_BACKUP_FPR],
            cwd=str(secrets_dir),
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=PASS_INIT_TIMEOUT_SEC,
        )
    except subprocess.CalledProcessError as exc:
        raise ArgitError(
            f"pass init failed (exit {exc.returncode}): "
            f"{(exc.stderr or exc.stdout).strip()}",
            f"run manually to see full output: {cmd_display}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ArgitError(
            f"pass init timed out after {PASS_INIT_TIMEOUT_SEC}s",
            f"run manually: {cmd_display}",
        ) from exc
    _emit(False, f"initialized pass store → {gpg_id.relative_to(repo_root)}")


def _handle_drift(
    repo_root: Path, *, yes: bool, no_upgrade_manifest: bool, dry_run: bool,
    agent_type: str = "openclaw", agent_version: str | None = None,
) -> None:
    """Classify and (conditionally) act on manifest drift.

    Runs BEFORE any load_manifest call (F2): a pre-spec-02 or otherwise
    grammar-incompatible manifest must still be reachable by the upgrade
    path — the classifier is hash-only.

    `agent_version` (if probed) targets the best-fit manifest for the live
    agent — operators on an older agent shouldn't be told to "upgrade" to a
    manifest that targets a newer schema they can't yet use.
    """
    manifest_dir = repo_root / ".argit" / "manifest"
    _cleanup_stale_upgrade_files(manifest_dir, yes=yes, dry_run=dry_run)

    if not manifest_dir.is_dir():
        return
    existing = sorted(manifest_dir.glob("*.manifest.json"))
    if not existing:
        return
    # Pre-Track-A multi-manifest state is a first-touch error at
    # find_manifest_file; respect it here by classifying only the first.
    # Whether _handle_drift should proactively error on multi-manifest
    # (per Copilot's suggestion in PR #5) is a spec question — filed as
    # a follow-up issue.
    repo_manifest_path = existing[0]
    existing_type, _, _ = parse_filename(repo_manifest_path.name)
    if existing_type != agent_type:
        raise ArgitError(
            f"repo manifest is for {existing_type}, but setup requested {agent_type}",
            "use the matching --agent-type or initialize a separate backup repo for this agent",
        )

    drift, matched_rev = _classify_drift(repo_manifest_path)
    bundled = _bundled_manifest_path(agent_type=agent_type, agent_version=agent_version)

    if drift == "clean":
        _already(f"manifest drift: clean ({repo_manifest_path.name})")
        return

    if drift == "operator_modified":
        _already(
            f"manifest drift: operator-modified ({repo_manifest_path.name}). "
            f"Leaving alone. If you intended operator extensions, move them to "
            f"`.manifest.local.json` — see MANIFEST.md §Overlay."
        )
        return

    # stale_bundle
    latest_rev = int(bundled.name.rsplit("-", 1)[-1].split(".", 1)[0])
    if no_upgrade_manifest:
        if dry_run:
            _emit(True, f"drift: stale bundle (rev {matched_rev} → {latest_rev}); "
                        f"--no-upgrade-manifest set, would skip upgrade")
        else:
            _already(
                f"manifest drift: stale bundle (rev {matched_rev} → {latest_rev} available); "
                f"--no-upgrade-manifest set, skipping upgrade."
            )
        return

    if dry_run:
        if yes:
            _emit(True, f"upgrade rev {matched_rev} → {latest_rev} (--yes would auto-accept)")
        else:
            _emit(True, f"drift: stale bundle (rev {matched_rev} → {latest_rev}); "
                        f"would prompt for upgrade (pass --yes to auto-accept in a non-dry-run)")
        return

    if not yes:
        click.echo(
            f"Your {repo_manifest_path.relative_to(repo_root)} matches an older bundled "
            f"revision (rev {matched_rev}). The current bundled revision is {latest_rev}. "
            f"Upgrade? [Y/n] ",
            nl=False,
        )
        try:
            raw = click.get_text_stream("stdin").readline()
        except KeyboardInterrupt:
            raise click.exceptions.Abort()
        # Distinguish EOF ("") from empty-line-with-enter ("\n"). EOF means
        # no TTY (piped invocation / CI without --yes) — do NOT silently
        # auto-accept an upgrade in that case.
        if raw == "":
            raise ArgitError(
                "no answer received on stdin (EOF); will not auto-accept the manifest upgrade",
                "pass --yes to auto-accept, or --no-upgrade-manifest to skip drift prompts",
            )
        answer = raw.strip().lower()
        if answer not in ("", "y", "yes"):
            _already(
                f"leaving {repo_manifest_path.name} at rev {matched_rev}. "
                f"Run with --no-upgrade-manifest to suppress this prompt."
            )
            return

    # Atomic upgrade inside the in-progress marker. Crash-safety invariant:
    # at most one *.manifest.json exists at any point.
    #
    # Sequence (when target.name == repo_manifest_path.name, the common
    # same-revision-rewrite case):
    #   1. Write new bytes to `<name>.new`    (crash → .new cleaned at next setup)
    #   2. os.replace(.new, name)              (atomic)
    #
    # Sequence (when target.name differs, rev-bump case):
    #   1. Write new bytes to `<new-name>.new` (crash → .new cleaned)
    #   2. Unlink old `<old-name>`            (crash → zero manifests, user reruns)
    #   3. os.replace(<new-name>.new, <new-name>)  (atomic, no two-manifest window)
    #
    # The "unlink old BEFORE replace" order eliminates the prior race where
    # a crash between replace + unlink-old left both files on disk,
    # triggering find_manifest_file's multi-manifest error downstream.
    new_path = manifest_dir / (bundled.name + ".new")
    target = manifest_dir / bundled.name
    with in_progress_marker(repo_root):
        new_path.write_bytes(bundled.read_bytes())
        if target.name != repo_manifest_path.name:
            repo_manifest_path.unlink(missing_ok=True)
        os.replace(new_path, target)
    _emit(False, f"upgraded manifest: rev {matched_rev} → {latest_rev} ({target.name})")


def _git_config_has(key: str) -> bool:
    """Return True when git config resolves `key` to a non-empty value at
    any scope (system/global/local). False on missing binary or missing key.
    """
    try:
        cp = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _collect_preflight_failures(repo_root: Path) -> list[tuple[str, str]]:
    """Run every environment-prereq check and collect (problem, remediation)
    pairs. Empty list means everything is ready.

    Single-fail behavior (bail on first missing prereq) is actively
    user-hostile: operator fixes one thing, reruns, hits the next missing
    thing, repeats. Collecting all failures up-front lets the operator
    install/configure everything in one pass.
    """
    problems: list[tuple[str, str]] = []

    def _try(fn) -> None:
        try:
            fn()
        except ArgitError as exc:
            problems.append((exc.diagnosis, exc.remediation))

    _try(require_python)
    _try(require_supported_platform)

    # Full binary set — setup previously only required gpg + git, which left
    # pass / sqlite3 / git-lfs failures for first-backup-time. Check them all
    # here so a fresh host gets one complete list of things to install.
    for binary in ("gpg", "git", "pass", "sqlite3", "git-lfs"):
        _try(lambda b=binary: require_binary(b))

    # git-lfs filter only makes sense to check if git-lfs itself is present;
    # otherwise the filter check's remediation would be "run git lfs install"
    # which conflicts with the "install git-lfs" line we already emitted.
    if shutil.which("git-lfs"):
        _try(check_lfs_filter_configured)

    _try(lambda: require_git_repo(repo_root))

    # git identity — pass init internally runs `git commit`, which aborts
    # when user.email / user.name are unset ("Author identity unknown").
    # Without this preflight check that failure surfaces mid-setup, after
    # argit has already imported keys and created directories. Check
    # up-front and surface alongside everything else missing.
    for key in ("user.email", "user.name"):
        if not _git_config_has(key):
            example = (
                "your.name@example.com" if key == "user.email" else "Your Name"
            )
            problems.append((
                f"git config {key} is not set",
                f'run: git config --global {key} "{example}"',
            ))

    return problems


def _raise_on_preflight_failures(problems: list[tuple[str, str]]) -> None:
    if not problems:
        return
    diagnosis_bullets = "\n  ".join(f"- {p}" for p, _ in problems)
    remediation_bullets = "\n  ".join(f"- {r}" for _, r in problems)
    raise ArgitError(
        f"{len(problems)} preflight check(s) failed:\n  {diagnosis_bullets}",
        f"address each:\n  {remediation_bullets}",
    )


def run_setup(repo_root: Path, *, yes: bool, agent_key: str | None, agent_type: str = "openclaw",
              no_upgrade_manifest: bool = False, dry_run: bool) -> None:
    _raise_on_preflight_failures(_collect_preflight_failures(repo_root))

    # Probe the live agent version once and thread it through so
    # `_bundled_manifest_path` can pick a best-fit manifest for whatever
    # agent version is actually installed. None on every probe failure
    # (binary missing, timeout, garbled output) — `_bundled_manifest_path`
    # then falls back to "latest available", matching pre-probe behavior.
    agent_version = probe_agent_version("openclaw") if agent_type == "openclaw" else None

    # Serialize concurrent `argit setup` invocations so .gitattributes and
    # .gitignore appends don't race. Lock acquisition itself is harmless in
    # dry-run too.
    with acquire_lock(repo_root):
        # Drift classification + upgrade MUST run before any load_manifest
        # call (F2). The classifier is hash-only and reachable even when
        # the existing manifest has an unparseable grammar.
        _handle_drift(repo_root, yes=yes, no_upgrade_manifest=no_upgrade_manifest,
                      agent_type=agent_type,
                      dry_run=dry_run, agent_version=agent_version)
        _ensure_manifest(repo_root, dry_run, agent_type=agent_type, agent_version=agent_version)
        _ensure_gitignore(repo_root, dry_run)
        manifest_agent_type = _read_agent_type(
            _bundled_manifest_path(agent_type=agent_type, agent_version=agent_version)
        )
        _ensure_gitattributes(repo_root, manifest_agent_type, dry_run)
        _ensure_secrets_dir(repo_root, dry_run)

        gpg = GpgWrap()
        _import_it_key(gpg, repo_root, dry_run, yes)
        agent_fpr = _detect_agent_key(gpg, agent_key)
        _emit(False, f"using agent GPG key: {agent_fpr}")
        _run_pass_init(repo_root, agent_fpr, dry_run)

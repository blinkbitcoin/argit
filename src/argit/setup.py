"""argit setup — one-time bootstrapping inside an existing git-init'd repo.

Idempotent. See tech-spec-01-mvp.md §Task 9.1 for the canonical sequence.
"""

from __future__ import annotations

import json
import shutil
import sys
from importlib import resources
from pathlib import Path

import click

from . import path_conventions
from .errors import ArgitError
from .gpgwrap import GpgWrap
from .hashing import canonical_hash
from .shared import (
    IT_BACKUP_FPR,
    IT_BACKUP_UID,
    acquire_lock,
    in_progress_marker,
    require_binary,
    require_git_repo,
    require_python,
    require_supported_platform,
)


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


def _bundled_manifest_path(agent_type: str = "openclaw", agent_version: str = "2026.4.14") -> Path:
    """Return the highest-revision bundled manifest for a given
    (agent_type, agent_version). Older revisions remain shipped for audit
    trail and as the hash catalog QS4 (issue #1) will consume.
    """
    pkg = resources.files("argit.manifest_templates")
    prefix = f"{agent_type}-{agent_version}-"
    suffix = ".manifest.json"
    candidates: list[tuple[int, Path]] = []
    for entry in pkg.iterdir():
        name = entry.name
        if name.startswith(prefix) and name.endswith(suffix):
            rev_str = name[len(prefix):-len(suffix)]
            try:
                rev = int(rev_str)
            except ValueError:
                continue
            candidates.append((rev, Path(str(entry))))
    if not candidates:
        raise ArgitError(
            f"no bundled manifest found for {agent_type}-{agent_version}",
            "verify the argit installation; manifest templates should ship inside the package",
        )
    return max(candidates)[1]


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
    pkg = resources.files("argit.manifest_templates")
    candidate = pkg.joinpath("hashes.json")
    try:
        body = json.loads(Path(str(candidate)).read_text(encoding="utf-8"))
    except (FileNotFoundError, AttributeError):
        return {}
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
    from .manifest import parse_filename  # local import — avoids cycle

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


def _ensure_manifest(repo_root: Path, dry_run: bool) -> bool:
    manifest_dir = repo_root / ".argit" / "manifest"
    bundled = _bundled_manifest_path()
    target = manifest_dir / bundled.name
    # Skip if ANY openclaw manifest revision is already present — repos pinned
    # to an older revision keep that pin until QS4 (issue #1) ships a proper
    # upgrade flow. Copying the bundled file alongside an existing different
    # revision would create two manifests in `.argit/manifest/` and break the
    # "exactly one manifest per repo" invariant on the next argit invocation.
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
    lfs_line = path_conventions.LFS_LINE_TEMPLATE.format(agent_type=agent_type)
    lfs_pattern = path_conventions.LFS_PATTERN_TEMPLATE.format(agent_type=agent_type)
    ga = repo_root / ".gitattributes"
    existing = ga.read_text(encoding="utf-8") if ga.exists() else ""
    crlf = "\r\n" in existing
    eol = "\r\n" if crlf else "\n"
    has_pattern_line = False
    has_exact = False
    differing_lines: list[str] = []
    for raw in existing.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.split()[0] == lfs_pattern:
            has_pattern_line = True
            if line == lfs_line:
                has_exact = True
            else:
                differing_lines.append(line)
    if has_exact:
        _already(".gitattributes already has the LFS line")
        return
    if has_pattern_line and not has_exact:
        click.echo(
            f"! .gitattributes has a different filter for `{lfs_pattern}` — review manually:\n  {differing_lines}",
            err=True,
        )
        return
    if dry_run:
        _emit(True, f"append to .gitattributes: {lfs_line}")
        return
    needs_leading_nl = bool(existing) and not existing.endswith(("\n", "\r\n"))
    block = (eol if needs_leading_nl else "") + lfs_line + eol
    ga.write_text(existing + block, encoding="utf-8")
    _emit(False, f"appended LFS line to .gitattributes")


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
    if gpg.is_key_imported(IT_BACKUP_FPR):
        _already(f"IT backup key already imported (fpr {IT_BACKUP_FPR})")
        return False
    asc = _bundled_it_key_path()
    if dry_run:
        _emit(True, f"import IT backup key (fpr {IT_BACKUP_FPR}, uid '{IT_BACKUP_UID}')")
        _emit(True, f"set ownertrust Full (4) on {IT_BACKUP_FPR}")
        return True
    if not yes:
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
        gpg.import_key(asc)
        # GPG ownertrust numerics: 4=Full, 5=Ultimate. The IT key is an
        # external vendor key — Full, not Ultimate (Ultimate is for keys the
        # operator controls). The tech-spec's "(5)" for Full inverts GPG's
        # actual convention; we honor the intent (Full, not Ultimate).
        gpg.set_ownertrust(IT_BACKUP_FPR, 4)
    _emit(False, f"imported IT backup key + set ownertrust Full")
    return True


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


def _print_pass_init_hint(repo_root: Path, agent_fpr: str, dry_run: bool) -> None:
    secrets_gpg_id = repo_root / "secrets" / ".gpg-id"
    line = (
        f"Run: cd secrets && PASSWORD_STORE_DIR=. pass init {agent_fpr} {IT_BACKUP_FPR}"
    )
    if secrets_gpg_id.is_file():
        # Already initialized — re-print for reference so the operator can
        # always recover the exact command.
        _already(f"secrets/.gpg-id present; pass-init command (for reference): {line}")
        return
    if dry_run:
        _emit(True, f"print: {line}")
        return
    click.echo(line)


def _handle_drift(
    repo_root: Path, *, yes: bool, no_upgrade_manifest: bool, dry_run: bool,
) -> None:
    """Classify and (conditionally) act on manifest drift.

    Runs BEFORE any load_manifest call (F2): a pre-spec-02 or otherwise
    grammar-incompatible manifest must still be reachable by the upgrade
    path — the classifier is hash-only.
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
    repo_manifest_path = existing[0]

    drift, matched_rev = _classify_drift(repo_manifest_path)
    bundled = _bundled_manifest_path()

    if drift == "clean":
        _already(f"manifest drift: clean ({repo_manifest_path.name})")
        return

    if drift == "operator_modified":
        click.echo(
            f"= manifest drift: operator-modified ({repo_manifest_path.name}). "
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
            click.echo(
                f"= manifest drift: stale bundle (rev {matched_rev} → {latest_rev} available); "
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
            answer = click.get_text_stream("stdin").readline().strip().lower()
        except KeyboardInterrupt:
            raise click.exceptions.Abort()
        if answer not in ("", "y", "yes"):
            click.echo(
                f"= leaving {repo_manifest_path.name} at rev {matched_rev}. "
                f"Run with --no-upgrade-manifest to suppress this prompt."
            )
            return

    # Atomic upgrade inside the in-progress marker.
    new_path = repo_manifest_path.with_suffix(".json.new")
    target = manifest_dir / bundled.name
    with in_progress_marker(repo_root):
        # Write the new bundled body to <file>.new, then atomically replace.
        new_path.write_bytes(bundled.read_bytes())
        # If the bundled filename differs from the repo's (revision bump),
        # remove the old file after writing the new — os.replace is the
        # atomic swap for same-name; for rename we do write-then-unlink-old.
        import os
        if target.name == repo_manifest_path.name:
            os.replace(new_path, target)
        else:
            os.replace(new_path, target)
            repo_manifest_path.unlink(missing_ok=True)
    _emit(False, f"upgraded manifest: rev {matched_rev} → {latest_rev} ({target.name})")


def run_setup(repo_root: Path, *, yes: bool, agent_key: str | None,
              no_upgrade_manifest: bool = False, dry_run: bool) -> None:
    require_python()
    require_supported_platform()
    for b in ("gpg", "git"):
        require_binary(b)
    require_git_repo(repo_root)

    # Serialize concurrent `argit setup` invocations so .gitattributes and
    # .gitignore appends don't race. Lock acquisition itself is harmless in
    # dry-run too.
    with acquire_lock(repo_root):
        # Drift classification + upgrade MUST run before any load_manifest
        # call (F2). The classifier is hash-only and reachable even when
        # the existing manifest has an unparseable grammar.
        _handle_drift(repo_root, yes=yes, no_upgrade_manifest=no_upgrade_manifest, dry_run=dry_run)
        _ensure_manifest(repo_root, dry_run)
        _ensure_gitignore(repo_root, dry_run)
        agent_type = _read_agent_type(_bundled_manifest_path())
        _ensure_gitattributes(repo_root, agent_type, dry_run)
        _ensure_secrets_dir(repo_root, dry_run)

        gpg = GpgWrap()
        _import_it_key(gpg, repo_root, dry_run, yes)
        agent_fpr = _detect_agent_key(gpg, agent_key)
        _emit(False, f"using agent GPG key: {agent_fpr}")
        _print_pass_init_hint(repo_root, agent_fpr, dry_run)

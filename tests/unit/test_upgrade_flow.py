"""Unit tests for Track A upgrade flow: _cleanup_stale_upgrade_files +
_handle_drift invoked via run_setup.

ACs exercised:
- AC-A6 — --no-upgrade-manifest skips prompt, reports drift
- AC-A7 — operator-modified manifest preserved byte-identical
- AC-A15 — flag precedence: --yes + --no-upgrade-manifest → no upgrade
- AC-A16 — stale .new cleanup: zero-byte unconditional, non-zero requires --yes
- AC-A17 — dry-run + --no-upgrade-manifest prints "would skip"
- AC-A23 — dry-run alone prints "would prompt"
- AC-A24 — dry-run + --yes prints "--yes would auto-accept"

run_setup's gpg/pass integration is outside this test's scope; a helper
context manager patches out everything after the drift-decision so we can
focus on the upgrade flow.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from argit.cli import _cli
from argit.errors import ArgitError
from argit.gpgwrap import GpgKey
from argit.hashing import canonical_hash
from argit.setup import _cleanup_stale_upgrade_files


# ---------- AC-A16 — stale .new cleanup ----------

def test_a16_zero_byte_new_file_unconditionally_removed(tmp_path, capsys):
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    stray = mdir / "openclaw-2026.4.14-5.manifest.json.new"
    stray.touch()  # zero bytes
    _cleanup_stale_upgrade_files(mdir, yes=False, dry_run=False)
    assert not stray.exists()
    assert "removed stale upgrade artifact" in capsys.readouterr().out


def test_a16_nonzero_new_file_requires_yes(tmp_path, capsys):
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    stray = mdir / "openclaw-2026.4.14-5.manifest.json.new"
    stray.write_text('{"possible_operator_backup":true}')

    _cleanup_stale_upgrade_files(mdir, yes=False, dry_run=False)
    assert stray.exists()
    err = capsys.readouterr().err
    assert "leaving in place" in err
    assert "--yes to auto-remove" in err

    _cleanup_stale_upgrade_files(mdir, yes=True, dry_run=False)
    assert not stray.exists()


def test_a16_silent_no_op_when_no_new_files(tmp_path, capsys):
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    (mdir / "openclaw-2026.4.14-5.manifest.json").write_text("{}")
    _cleanup_stale_upgrade_files(mdir, yes=False, dry_run=False)
    out = capsys.readouterr()
    assert "stale" not in out.out
    assert "stray" not in out.err


def test_a16_missing_manifest_dir_is_noop(tmp_path):
    _cleanup_stale_upgrade_files(tmp_path / "does-not-exist", yes=False, dry_run=False)


# ---------- end-to-end run_setup upgrade-decision helpers ----------

@contextlib.contextmanager
def _stub_setup_env(catalog: dict[str, str]):
    """Patch out preflight + post-drift steps in run_setup so we only
    exercise the _handle_drift branch."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("argit.setup.require_python", lambda: None))
        stack.enter_context(patch("argit.setup.require_supported_platform", lambda: None))
        stack.enter_context(patch("argit.setup.require_git_repo", lambda r: None))
        stack.enter_context(patch("argit.setup.require_binary", lambda name: None))
        stack.enter_context(patch("argit.setup._ensure_manifest", lambda *a, **k: False))
        stack.enter_context(patch("argit.setup._ensure_gitignore", lambda *a, **k: None))
        stack.enter_context(patch("argit.setup._ensure_gitattributes", lambda *a, **k: None))
        stack.enter_context(patch("argit.setup._ensure_secrets_dir", lambda *a, **k: None))
        stack.enter_context(patch("argit.setup._import_it_key", lambda *a, **k: False))
        stack.enter_context(patch("argit.setup._detect_agent_key", lambda gpg, ak: "A" * 40))
        stack.enter_context(patch("argit.setup._print_pass_init_hint", lambda *a, **k: None))
        stack.enter_context(patch("argit.setup._load_hash_catalog", return_value=catalog))
        mock_gpg = stack.enter_context(patch("argit.setup.GpgWrap"))
        inst = mock_gpg.return_value
        inst.is_key_imported.return_value = True
        inst.list_personal_keys.return_value = [GpgKey(fpr="A" * 40, uids=["op"])]
        yield


def _fake_catalog(repo_rev: int) -> tuple[Path, dict, int]:
    """Return a bundled-manifest path at `repo_rev`, a full catalog covering
    all shipped revisions, and the latest shipped rev number."""
    from argit.setup import _all_bundled_manifest_paths
    from argit.manifest import parse_filename
    catalog: dict[str, str] = {}
    repo_path = None
    revs = []
    for bp in _all_bundled_manifest_paths():
        catalog[bp.name] = canonical_hash(bp)
        _, _, r = parse_filename(bp.name)
        revs.append(r)
        if r == repo_rev:
            repo_path = bp
    if repo_path is None:
        raise RuntimeError(f"no bundled manifest at rev {repo_rev}")
    return repo_path, catalog, max(revs)


def _stage_repo_in_cwd(cwd: str, manifest_src: Path) -> Path:
    """Stage an argit repo inside the runner's isolated-filesystem cwd.

    Click's group callback overwrites ctx.obj["repo_root"] with Path.cwd(),
    so the repo must BE the cwd (not an obj= override)."""
    repo = Path(cwd)
    (repo / ".git").mkdir()
    mdir = repo / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    (mdir / manifest_src.name).write_bytes(manifest_src.read_bytes())
    return repo


# ---------- dry-run UX (AC-A17, AC-A23, AC-A24) ----------

def test_a23_dry_run_alone_on_stale_bundle_prints_would_prompt(tmp_path):
    repo_manifest_src, catalog, latest_rev = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        _stage_repo_in_cwd(cwd, repo_manifest_src)
        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "stale bundle" in result.output
    assert f"rev 1 → {latest_rev}" in result.output
    assert "would prompt" in result.output


def test_a24_dry_run_plus_yes_prints_yes_would_auto_accept(tmp_path):
    repo_manifest_src, catalog, latest_rev = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        _stage_repo_in_cwd(cwd, repo_manifest_src)
        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup", "--dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert f"upgrade rev 1 → {latest_rev}" in result.output
    assert "--yes would auto-accept" in result.output


def test_a17_dry_run_plus_no_upgrade_manifest_prints_would_skip(tmp_path):
    repo_manifest_src, catalog, _ = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        _stage_repo_in_cwd(cwd, repo_manifest_src)
        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup", "--dry-run", "--no-upgrade-manifest"])
    assert result.exit_code == 0, result.output
    assert "would skip upgrade" in result.output


# ---------- non-dry-run flag behavior (AC-A6, AC-A15) ----------

def test_a6_no_upgrade_manifest_reports_but_skips(tmp_path):
    repo_manifest_src, catalog, _ = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = _stage_repo_in_cwd(cwd, repo_manifest_src)
        repo_mfile = repo / ".argit" / "manifest" / repo_manifest_src.name
        original_bytes = repo_mfile.read_bytes()
        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup", "--no-upgrade-manifest"])
        assert result.exit_code == 0, result.output
        assert "stale bundle" in result.output
        assert repo_mfile.read_bytes() == original_bytes


def test_a15_flag_precedence_no_upgrade_beats_yes(tmp_path):
    repo_manifest_src, catalog, _ = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = _stage_repo_in_cwd(cwd, repo_manifest_src)
        repo_mfile = repo / ".argit" / "manifest" / repo_manifest_src.name
        original_bytes = repo_mfile.read_bytes()
        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup", "--yes", "--no-upgrade-manifest"])
        assert result.exit_code == 0, result.output
        assert "stale bundle" in result.output
        assert repo_mfile.read_bytes() == original_bytes


# ---------- AC-A7 — operator-modified preservation ----------

def test_a7_operator_modified_preserved_byte_identical(tmp_path):
    from argit.setup import _bundled_manifest_path
    bundled = _bundled_manifest_path()
    body = json.loads(bundled.read_text())
    body["exclude"].append("operator-specific-thing/")
    catalog = {bundled.name: canonical_hash(bundled)}
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = Path(cwd)
        (repo / ".git").mkdir()
        mdir = repo / ".argit" / "manifest"
        mdir.mkdir(parents=True)
        repo_mfile = mdir / bundled.name
        repo_mfile.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
        original_bytes = repo_mfile.read_bytes()
        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup"])
        assert result.exit_code == 0, result.output
        assert "operator-modified" in result.output
        assert ".manifest.local.json" in result.output
        assert repo_mfile.read_bytes() == original_bytes


# ---------- AC-A4 — actual upgrade write path (both interactive + --yes) ----------

def test_a4_upgrade_interactive_y_writes_new_bundled(tmp_path):
    """Interactive Y acceptance actually writes bundled bytes, removes .new,
    and the on-disk hash matches the catalog's latest-rev entry."""
    repo_manifest_src, catalog, latest_rev = _fake_catalog(repo_rev=1)
    from argit.setup import _bundled_manifest_path
    bundled = _bundled_manifest_path()
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = _stage_repo_in_cwd(cwd, repo_manifest_src)
        mdir = repo / ".argit" / "manifest"
        old_mfile = mdir / repo_manifest_src.name
        new_mfile = mdir / bundled.name

        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup"], input="y\n")
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert f"upgraded manifest: rev 1 → {latest_rev}" in result.output
        assert new_mfile.is_file()
        # If bundled name differs from the original (rev bump), the old file
        # should be gone.
        if old_mfile.name != new_mfile.name:
            assert not old_mfile.exists()
        # Bytes on disk match the bundled (canonical-hash-matches-catalog).
        assert canonical_hash(new_mfile) == catalog[bundled.name]
        # No stale .new siblings.
        assert list(mdir.glob("*.manifest.json.new")) == []


def test_a4_upgrade_yes_flag_writes_new_bundled(tmp_path):
    """--yes (non-dry-run) auto-accepts without stdin and writes bundled."""
    repo_manifest_src, catalog, latest_rev = _fake_catalog(repo_rev=1)
    from argit.setup import _bundled_manifest_path
    bundled = _bundled_manifest_path()
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = _stage_repo_in_cwd(cwd, repo_manifest_src)
        mdir = repo / ".argit" / "manifest"
        new_mfile = mdir / bundled.name

        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup", "--yes"])
        assert result.exit_code == 0, result.output
        assert f"upgraded manifest: rev 1 → {latest_rev}" in result.output
        assert new_mfile.is_file()
        assert canonical_hash(new_mfile) == catalog[bundled.name]
        assert list(mdir.glob("*.manifest.json.new")) == []


def test_upgrade_declined_preserves_manifest(tmp_path):
    """Answering 'n' at the prompt leaves the old manifest untouched."""
    repo_manifest_src, catalog, _ = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = _stage_repo_in_cwd(cwd, repo_manifest_src)
        mdir = repo / ".argit" / "manifest"
        old_mfile = mdir / repo_manifest_src.name
        original_bytes = old_mfile.read_bytes()

        with _stub_setup_env(catalog):
            result = runner.invoke(_cli, ["setup"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "leaving" in result.output
        assert old_mfile.read_bytes() == original_bytes


# ---------- AC-A4 addendum — EOF-on-stdin does NOT silently upgrade ----------

def test_eof_on_stdin_without_yes_raises_rather_than_auto_accept(tmp_path):
    """Non-TTY invocation without --yes must abort, not silently upgrade.

    The readline() returns "" on EOF; without an explicit guard the code
    would treat EOF as the Y-default and proceed with the upgrade — a
    safety hazard for piped / CI invocations."""
    repo_manifest_src, catalog, _ = _fake_catalog(repo_rev=1)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        repo = _stage_repo_in_cwd(cwd, repo_manifest_src)
        mdir = repo / ".argit" / "manifest"
        old_mfile = mdir / repo_manifest_src.name
        original_bytes = old_mfile.read_bytes()

        with _stub_setup_env(catalog):
            # input="" → stdin EOF immediately, no newline.
            result = runner.invoke(_cli, ["setup"], input="")
        assert result.exit_code != 0, result.output
        # CliRunner doesn't route through cli._entrypoint's ArgitError
        # handler, so the diagnosis lives on result.exception.
        assert isinstance(result.exception, ArgitError)
        assert "EOF" in result.exception.diagnosis or "no answer" in result.exception.diagnosis
        assert "--yes" in result.exception.remediation
        # Manifest preserved — upgrade did NOT fire.
        assert old_mfile.read_bytes() == original_bytes

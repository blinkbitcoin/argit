"""argit setup --dry-run action-list generation, mocked GPG."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from argit.cli import _cli
from argit.gpgwrap import GpgKey


def _init_git(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


@patch("argit.setup.GpgWrap")
def test_setup_dryrun_pristine(mock_gpg, tmp_path):
    repo = _init_git(tmp_path)
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = False
    inst.list_personal_keys.return_value = [GpgKey(fpr="ABCDEF1234567890", uids=["Operator <op@x>"])]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        # Re-create .git inside the isolated fs
        Path(cwd, ".git").mkdir()
        result = runner.invoke(_cli, ["setup", "--dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "would: copy bundled manifest" in result.output
    assert "would: append to .gitignore" in result.output
    assert "would: append to .gitattributes" in result.output
    assert "would: mkdir secrets/" in result.output
    assert "would: import IT backup key" in result.output
    assert "would: print: Run: cd secrets" in result.output


@patch("argit.setup.GpgWrap")
def test_setup_dryrun_partially_set_up(mock_gpg, tmp_path):
    """When manifest already present + IT key already imported, only deltas appear."""
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = [GpgKey(fpr="ABCDEF1234567890")]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        # Pre-seed manifest dir + bundled manifest (latest revision)
        from argit.setup import _bundled_manifest_path
        mdir = Path(cwd, ".argit", "manifest")
        mdir.mkdir(parents=True)
        bundled = _bundled_manifest_path()
        (mdir / bundled.name).write_text(bundled.read_text())
        # Pre-seed gitignore + gitattributes + secrets dir
        Path(cwd, ".gitignore").write_text(".argit/in-progress\n.argit/lock\n")
        Path(cwd, ".gitattributes").write_text("openclaw/blob/** filter=lfs diff=lfs merge=lfs -text\n")
        Path(cwd, "secrets").mkdir()

        result = runner.invoke(_cli, ["setup", "--dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "manifest already present" in result.output
    assert ".gitignore already lists transient state" in result.output
    assert "already has the LFS line" in result.output
    assert "secrets/ already exists" in result.output
    assert "IT backup key already imported" in result.output


@patch("argit.setup.GpgWrap")
def test_setup_dryrun_zero_personal_keys(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = []

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        result = runner.invoke(_cli, ["setup", "--dry-run", "--yes"])
    # ArgitError propagates as result.exception (CliRunner doesn't run our wrapper).
    assert result.exit_code != 0
    assert result.exception is not None
    assert "no personal GPG key" in str(result.exception)


@patch("argit.setup.GpgWrap")
def test_setup_dryrun_multi_key_no_agent_key(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = [
        GpgKey(fpr="A" * 40, uids=["A <a@x>"]),
        GpgKey(fpr="B" * 40, uids=["B <b@x>"]),
    ]
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        result = runner.invoke(_cli, ["setup", "--dry-run", "--yes"])
    assert result.exit_code != 0
    assert result.exception is not None
    msg = str(result.exception)
    assert "multiple personal GPG keys" in msg
    assert "--agent-key" in msg


@patch("argit.setup.GpgWrap")
def test_setup_dryrun_multi_key_with_agent_key(mock_gpg, tmp_path):
    fpr_b = "B" * 40
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = [
        GpgKey(fpr="A" * 40, uids=["A <a@x>"]),
        GpgKey(fpr=fpr_b, uids=["B <b@x>"]),
    ]
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        result = runner.invoke(_cli, ["setup", "--dry-run", "--yes", "--agent-key", fpr_b])
    assert result.exit_code == 0, result.output
    assert fpr_b in result.output

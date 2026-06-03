"""argit doctor — pristine / partial / fully-set-up states."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from argit.cli import _cli
from argit.gpgwrap import GpgKey
from argit.setup import _bundled_manifest_path
from argit.shared import IT_BACKUP_FPR

BUNDLED = _bundled_manifest_path()
OPENCLAW_LFS_LINES = (
    "openclaw/blob/** filter=lfs diff=lfs merge=lfs -text\n"
    "openclaw/sqlite/** filter=lfs diff=lfs merge=lfs -text\n"
)


def _stub_subprocess_run():
    """Stub subprocess.run for doctor's git/lfs/push probes."""
    from subprocess import CompletedProcess

    def fake_run(args, **kwargs):
        # First arg is the command list
        cmd = args
        if cmd[:3] == ["git", "config", "--get"] and cmd[3] == "filter.lfs.clean":
            return CompletedProcess(args=cmd, returncode=0, stdout="git-lfs clean -- %f\n", stderr="")
        if cmd[:3] == ["git", "remote", "get-url"]:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    return fake_run


@patch("argit.doctor.GpgWrap")
@patch("argit.doctor.require_binary", lambda name: None)
@patch("argit.shared.require_binary", lambda name: None)
@patch("argit.shared.check_lfs_filter_configured", lambda: None)
@patch("argit.doctor.check_lfs_filter_configured", lambda: None)
def test_doctor_pristine_exits_nonzero(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = False
    inst.list_personal_keys.return_value = []

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        with patch("subprocess.run", side_effect=_stub_subprocess_run()):
            result = runner.invoke(_cli, ["doctor"])
    assert result.exit_code == 1
    assert "✗" in result.output
    assert "manifest" in result.output


@patch("argit.doctor.GpgWrap")
@patch("argit.doctor.require_binary", lambda name: None)
@patch("argit.doctor.check_lfs_filter_configured", lambda: None)
def test_doctor_fully_set_up_exits_zero(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = [GpgKey(fpr="A" * 40, uids=["op"])]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        # manifest
        mdir = Path(cwd, ".argit", "manifest")
        mdir.mkdir(parents=True)
        (mdir / BUNDLED.name).write_text(BUNDLED.read_text())
        # gitattributes with LFS line
        Path(cwd, ".gitattributes").write_text(OPENCLAW_LFS_LINES)
        # secrets/.gpg-id with agent + backup recipients
        secrets = Path(cwd, "secrets")
        secrets.mkdir()
        (secrets / ".gpg-id").write_text(
            f"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n{IT_BACKUP_FPR}\n"
        )

        with patch("subprocess.run", side_effect=_stub_subprocess_run()):
            result = runner.invoke(_cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "✓" in result.output


@patch("argit.doctor.GpgWrap")
@patch("argit.doctor.require_binary", lambda name: None)
@patch("argit.doctor.check_lfs_filter_configured", lambda: None)
def test_doctor_single_recipient_fails_with_actionable_remediation(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = [GpgKey(fpr="A" * 40)]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        mdir = Path(cwd, ".argit", "manifest")
        mdir.mkdir(parents=True)
        (mdir / BUNDLED.name).write_text(BUNDLED.read_text())
        Path(cwd, ".gitattributes").write_text(OPENCLAW_LFS_LINES)
        secrets = Path(cwd, "secrets")
        secrets.mkdir()
        (secrets / ".gpg-id").write_text("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")

        with patch("subprocess.run", side_effect=_stub_subprocess_run()):
            result = runner.invoke(_cli, ["doctor"])
    assert result.exit_code == 1
    assert "expected >=2 recipients" in result.output
    assert "pass init <agent-fpr> <backup-fpr>" in result.output


@patch("argit.doctor.GpgWrap")
@patch("argit.doctor.require_binary", lambda name: None)
@patch("argit.doctor.check_lfs_filter_configured", lambda: None)
def test_doctor_dual_recipient_with_foreign_key_no_blink_required(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    foreign = "B" * 40
    inst.is_key_imported.side_effect = lambda fpr: fpr in {"A" * 40, foreign}
    inst.list_personal_keys.return_value = [GpgKey(fpr="A" * 40)]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        mdir = Path(cwd, ".argit", "manifest")
        mdir.mkdir(parents=True)
        (mdir / BUNDLED.name).write_text(BUNDLED.read_text())
        Path(cwd, ".gitattributes").write_text(OPENCLAW_LFS_LINES)
        secrets = Path(cwd, "secrets")
        secrets.mkdir()
        (secrets / ".gpg-id").write_text(f"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n{foreign}\n")

        with patch("subprocess.run", side_effect=_stub_subprocess_run()):
            result = runner.invoke(_cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "IT backup key imported" not in result.output


@patch("argit.doctor.GpgWrap")
@patch("argit.doctor.require_binary", lambda name: None)
@patch("argit.doctor.check_lfs_filter_configured", lambda: None)
def test_doctor_lifecycle_preview(mock_gpg, tmp_path):
    inst = mock_gpg.return_value
    inst.is_key_imported.return_value = True
    inst.list_personal_keys.return_value = [GpgKey(fpr="A" * 40)]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        Path(cwd, ".git").mkdir()
        mdir = Path(cwd, ".argit", "manifest")
        mdir.mkdir(parents=True)
        (mdir / BUNDLED.name).write_text(BUNDLED.read_text())
        Path(cwd, ".gitattributes").write_text(OPENCLAW_LFS_LINES)
        secrets = Path(cwd, "secrets")
        secrets.mkdir()
        (secrets / ".gpg-id").write_text(f"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n{IT_BACKUP_FPR}\n")

        with patch("subprocess.run", side_effect=_stub_subprocess_run()):
            result = runner.invoke(_cli, ["doctor"])
    assert "Lifecycle commands argit would execute" in result.output
    assert "detect_running" in result.output
    assert "stop" in result.output
    assert "start" in result.output

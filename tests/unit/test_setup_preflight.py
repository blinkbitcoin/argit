"""argit setup preflight — collect-all-missing + specific fixes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argit.errors import ArgitError
from argit.setup import (
    _collect_preflight_failures,
    _import_it_key,
    _raise_on_preflight_failures,
    _run_pass_init,
)
from argit.shared import IT_BACKUP_FPR


# ---------- preflight collection ----------

def test_preflight_collects_all_failures_not_first(tmp_path):
    """Multiple missing prereqs must surface as a single consolidated error."""
    # Simulate: missing sqlite3, missing git-lfs, missing git.user.email.
    def _fake_which(name: str) -> str | None:
        return None if name in ("sqlite3", "git-lfs") else f"/usr/bin/{name}"

    def _fake_git_config(key: str) -> bool:
        return key == "user.name"  # user.email missing

    with patch("argit.setup.shutil.which", side_effect=_fake_which):
        with patch("argit.setup._git_config_has", side_effect=_fake_git_config):
            with patch("argit.setup.require_git_repo", lambda r: None):
                with patch("argit.setup.check_lfs_filter_configured", lambda: None):
                    problems = _collect_preflight_failures(tmp_path)

    # Every missing item should appear. No bail-on-first behavior.
    diagnoses = [p for p, _ in problems]
    assert any("sqlite3" in d for d in diagnoses)
    assert any("git-lfs" in d for d in diagnoses)
    assert any("user.email" in d for d in diagnoses)
    # user.name was present → should NOT appear.
    assert not any("user.name" in d for d in diagnoses)


def test_raise_on_preflight_renders_bullet_list():
    problems = [
        ("sqlite3: command not found", "Install: brew install sqlite / apt install sqlite3"),
        ("git config user.email is not set", 'run: git config --global user.email "..."'),
    ]
    with pytest.raises(ArgitError) as exc:
        _raise_on_preflight_failures(problems)
    msg = str(exc.value)
    assert "2 preflight check(s) failed" in msg
    assert "sqlite3: command not found" in msg
    assert "user.email" in msg
    # Both remediations must appear.
    assert "brew install sqlite" in msg
    assert "git config --global user.email" in msg


def test_raise_on_preflight_empty_is_noop():
    _raise_on_preflight_failures([])  # must not raise


def test_preflight_includes_git_identity_both_keys(tmp_path):
    """Both user.email AND user.name must be checked independently."""
    with patch("argit.setup.shutil.which", return_value="/usr/bin/fake"):
        with patch("argit.setup._git_config_has", return_value=False):
            with patch("argit.setup.require_git_repo", lambda r: None):
                with patch("argit.setup.check_lfs_filter_configured", lambda: None):
                    problems = _collect_preflight_failures(tmp_path)
    diagnoses = [p for p, _ in problems]
    assert any("user.email" in d for d in diagnoses)
    assert any("user.name" in d for d in diagnoses)


# ---------- _import_it_key: ownertrust re-asserted on existing key ----------

def test_import_it_key_sets_ownertrust_even_when_already_imported(tmp_path):
    """Operator-visible bug: a host where the IT key was imported by a
    previous argit (no trust set) or by a non-argit channel saw GPG prompt
    'Use this key anyway?' on every encrypt → `pass insert` hung. Fix:
    always call set_ownertrust, even on the already-imported path."""
    gpg = MagicMock()
    gpg.is_key_imported.return_value = True  # key already present
    _import_it_key(gpg, tmp_path, dry_run=False, yes=True)
    gpg.set_ownertrust.assert_called_once_with(IT_BACKUP_FPR, 4)
    # import_key must NOT be called — the key is already there.
    gpg.import_key.assert_not_called()


def test_import_it_key_newly_imported_also_sets_ownertrust(tmp_path):
    gpg = MagicMock()
    gpg.is_key_imported.return_value = False
    with patch("argit.setup._bundled_it_key_path", return_value=Path("/fake/key.asc")):
        _import_it_key(gpg, tmp_path, dry_run=False, yes=True)
    gpg.import_key.assert_called_once()
    gpg.set_ownertrust.assert_called_once_with(IT_BACKUP_FPR, 4)


def test_import_it_key_dry_run_emits_ownertrust_line_even_when_imported(tmp_path, capsys):
    gpg = MagicMock()
    gpg.is_key_imported.return_value = True
    _import_it_key(gpg, tmp_path, dry_run=True, yes=True)
    captured = capsys.readouterr().out
    assert "IT backup key already imported" in captured
    assert "would: set ownertrust Full" in captured
    # Dry-run must not actually mutate GPG state.
    gpg.set_ownertrust.assert_not_called()
    gpg.import_key.assert_not_called()


# ---------- _run_pass_init: auto-run instead of print ----------

def test_run_pass_init_executes_pass_init(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    agent_fpr = "A" * 40

    calls = []

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    with patch("argit.setup.subprocess.run", side_effect=_fake_run):
        _run_pass_init(tmp_path, agent_fpr, dry_run=False)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ["pass", "init"]
    assert agent_fpr in args
    assert IT_BACKUP_FPR in args
    assert kwargs["cwd"] == str(secrets)
    assert kwargs["env"]["PASSWORD_STORE_DIR"] == "."


def test_run_pass_init_idempotent_when_gpg_id_exists(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / ".gpg-id").write_text("A" * 40 + "\n")

    with patch("argit.setup.subprocess.run") as fake_run:
        _run_pass_init(tmp_path, "A" * 40, dry_run=False)

    fake_run.assert_not_called()


def test_run_pass_init_dry_run_does_not_execute(tmp_path, capsys):
    secrets = tmp_path / "secrets"
    secrets.mkdir()

    with patch("argit.setup.subprocess.run") as fake_run:
        _run_pass_init(tmp_path, "A" * 40, dry_run=True)

    fake_run.assert_not_called()
    assert "would: run: cd secrets" in capsys.readouterr().out


def test_run_pass_init_surfaces_failure_with_manual_command(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()

    def _fail(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=128, cmd=args, stderr="fatal: empty ident name\n",
        )

    with patch("argit.setup.subprocess.run", side_effect=_fail):
        with pytest.raises(ArgitError) as exc:
            _run_pass_init(tmp_path, "A" * 40, dry_run=False)
    msg = str(exc.value)
    assert "pass init failed" in msg
    assert "empty ident name" in msg
    # Remediation gives the manual command so operator can see full output.
    assert "cd secrets" in msg
    assert "PASSWORD_STORE_DIR=." in msg

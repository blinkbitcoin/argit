"""check_no_partial_state auto-clears when the working tree is clean.

Operator scenario: `argit backup` hangs on a pinentry prompt, operator
Ctrl-C's. `pass insert` blocked before writing anything, so `git status`
is clean. Old behavior: next `argit backup` refuses until the operator
manually deletes the marker. New behavior: marker auto-cleared with a
notice; the hard error only fires when the tree is actually dirty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from argit.errors import ArgitError
from argit.shared import IN_PROGRESS, check_no_partial_state


def _seed_marker(repo_root: Path) -> Path:
    marker = repo_root / IN_PROGRESS
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("12345\n")
    return marker


def _mock_git_status(stdout: str, returncode: int = 0):
    def _fn(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )
    return _fn


def test_marker_autoclears_when_git_status_is_clean(tmp_path):
    marker = _seed_marker(tmp_path)
    with patch("argit.shared.subprocess.run", _mock_git_status("")):
        check_no_partial_state(tmp_path, "backup")  # must not raise
    assert not marker.exists(), "clean tree → marker should be auto-removed"


def test_marker_blocks_when_git_status_is_dirty(tmp_path):
    marker = _seed_marker(tmp_path)
    with patch("argit.shared.subprocess.run", _mock_git_status(" M secrets/foo.gpg\n")):
        with pytest.raises(ArgitError) as exc:
            check_no_partial_state(tmp_path, "backup")
    msg = str(exc.value)
    assert "uncommitted changes" in msg
    assert str(marker) in msg
    # Marker must NOT be cleared when tree is dirty — operator inspects first.
    assert marker.exists()


def test_marker_blocks_when_untracked_files_present(tmp_path):
    """`?? secrets/foo.gpg` (untracked) is a real sign of partial state —
    `pass insert` wrote a new encrypted file before the interrupt."""
    marker = _seed_marker(tmp_path)
    with patch("argit.shared.subprocess.run", _mock_git_status("?? secrets/foo.gpg\n")):
        with pytest.raises(ArgitError):
            check_no_partial_state(tmp_path, "backup")
    assert marker.exists()


def test_no_marker_noop(tmp_path):
    # No marker at all → silent return, no git invocation.
    with patch("argit.shared.subprocess.run") as fake_run:
        check_no_partial_state(tmp_path, "backup")
    fake_run.assert_not_called()


def test_marker_kept_when_git_status_fails(tmp_path):
    """If `git status` errors (corrupt repo, not a git dir, etc.), we
    cannot verify cleanness — keep the marker and surface the error, so
    the operator isn't silently bypassed."""
    marker = _seed_marker(tmp_path)
    with patch("argit.shared.subprocess.run", _mock_git_status("", returncode=128)):
        with pytest.raises(ArgitError):
            check_no_partial_state(tmp_path, "backup")
    assert marker.exists()


def test_marker_kept_when_git_missing(tmp_path):
    marker = _seed_marker(tmp_path)

    def _raise_fnf(*_args, **_kwargs):
        raise FileNotFoundError("git")

    with patch("argit.shared.subprocess.run", side_effect=_raise_fnf):
        with pytest.raises(ArgitError):
            check_no_partial_state(tmp_path, "backup")
    assert marker.exists()

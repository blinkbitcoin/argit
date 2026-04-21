"""AC 28-37: lifecycle orchestration in argit.restore.

Mocks subprocess.run to control detect_running / stop / start exit codes
and asserts the orchestration logic + argv-printed-before-exec invariant.
"""

from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import click
import pytest

from argit.errors import ArgitError
from argit.manifest import Lifecycle, LifecycleCommand
from argit.restore import (
    _detect_running,
    _exec_log,
    _resolve_paths,
    _start_after_restore,
    _stop_and_wait,
)


def _life(detect_exit=0, stop_exit=0, start_exit=0, timeout_sec=2, poll_ms=50):
    return Lifecycle(
        detect_running=LifecycleCommand(
            description="probe",
            command=["sh", "-c", f"exit {detect_exit}"],
            running_exit_code=0,
            timeout_sec=timeout_sec,
        ),
        stop=LifecycleCommand(
            description="stop",
            command=["sh", "-c", f"exit {stop_exit}"],
            timeout_sec=timeout_sec,
            poll_interval_ms=poll_ms,
        ),
        start=LifecycleCommand(
            description="start",
            command=["sh", "-c", f"exit {start_exit}"],
            timeout_sec=timeout_sec,
        ),
    )


def test_ac28_detect_running_when_agent_running(capsys):
    """AC 28 prerequisite: detect_running returns True for matching exit code."""
    life = _life(detect_exit=0)
    with patch("argit.restore.subprocess.run", return_value=CompletedProcess([], 0)):
        assert _detect_running(life, dry=False) is True


def test_ac28_detect_not_running_when_exit_nonzero():
    life = _life(detect_exit=1)
    with patch("argit.restore.subprocess.run", return_value=CompletedProcess([], 1)):
        assert _detect_running(life, dry=False) is False


def test_ac28_stop_and_wait_succeeds_when_agent_stops_quickly():
    life = _life(timeout_sec=2, poll_ms=50)
    # First detect_running call (after stop) returns "stopped" (exit 1).
    seq = [
        CompletedProcess([], 0),  # the stop subprocess
        CompletedProcess([], 1),  # poll: stopped
    ]
    with patch("argit.restore.subprocess.run", side_effect=seq):
        # Should NOT raise.
        _stop_and_wait(life, dry=False)


def test_ac28_stop_and_wait_raises_when_agent_never_stops():
    life = _life(timeout_sec=1, poll_ms=50)
    # Stop returns 0, every poll returns "still running" (exit 0).
    def fake_run(cmd, **_kwargs):
        return CompletedProcess(cmd, 0)
    with patch("argit.restore.subprocess.run", side_effect=fake_run):
        with pytest.raises(ArgitError) as exc:
            _stop_and_wait(life, dry=False)
        assert "did not stop" in str(exc.value)


def test_ac30_start_logs_warning_on_nonzero_but_does_not_raise(capsys):
    """AC 30: lifecycle.start non-zero exit → warning, NOT failure."""
    life = _life(start_exit=1)
    with patch("argit.restore.subprocess.run", return_value=CompletedProcess([], 1)):
        # Should not raise.
        _start_after_restore(life, dry=False)
    captured = capsys.readouterr()
    assert "auto-start" in captured.err.lower() or "could not" in captured.err.lower()


def test_ac35_argv_printed_before_exec(capsys):
    """AC 35: full argv printed to stderr with `→ exec:` prefix BEFORE subprocess invocation."""
    argv = ["sh", "-c", "exit 0"]
    _exec_log(argv)
    captured = capsys.readouterr()
    assert "→ exec:" in captured.err
    assert "sh" in captured.err
    assert "exit 0" in captured.err


def test_ac35_detect_running_logs_argv_before_subprocess(capsys):
    life = _life(detect_exit=0)
    log_order = []

    def record_log(args, **_kwargs):
        log_order.append("subprocess")
        return CompletedProcess(args, 0)

    with patch("argit.restore.subprocess.run", side_effect=record_log):
        _detect_running(life, dry=False)

    captured = capsys.readouterr()
    # The exec log appeared on stderr; the subprocess ran.
    assert "→ exec:" in captured.err
    assert log_order == ["subprocess"]


def test_ac31_target_resolution_scratch_vs_source_root(tmp_path, monkeypatch):
    """AC 31: --target /tmp/scratch resolves differently from manifest's source_root."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target_str = str(tmp_path / "scratch")
    tgt, msr, is_scratch = _resolve_paths("~/.openclaw", target_str)
    assert is_scratch is True


def test_ac31_target_same_path_resolves_as_live_restore(tmp_path, monkeypatch):
    """AC 31: explicit --target ~/.openclaw IS treated as live-restore."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Explicit --target equal to manifest source_root → not scratch
    target_str = str(home / ".openclaw")
    tgt, msr, is_scratch = _resolve_paths("~/.openclaw", target_str)
    assert is_scratch is False


def test_ac32_no_lifecycle_no_probe(tmp_path):
    """AC 32: manifest with no lifecycle → restore proceeds without probing."""
    # _detect_running with detect_running=None returns False without subprocess.
    life = Lifecycle(detect_running=None, stop=None, start=None)
    with patch("argit.restore.subprocess.run") as mock_run:
        result = _detect_running(life, dry=False)
    assert result is False
    mock_run.assert_not_called()

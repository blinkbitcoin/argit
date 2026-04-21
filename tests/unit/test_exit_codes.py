"""Exit-code surfacing through the cli wrapper: EXIT_LOCK_CONTENTION (4),
EXIT_PARTIAL_STATE (5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.shared import (
    EXIT_LOCK_CONTENTION,
    EXIT_PARTIAL_STATE,
    check_no_partial_state,
    in_progress_path,
)


def test_partial_state_exit_code_attached(tmp_path):
    """check_no_partial_state raises ArgitError with .exit_code = EXIT_PARTIAL_STATE."""
    marker = in_progress_path(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("12345\n")

    with pytest.raises(ArgitError) as exc:
        check_no_partial_state(tmp_path, "backup")
    assert getattr(exc.value, "exit_code", None) == EXIT_PARTIAL_STATE


def test_lock_contention_exit_code_attached(tmp_path):
    """acquire_lock raises ArgitError with .exit_code = EXIT_LOCK_CONTENTION
    when contended past the timeout."""
    import os
    import fcntl
    from argit.shared import LOCK_FILE, acquire_lock

    # Create the lock file and hold an exclusive flock from a separate fd.
    lock_path = tmp_path / LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        # Patch the LOCK_TIMEOUT_SEC down so the test runs fast
        from argit import shared
        original_timeout = shared.LOCK_TIMEOUT_SEC
        shared.LOCK_TIMEOUT_SEC = 1
        try:
            with pytest.raises(ArgitError) as exc:
                with acquire_lock(tmp_path):
                    pass
            assert getattr(exc.value, "exit_code", None) == EXIT_LOCK_CONTENTION
        finally:
            shared.LOCK_TIMEOUT_SEC = original_timeout
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_argit_error_default_exit_code_is_attribute_lookup():
    """Wrapper uses getattr(exc, 'exit_code', EXIT_FIRST_TOUCH) — verify the
    default-exit-code path works for plain ArgitError without exit_code."""
    err = ArgitError("test", "do nothing")
    assert getattr(err, "exit_code", 1) == 1  # default

"""Multi-key GPG detection logic in argit.setup."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argit.errors import ArgitError
from argit.gpgwrap import GpgKey
from argit.setup import _detect_agent_key


def _gpg_with_personal(keys: list[GpgKey]):
    g = MagicMock()
    g.list_personal_keys.return_value = keys
    return g


def test_zero_personal_keys_raises():
    g = _gpg_with_personal([])
    with pytest.raises(ArgitError) as exc:
        _detect_agent_key(g, agent_key=None)
    assert "no personal GPG key" in str(exc.value)
    assert "gpg --full-generate-key" in str(exc.value)


def test_one_personal_key_uses_it():
    g = _gpg_with_personal([GpgKey(fpr="A" * 40, uids=["op"])])
    assert _detect_agent_key(g, agent_key=None) == "A" * 40


def test_two_personal_keys_no_agent_key_raises():
    g = _gpg_with_personal([
        GpgKey(fpr="A" * 40, uids=["A <a@x>"]),
        GpgKey(fpr="B" * 40, uids=["B <b@x>"]),
    ])
    with pytest.raises(ArgitError) as exc:
        _detect_agent_key(g, agent_key=None)
    assert "multiple personal GPG keys" in str(exc.value)
    assert "--agent-key" in str(exc.value)


def test_agent_key_explicit():
    g = _gpg_with_personal([
        GpgKey(fpr="A" * 40, uids=["A"]),
        GpgKey(fpr="B" * 40, uids=["B"]),
    ])
    assert _detect_agent_key(g, agent_key="B" * 40) == "B" * 40


def test_agent_key_short_suffix_match():
    fpr_a = "A" * 40
    g = _gpg_with_personal([GpgKey(fpr=fpr_a, uids=["op"])])
    assert _detect_agent_key(g, agent_key="A" * 16) == fpr_a


def test_agent_key_not_found_raises():
    g = _gpg_with_personal([GpgKey(fpr="A" * 40)])
    with pytest.raises(ArgitError) as exc:
        _detect_agent_key(g, agent_key="DEADBEEFDEADBEEF")
    assert "not found" in str(exc.value)

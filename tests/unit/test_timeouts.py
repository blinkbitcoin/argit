"""AC 24: subprocess timeout → first-touch error with pinentry hint."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from argit.errors import ArgitError
from argit.gpgwrap import GpgWrap
from argit.passwrap import PASS_TIMEOUT_SEC, PassWrap


def _raise_timeout(cmd, timeout, **_kwargs):
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)


def test_pass_insert_timeout_surfaces_pinentry_hint(tmp_path):
    pw = PassWrap(tmp_path / "secrets")
    # The idempotency check runs `has()` first. Patch at the subprocess level
    # so both `has` and the actual insert subprocess raise TimeoutExpired.
    with patch("argit.passwrap.subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(ArgitError) as exc:
            pw.insert("argit/openclaw/foo", "secret-value")
    msg = str(exc.value)
    assert "timed out" in msg
    assert "pinentry" in msg.lower()


def test_pass_show_timeout_surfaces_pinentry_hint(tmp_path):
    pw = PassWrap(tmp_path / "secrets")
    with patch("argit.passwrap.subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(ArgitError) as exc:
            pw.show("argit/openclaw/foo")
    msg = str(exc.value)
    assert "timed out" in msg
    assert "pinentry" in msg.lower()


def test_pass_has_timeout_surfaces_pinentry_hint(tmp_path):
    pw = PassWrap(tmp_path / "secrets")
    with patch("argit.passwrap.subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(ArgitError) as exc:
            pw.has("argit/openclaw/foo")
    assert "pinentry" in str(exc.value).lower()


def test_pass_env_forces_noninteractive_trust_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PASSWORD_STORE_GPG_OPTS", "--batch")
    env = PassWrap(tmp_path / "secrets")._env()
    assert env["PASSWORD_STORE_DIR"] == str(tmp_path / "secrets")
    assert env["PASSWORD_STORE_GPG_OPTS"] == "--batch --trust-model always"


def test_gpg_list_keys_timeout():
    g = GpgWrap()
    with patch("argit.gpgwrap.subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(ArgitError) as exc:
            g.list_keys()
    assert "timed out" in str(exc.value)
    assert "pinentry" in str(exc.value).lower()


def test_pass_cmd_not_found_without_pass_binary(tmp_path):
    """AC 25: missing pass binary → first-touch error with install lines."""
    pw = PassWrap(tmp_path / "secrets")
    with patch("argit.passwrap.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ArgitError) as exc:
            pw.has("argit/openclaw/foo")
    msg = str(exc.value)
    assert "pass: command not found" in msg
    assert "brew install pass" in msg
    assert "apt install pass" in msg

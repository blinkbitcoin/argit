"""AC 21 + AC 22: verify phase helpers detect placeholder / LFS-pointer leaks."""

from __future__ import annotations

from pathlib import Path

from argit.restore import LFS_POINTER_PREFIX, _file_starts_with
from argit.sanitize import find_placeholders


def test_find_placeholders_in_nested_config():
    """AC 21 prerequisite: find_placeholders reports every leftover ${pass:}."""
    body = {
        "gateway": {"auth": {"token": "real-secret"}},
        "channels": {
            "slack": {"botToken": "${pass:argit/openclaw/channels/slack-bot-token}"},
        },
        "env": "${pass:argit/openclaw/env}",
    }
    leftovers = find_placeholders(body)
    paths = sorted(p for _, p in leftovers)
    assert paths == [
        "argit/openclaw/channels/slack-bot-token",
        "argit/openclaw/env",
    ]


def test_find_placeholders_clean_config_has_none():
    body = {"gateway": {"auth": {"token": "reinjected-secret"}}}
    assert find_placeholders(body) == []


def test_lfs_pointer_detection_positive(tmp_path):
    """AC 22 prerequisite: _file_starts_with recognizes an LFS pointer."""
    pointer = tmp_path / "pointer.bin"
    pointer.write_bytes(
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:abcdef\n"
        b"size 1234\n"
    )
    assert _file_starts_with(pointer, LFS_POINTER_PREFIX)


def test_lfs_pointer_detection_negative_on_real_content(tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    assert not _file_starts_with(real, LFS_POINTER_PREFIX)


def test_lfs_pointer_detection_missing_file_is_false(tmp_path):
    assert not _file_starts_with(tmp_path / "does-not-exist", LFS_POINTER_PREFIX)

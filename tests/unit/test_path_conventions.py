"""Unit tests for path_conventions — forward + inverse derivation + glob grammar."""

from __future__ import annotations

import pytest

from argit.errors import ArgitError
from argit.path_conventions import (
    default_mode,
    derive_item_pass,
    derive_item_target,
    derive_pass,
    derive_sanitize_target,
    invert_item_target,
    targets_overlap,
    validate_glob_source,
)


# ---------- default_mode ----------

@pytest.mark.parametrize(
    "kind,expected",
    [
        ("secret", "0600"),
        ("data", "0644"),
        ("sqlite", "0600"),
        ("blob", "0644"),
    ],
)
def test_default_mode(kind, expected):
    assert default_mode(kind) == expected


def test_default_mode_unknown_kind():
    with pytest.raises(ArgitError):
        default_mode("bogus")


# ---------- derive_sanitize_target ----------

def test_derive_sanitize_target():
    assert derive_sanitize_target("openclaw", "openclaw.json") == "openclaw/config/openclaw.json"
    assert derive_sanitize_target("hermes", "hermes.conf.json") == "hermes/config/hermes.conf.json"


# ---------- derive_pass (sanitize rule) ----------

@pytest.mark.parametrize(
    "file,dotted,expected",
    [
        # AC-D7: camelCase→kebab, dots→slashes, file-stem included even when
        # it equals agent_type (root-config path doubling).
        ("openclaw.json", ".channels.telegram.botToken",
         "argit/openclaw/openclaw/channels/telegram/bot-token"),
        # `.` splits to `/` per-segment; no dash-joining across dot-segments.
        ("openclaw.json", ".gateway.auth.token",
         "argit/openclaw/openclaw/gateway/auth/token"),
        ("exec-approvals.json", ".socket.token",
         "argit/openclaw/exec-approvals/socket/token"),
        # Leading dot is stripped (same result with or without)
        ("openclaw.json", "env", "argit/openclaw/openclaw/env"),
        ("openclaw.json", ".env", "argit/openclaw/openclaw/env"),
        # camelCase with trailing uppercase run — only split on lowercase→uppercase boundary
        ("openclaw.json", ".apiURL", "argit/openclaw/openclaw/api-url"),
    ],
)
def test_derive_pass(file, dotted, expected):
    assert derive_pass("openclaw", file, dotted) == expected


# ---------- derive_item_pass ----------

@pytest.mark.parametrize(
    "source,expected",
    [
        ("identity/device.json", "argit/openclaw/identity/device"),
        ("agents/main/agent/auth-profiles.json", "argit/openclaw/agents/main/agent/auth-profiles"),
        # No .json extension — source taken verbatim.
        ("identity/device", "argit/openclaw/identity/device"),
    ],
)
def test_derive_item_pass(source, expected):
    assert derive_item_pass("openclaw", source) == expected


# ---------- derive_item_target ----------

@pytest.mark.parametrize(
    "kind,source,expected",
    [
        # AC-D4: sqlite separators preserved (no dash flattening).
        ("sqlite", "memory/main.sqlite", "openclaw/sqlite/memory/main.sqlite"),
        ("sqlite", "a/b/c.sqlite", "openclaw/sqlite/a/b/c.sqlite"),
        ("data", "agents/main/agent/auth-state.json", "openclaw/data/agents/main/agent/auth-state.json"),
        # Trailing `/` preserved on dir sources.
        ("data", "telegram/", "openclaw/data/telegram/"),
        # AC-D5: blob target.
        ("blob", "media/browser/", "openclaw/blob/media/browser/"),
    ],
)
def test_derive_item_target(kind, source, expected):
    assert derive_item_target("openclaw", kind, source) == expected


def test_derive_item_target_rejects_secret():
    with pytest.raises(ArgitError):
        derive_item_target("openclaw", "secret", "identity/device.json")


def test_derive_item_target_rejects_unknown_kind():
    with pytest.raises(ArgitError):
        derive_item_target("openclaw", "bogus", "x")


# ---------- invert_item_target ----------

@pytest.mark.parametrize(
    "kind,source",
    [
        ("data", "agents/main/agent/auth-state.json"),
        ("sqlite", "memory/main.sqlite"),
        ("sqlite", "a/b/c.sqlite"),
        ("blob", "media/browser/"),
        ("data", "telegram/"),
    ],
)
def test_invert_item_target_roundtrip(kind, source):
    target = derive_item_target("openclaw", kind, source)
    assert invert_item_target("openclaw", kind, target) == source


def test_invert_item_target_rejects_glob_pattern():
    with pytest.raises(ArgitError) as exc:
        invert_item_target("openclaw", "data", "openclaw/data/agents/*/x.json")
    assert "glob pattern" in str(exc.value).lower()


def test_invert_item_target_rejects_wrong_prefix():
    with pytest.raises(ArgitError):
        invert_item_target("openclaw", "data", "hermes/data/x")


# ---------- validate_glob_source ----------

@pytest.mark.parametrize(
    "source",
    [
        "agents/main/agent/auth-profiles.json",        # no glob — accepted
        "agents/*/agent/auth-profiles.json",           # trailing-before-filename `*`
        "agents/*/*/data.json",                         # multi-`*`
        "*/auth-state.json",                            # leading `*`
        "agents/*/",                                    # trailing `*` component (dir glob)
    ],
)
def test_validate_glob_source_accepts(source):
    validate_glob_source(source)  # raises on failure


@pytest.mark.parametrize(
    "source,fragment",
    [
        ("agents/**/foo.json", "**"),
        ("foo*.json", "glob is only valid as a whole path component"),
        ("*.json", "glob is only valid as a whole path component"),
        ("foo-*-bar.json", "glob is only valid as a whole path component"),
    ],
)
def test_validate_glob_source_rejects(source, fragment):
    with pytest.raises(ArgitError) as exc:
        validate_glob_source(source)
    assert fragment in str(exc.value)


# ---------- targets_overlap ----------

@pytest.mark.parametrize(
    "a,b,expected",
    [
        # AC-D19 worked examples.
        ("agents/*/foo.json", "agents/main/foo.json", True),   # star matches literal at same position
        ("agents/*/foo.json", "other/main/foo.json", False),   # different literal at position 0
        ("agents/*", "agents/*/foo.json", False),              # length mismatch
        # Additional cases for symmetry & completeness.
        ("a/b", "a/b", True),                                   # identical literals
        ("a/*", "*/b", True),                                   # star/star at different positions
        ("a/b", "a/c", False),                                  # different literals, same length
        ("a/*/c", "a/x/c", True),
        ("a/*/c", "a/x/d", False),
    ],
)
def test_targets_overlap(a, b, expected):
    assert targets_overlap(a, b) is expected
    assert targets_overlap(b, a) is expected  # symmetric

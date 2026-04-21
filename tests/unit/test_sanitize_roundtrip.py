"""Sanitize / re-inject round-trip — leaf, nested, subtree, mixed."""

from __future__ import annotations

import json

import pytest

from argit.errors import ArgitError
from argit.manifest import SanitizeRule
from argit.sanitize import find_placeholders, reinject, sanitize


def lookup_factory(extracted: dict[str, str]):
    return lambda p: extracted[p]


def test_leaf_only_roundtrip():
    config = {"gateway": {"auth": {"token": "abc123"}}}
    rules = [SanitizeRule(path=".gateway.auth.token", pass_path="argit/openclaw/gateway/auth-token")]
    sanitized, extracted, skipped = sanitize(config, rules)
    assert sanitized["gateway"]["auth"]["token"] == "${pass:argit/openclaw/gateway/auth-token}"
    assert extracted == {"argit/openclaw/gateway/auth-token": "abc123"}
    assert skipped == []
    restored = reinject(sanitized, lookup_factory(extracted))
    assert restored == config


def test_nested_dotted_paths():
    config = {
        "channels": {
            "telegram": {"botToken": "tg-x"},
            "slack": {"botToken": "sl-x", "appToken": "sl-app"},
        }
    }
    rules = [
        SanitizeRule(".channels.telegram.botToken", "p/tg"),
        SanitizeRule(".channels.slack.botToken", "p/sl-bot"),
        SanitizeRule(".channels.slack.appToken", "p/sl-app"),
    ]
    sanitized, extracted, _ = sanitize(config, rules)
    assert extracted == {"p/tg": "tg-x", "p/sl-bot": "sl-x", "p/sl-app": "sl-app"}
    assert reinject(sanitized, lookup_factory(extracted)) == config


def test_subtree_roundtrip():
    config = {"env": {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"}}
    rules = [SanitizeRule(".env", "p/env", subtree=True)]
    sanitized, extracted, _ = sanitize(config, rules)
    assert sanitized["env"] == "${pass:p/env}"
    # Subtree value is JSON-encoded, sorted-keys, no whitespace.
    parsed = json.loads(extracted["p/env"])
    assert parsed == {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"}
    restored = reinject(sanitized, lookup_factory(extracted))
    assert restored == config


def test_mixed_leaf_and_subtree():
    config = {
        "gateway": {"auth": {"token": "tok"}},
        "env": {"K": "v1", "K2": "v2"},
    }
    rules = [
        SanitizeRule(".gateway.auth.token", "p/tok"),
        SanitizeRule(".env", "p/env", subtree=True),
    ]
    sanitized, extracted, _ = sanitize(config, rules)
    restored = reinject(sanitized, lookup_factory(extracted))
    assert restored == config


def test_wildcard_path_rejected():
    config = {"profiles": {"a": {"token": "x"}}}
    rules = [SanitizeRule(".profiles.*.token", "p/x")]
    with pytest.raises(ArgitError) as exc:
        sanitize(config, rules)
    assert "wildcard" in str(exc.value).lower()


def test_missing_path_returned_as_skipped():
    """Production-hardening: a real-world config that omits an optional
    subsystem (no Telegram → no `.channels.telegram.botToken`) should not
    fail the backup. Missing paths are returned as `skipped` and the caller
    (backup.py) warns + continues."""
    config = {"gateway": {}}
    rules = [
        SanitizeRule(".gateway.auth.token", "p/tok"),
        SanitizeRule(".gateway.existing", "p/ok"),
    ]
    config2 = {"gateway": {"existing": "value-x"}}
    sanitized, extracted, skipped = sanitize(config2, rules)
    # One rule skipped, one applied
    assert len(skipped) == 1
    assert skipped[0].path == ".gateway.auth.token"
    assert extracted == {"p/ok": "value-x"}


def test_dict_value_without_subtree_raises():
    """Author-typo case still raises — a rule that exists AND resolves to a
    dict without subtree=True is a manifest bug (not a real-world variance)."""
    config = {"env": {"K": "v"}}
    rules = [SanitizeRule(".env", "p/env")]  # missing subtree=True
    with pytest.raises(ArgitError) as exc:
        sanitize(config, rules)
    assert "subtree" in str(exc.value).lower()


def test_find_placeholders_detects_leftovers():
    body = {"a": "${pass:foo}", "b": [1, "${pass:bar}", {"c": "${pass:baz}"}]}
    out = find_placeholders(body)
    paths = sorted(p for _, p in out)
    assert paths == ["bar", "baz", "foo"]


def test_env_subtree_snapshot_fidelity():
    """AC 26: restored .env reflects backup-time state, not live state."""
    config = {"env": {"OPENAI_API_KEY": "sk-1"}}
    rules = [SanitizeRule(".env", "p/env", subtree=True)]
    sanitized, extracted, _ = sanitize(config, rules)
    restored = reinject(sanitized, lookup_factory(extracted))
    assert restored["env"] == {"OPENAI_API_KEY": "sk-1"}
    assert "ANTHROPIC_API_KEY" not in restored["env"]

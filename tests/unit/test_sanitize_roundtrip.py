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


def test_wildcard_expands_to_concrete_rules():
    """Single whole-segment `*` expands at sanitize-time to one rule per
    matched key, each with its own pass_path (wildcard substituted)."""
    config = {
        "channels": {
            "telegram": {
                "accounts": {
                    "default": {"botToken": "tok-d", "allowFrom": "x"},
                    "erbot": {"botToken": "tok-e"},
                }
            }
        }
    }
    rules = [SanitizeRule(
        path=".channels.telegram.accounts.*.botToken",
        pass_path="argit/openclaw/openclaw/channels/telegram/accounts/*/bot-token",
    )]
    sanitized, extracted, skipped = sanitize(config, rules)
    assert skipped == []
    assert extracted == {
        "argit/openclaw/openclaw/channels/telegram/accounts/default/bot-token": "tok-d",
        "argit/openclaw/openclaw/channels/telegram/accounts/erbot/bot-token": "tok-e",
    }
    # Non-secret keys remain visible in the sanitized JSON.
    assert sanitized["channels"]["telegram"]["accounts"]["default"]["allowFrom"] == "x"
    # Round-trip restores the original.
    restored = reinject(sanitized, lookup_factory(extracted))
    assert restored == config


def test_wildcard_zero_matches_skipped():
    """Wildcard against missing prefix OR empty dict at wildcard depth →
    skipped, not raised. Mirrors the missing-fixed-path behavior."""
    rule = SanitizeRule(
        ".channels.telegram.accounts.*.botToken",
        "argit/openclaw/openclaw/channels/telegram/accounts/*/bot-token",
    )
    # Missing prefix entirely.
    _, extracted, skipped = sanitize({"channels": {"slack": {}}}, [rule])
    assert extracted == {}
    assert len(skipped) == 1
    # Empty dict at wildcard depth.
    _, extracted, skipped = sanitize({"channels": {"telegram": {"accounts": {}}}}, [rule])
    assert extracted == {}
    assert len(skipped) == 1


def test_wildcard_prefix_not_dict_raises():
    """If the segment before `*` resolves to a non-dict (author bug), raise
    rather than skip — `accounts: []` is a manifest/config schema violation
    we want surfaced, not silently swallowed."""
    config = {"channels": {"telegram": {"accounts": []}}}
    rule = SanitizeRule(
        ".channels.telegram.accounts.*.botToken",
        "argit/openclaw/openclaw/channels/telegram/accounts/*/bot-token",
    )
    with pytest.raises(ArgitError) as exc:
        sanitize(config, [rule])
    assert "object" in str(exc.value).lower()


def test_wildcard_with_subtree():
    """`*` at the leaf with subtree=true: each match becomes its own subtree
    pass entry, JSON-serialized."""
    config = {"profiles": {"a": {"k": "v1"}, "b": {"k": "v2"}}}
    rule = SanitizeRule(
        ".profiles.*",
        "argit/openclaw/openclaw/profiles/*",
        subtree=True,
    )
    sanitized, extracted, _ = sanitize(config, [rule])
    assert json.loads(extracted["argit/openclaw/openclaw/profiles/a"]) == {"k": "v1"}
    assert json.loads(extracted["argit/openclaw/openclaw/profiles/b"]) == {"k": "v2"}
    restored = reinject(sanitized, lookup_factory(extracted))
    assert restored == config


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

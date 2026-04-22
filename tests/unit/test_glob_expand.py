"""Track B — expand_globbed_item + enumerate_restore_targets tests.

ACs: AC-B1, AC-B4, AC-B6, AC-B9, AC-B10, AC-B12 + non-globbed passthrough.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.manifest import (
    Item,
    enumerate_restore_targets,
    enumerate_secret_glob_from_pass,
    expand_globbed_item,
)


def _glob_item(kind: str, source: str) -> Item:
    mode = {"secret": "0600", "data": "0644", "sqlite": "0600", "blob": "0644"}[kind]
    return Item(kind=kind, source=source, mode=mode, target=None, pass_path=None, origin="bundled")


# ---------- non-globbed passthrough ----------

def test_non_globbed_returns_item_as_is(tmp_path):
    it = _glob_item("data", "identity/device.json")
    assert expand_globbed_item(it, tmp_path, "openclaw") == [it]


# ---------- AC-B1 — glob grammar acceptance is already tested in path_conventions ----------

# ---------- AC-B4 — zero-match glob ----------

def test_b4_zero_match_returns_empty_list(tmp_path):
    it = _glob_item("data", "agents/*/missing.json")
    (tmp_path / "agents").mkdir()  # no agent subdirs
    assert expand_globbed_item(it, tmp_path, "openclaw") == []


# ---------- AC-B6 — sorted expansion order ----------

def test_b6_multi_agent_sorted_lexicographically(tmp_path):
    (tmp_path / "agents" / "zebra" / "agent").mkdir(parents=True)
    (tmp_path / "agents" / "alpha" / "agent").mkdir(parents=True)
    (tmp_path / "agents" / "zebra" / "agent" / "auth.json").write_text("{}")
    (tmp_path / "agents" / "alpha" / "agent" / "auth.json").write_text("{}")
    it = _glob_item("data", "agents/*/agent/auth.json")

    result = expand_globbed_item(it, tmp_path, "openclaw")
    sources = [r.source for r in result]
    assert sources == ["agents/alpha/agent/auth.json", "agents/zebra/agent/auth.json"]


# ---------- AC-B9 — glob × exclude silently drops ----------

def test_b9_excluded_concrete_paths_dropped(tmp_path):
    (tmp_path / "agents" / "main" / "agent").mkdir(parents=True)
    (tmp_path / "agents" / "erbot" / "agent").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "agent" / "auth.json").write_text("{}")
    (tmp_path / "agents" / "erbot" / "agent" / "auth.json").write_text("{}")
    it = _glob_item("data", "agents/*/agent/auth.json")

    result = expand_globbed_item(
        it, tmp_path, "openclaw", exclude_patterns=["agents/erbot/"],
    )
    sources = [r.source for r in result]
    assert sources == ["agents/main/agent/auth.json"]


# ---------- AC-B10 — multi-* expansion ----------

def test_b10_multi_star_expansion(tmp_path):
    (tmp_path / "agents" / "main" / "team-a").mkdir(parents=True)
    (tmp_path / "agents" / "erbot" / "team-b").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "team-a" / "data.json").write_text("{}")
    (tmp_path / "agents" / "erbot" / "team-b" / "data.json").write_text("{}")
    it = _glob_item("data", "agents/*/*/data.json")

    result = expand_globbed_item(it, tmp_path, "openclaw")
    sources = sorted(r.source for r in result)
    assert sources == [
        "agents/erbot/team-b/data.json",
        "agents/main/team-a/data.json",
    ]


# ---------- target / pass_path derivation on expanded items ----------

def test_expanded_data_item_has_derived_target(tmp_path):
    (tmp_path / "agents" / "main" / "agent").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "agent" / "auth.json").write_text("{}")
    it = _glob_item("data", "agents/*/agent/auth.json")

    result = expand_globbed_item(it, tmp_path, "openclaw")
    assert len(result) == 1
    expanded = result[0]
    assert expanded.source == "agents/main/agent/auth.json"
    assert expanded.target == "openclaw/data/agents/main/agent/auth.json"
    assert expanded.pass_path is None


def test_expanded_secret_item_has_derived_pass_path(tmp_path):
    (tmp_path / "agents" / "main" / "agent").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "agent" / "token.json").write_text("{}")
    it = _glob_item("secret", "agents/*/agent/token.json")

    result = expand_globbed_item(it, tmp_path, "openclaw")
    assert len(result) == 1
    expanded = result[0]
    assert expanded.source == "agents/main/agent/token.json"
    assert expanded.target is None
    assert expanded.pass_path == "argit/openclaw/agents/main/agent/token"


# ---------- origin preserved on expansion ----------

def test_origin_preserved_through_expansion(tmp_path):
    (tmp_path / "plugin" / "main").mkdir(parents=True)
    (tmp_path / "plugin" / "main" / "state.json").write_text("{}")
    it = Item(
        kind="data", source="plugin/*/state.json", mode="0644",
        target=None, pass_path=None, origin="overlay",
    )
    result = expand_globbed_item(it, tmp_path, "openclaw")
    assert len(result) == 1
    assert result[0].origin == "overlay"


# ---------- enumerate_restore_targets (AC-B7) ----------

def test_b7_restore_enumerates_from_repo_filesystem_when_source_root_empty(tmp_path):
    """Fresh-DR: source_root empty, repo has concrete targets. Restore-side
    enumeration walks the repo, not source_root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openclaw" / "data" / "agents" / "main" / "agent").mkdir(parents=True)
    (repo / "openclaw" / "data" / "agents" / "erbot" / "agent").mkdir(parents=True)
    (repo / "openclaw" / "data" / "agents" / "main" / "agent" / "auth.json").write_text("{}")
    (repo / "openclaw" / "data" / "agents" / "erbot" / "agent" / "auth.json").write_text("{}")
    it = _glob_item("data", "agents/*/agent/auth.json")

    result = enumerate_restore_targets(it, repo, "openclaw")
    sources = sorted(r.source for r in result)
    assert sources == [
        "agents/erbot/agent/auth.json",
        "agents/main/agent/auth.json",
    ]
    for r in result:
        assert r.target is not None
        assert "*" not in r.target


def test_enumerate_restore_rejects_secret_glob(tmp_path):
    """Secret globs must enumerate via pass store, not repo filesystem."""
    it = _glob_item("secret", "agents/*/token.json")
    with pytest.raises(ArgitError) as exc:
        enumerate_restore_targets(it, tmp_path, "openclaw")
    assert "secret" in str(exc.value).lower()


# ---------- AC-B12 — invert_item_target precondition via enumerator ----------
# (direct tests live in test_path_conventions.py; here we confirm the
#  enumerator never calls invert with a star by construction.)

def test_enumerate_never_passes_glob_to_inverter(tmp_path):
    """Regression guard for the AC-B12 contract: enumerate_restore_targets
    must enumerate concrete paths first, then invert each — never pass a
    pattern to invert_item_target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # No files → no concrete targets to invert → empty result, no error.
    it = _glob_item("data", "agents/*/agent/auth.json")
    assert enumerate_restore_targets(it, repo, "openclaw") == []


# ---------- enumerate_secret_glob_from_pass ----------

def test_secret_glob_enumerated_from_pass_entries():
    it = _glob_item("secret", "agents/*/agent/auth-profiles.json")
    pass_entries = [
        "argit/openclaw/agents/main/agent/auth-profiles",
        "argit/openclaw/agents/erbot/agent/auth-profiles",
        "argit/openclaw/unrelated/other",  # should not match
        "argit/openclaw/identity/device",  # shorter, should not match
    ]
    result = enumerate_secret_glob_from_pass(it, "openclaw", pass_entries)
    assert len(result) == 2
    sources = sorted(r.source for r in result)
    assert sources == [
        "agents/erbot/agent/auth-profiles.json",
        "agents/main/agent/auth-profiles.json",
    ]
    for r in result:
        assert r.kind == "secret"
        assert r.target is None
        assert r.pass_path is not None
        assert "*" not in r.pass_path


def test_secret_glob_rejects_non_secret_kind():
    it = _glob_item("data", "agents/*/x.json")
    with pytest.raises(ArgitError):
        enumerate_secret_glob_from_pass(it, "openclaw", [])

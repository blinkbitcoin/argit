"""Regression tests for coverage helpers lifted from backup.py to shared.py.

The helpers were renamed public during the lift (`_walk_relative` →
`walk_relative`, etc.). These tests directly exercise the new public API
to lock in the behavior that previously rode on backup-integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from argit.shared import (
    covered_by_items,
    covered_by_sanitize,
    glob_pattern_matches,
    is_under,
    walk_relative,
)


# ---------- walk_relative ----------


def test_walk_relative_yields_files_and_symlinks(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.json").write_text("{}")
    out = sorted(str(p) for p in walk_relative(tmp_path))
    assert out == ["a.json", "sub/b.json"]


def test_walk_relative_yields_empty_directories(tmp_path):
    """Empty dirs surface so a freshly-created `future-plugin/` fires
    the unspecified-files warning even before content lands."""
    (tmp_path / "empty-plugin").mkdir()
    out = list(walk_relative(tmp_path))
    assert Path("empty-plugin") in out


def test_walk_relative_skips_dirs_with_content(tmp_path):
    """Non-empty parent dirs are NOT yielded — only their leaves are.
    Otherwise the unspecified report would double-count."""
    (tmp_path / "with-content").mkdir()
    (tmp_path / "with-content" / "file.json").write_text("{}")
    out = sorted(str(p) for p in walk_relative(tmp_path))
    assert out == ["with-content/file.json"]


def test_walk_relative_returns_nothing_for_missing_root(tmp_path):
    out = list(walk_relative(tmp_path / "does-not-exist"))
    assert out == []


# ---------- is_under ----------


def test_is_under_exact_file_match():
    assert is_under(Path("openclaw.json"), "openclaw.json") is True
    assert is_under(Path("other.json"), "openclaw.json") is False


def test_is_under_dir_prefix_with_trailing_slash():
    assert is_under(Path("logs/today.log"), "logs/") is True
    assert is_under(Path("logs"), "logs/") is True  # the dir entry itself
    assert is_under(Path("logs-archive/x.log"), "logs/") is False  # not under


# ---------- glob_pattern_matches ----------


def test_glob_single_component_matches():
    assert glob_pattern_matches("agents/main/agent/auth-state.json", "agents/*/agent/auth-state.json")
    assert glob_pattern_matches("agents/erbot/agent/auth-state.json", "agents/*/agent/auth-state.json")


def test_glob_does_not_cross_path_separator():
    """`*` matches a single path segment, never `/`."""
    assert not glob_pattern_matches("agents/main/sub/agent/auth-state.json", "agents/*/agent/auth-state.json")


def test_glob_dir_pattern_with_trailing_slash():
    """`agents/*/` covers the matched dir AND everything under it."""
    assert glob_pattern_matches("agents/main", "agents/*/")
    assert glob_pattern_matches("agents/main/anything/here.txt", "agents/*/")


# ---------- covered_by_items / covered_by_sanitize ----------


@dataclass
class _Item:
    """Synthetic Item — only the attributes the helper reads."""
    source: str
    is_globbed: bool


@dataclass
class _SanitizeFile:
    file: str


def test_covered_by_items_handles_globbed_and_literal():
    items = [
        _Item(source="agents/*/agent/auth-state.json", is_globbed=True),
        _Item(source="literal.json", is_globbed=False),
    ]
    assert covered_by_items(Path("agents/main/agent/auth-state.json"), items)
    assert covered_by_items(Path("literal.json"), items)
    assert not covered_by_items(Path("uncovered.json"), items)


def test_covered_by_items_dir_prefix_source():
    items = [_Item(source="logs/", is_globbed=False)]
    assert covered_by_items(Path("logs/today.log"), items)
    assert not covered_by_items(Path("metrics/today.log"), items)


def test_covered_by_sanitize_exact_file_match():
    sf = [_SanitizeFile(file="openclaw.json")]
    assert covered_by_sanitize(Path("openclaw.json"), sf)
    assert not covered_by_sanitize(Path("openclaw.json.bak"), sf)
    assert not covered_by_sanitize(Path("dir/openclaw.json"), sf)

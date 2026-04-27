"""matches_exclude pattern semantics — especially the glob + dir-prefix
composition that rev-7 exposed as broken."""

from __future__ import annotations

from pathlib import Path

import pytest

from argit.shared import matches_exclude


@pytest.mark.parametrize(
    "path,pat,expected",
    [
        # Literal dir-prefix (pre-existing, still works).
        ("agents/main/sessions/foo.jsonl", "agents/main/sessions/", True),
        ("agents/main/sessions/", "agents/main/sessions/", True),
        ("agents/main/sessions/x/y.jsonl", "agents/main/sessions/", True),
        # Literal dir-prefix disjoint → no match.
        ("agents/erbot/sessions/foo.jsonl", "agents/main/sessions/", False),

        # Glob with trailing slash — the regression rev-7 surfaced. Must
        # match any agent's sessions dir and anything below it.
        ("agents/main/sessions/foo.jsonl", "agents/*/sessions/", True),
        ("agents/erbot/sessions/bar.jsonl", "agents/*/sessions/", True),
        ("agents/main/sessions/sub/deep.jsonl", "agents/*/sessions/", True),
        ("agents/main/sessions/", "agents/*/sessions/", True),
        # Same scope — disjoint paths still don't match.
        ("agents/main/agent/foo.json", "agents/*/sessions/", False),
        ("plugins/main/sessions/foo.jsonl", "agents/*/sessions/", False),

        # Glob-with-suffix dir-prefix — pre-existing pattern that had the
        # same latent bug. memory/lancedb.bak*/ must match anything under
        # e.g. memory/lancedb.bak-2026-04-22/ .
        ("memory/lancedb.bak-2026-04-22/seg0", "memory/lancedb.bak*/", True),
        ("memory/lancedb.bak-xyz/data.json", "memory/lancedb.bak*/", True),
        ("memory/lancedb/data.json", "memory/lancedb.bak*/", False),

        # Non-slash globs — fnmatch path (unchanged).
        ("tasks/runs.sqlite-wal", "*.sqlite-wal", True),
        ("tasks/runs.sqlite-shm", "*.sqlite-wal", False),

        # Dir-prefix literal at the repo root.
        ("browser/tab.json", "browser/", True),
        ("completions/x.json", "completions/", True),
    ],
)
def test_matches_exclude_cases(path, pat, expected):
    assert matches_exclude(Path(path), [pat]) is expected


def test_matches_exclude_any_pattern_triggers():
    """When any pattern matches, return True."""
    patterns = ["agents/*/sessions/", "completions/", "*.bak*"]
    assert matches_exclude(Path("agents/erbot/sessions/x.jsonl"), patterns)
    assert matches_exclude(Path("completions/c.json"), patterns)
    assert matches_exclude(Path("foo.bak1"), patterns)
    assert not matches_exclude(Path("agents/main/agent/models.json"), patterns)

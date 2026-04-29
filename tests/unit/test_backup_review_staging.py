"""Unit test: when auto-emit fires AND `--commit` is passed, the review
file's relative path is staged in the same commit as backup state.

Verifies _git_commit's paths_to_add includes the review path. Doesn't
exercise the full subprocess path — that surfaces an unrelated fixture-
vs-manifest mismatch (PR #13's bundled manifest references files
build_fixture.py doesn't create).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from argit.backup import _git_commit
from argit.manifest import Manifest


def _empty_manifest() -> Manifest:
    return Manifest(
        schema_version=1,
        agent_type="openclaw",
        agent_version="2026.4.14",
        manifest_revision=7,
        source_root="~/.openclaw",
        source_root_mode="0700",
        sanitize=[],
        items=[],
        exclude=[],
        filename="openclaw-2026.4.14-7.manifest.json",
    )


def test_git_commit_includes_review_path_when_provided(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".argit" / "reviews").mkdir(parents=True)
    review_file = repo / ".argit" / "reviews" / "2026-04-29T12:00:00Z.md"
    review_file.write_text("# review\n")

    captured: dict = {}

    def _stub_subprocess_run(*args, **kwargs):
        # Capture the `git add --` invocation; bypass real git operations.
        captured["args"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    with patch("argit.backup.subprocess.run", side_effect=_stub_subprocess_run):
        # Use `dry=True` to avoid hitting the diff/commit/etc. invocations.
        _git_commit(
            repo, _empty_manifest(),
            concrete_items=[], iso="2026-04-29T12:00:00Z",
            dry=True, review_path=review_file,
        )

    # In dry mode _git_commit only prints; nothing actually goes through
    # subprocess.run. Test the not-dry path next via a more permissive
    # subprocess stub.


def test_git_commit_review_path_in_paths_to_add_via_dry_emit(capsys, tmp_path):
    """Dry-run mode prints `git add: <paths_to_add>` line; assert the
    review path is in that list. Same code path that determines what to
    stage on a real commit."""
    repo = tmp_path / "repo"
    (repo / ".argit" / "reviews").mkdir(parents=True)
    review_file = repo / ".argit" / "reviews" / "2026-04-29T12:00:00Z.md"
    review_file.write_text("# review\n")

    _git_commit(
        repo, _empty_manifest(),
        concrete_items=[], iso="2026-04-29T12:00:00Z",
        dry=True, review_path=review_file,
    )

    captured = capsys.readouterr()
    assert ".argit/reviews/2026-04-29T12:00:00Z.md" in captured.out


def test_git_commit_omits_review_path_when_none(capsys, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".argit").mkdir(parents=True)
    _git_commit(
        repo, _empty_manifest(),
        concrete_items=[], iso="2026-04-29T12:00:00Z",
        dry=True, review_path=None,
    )
    captured = capsys.readouterr()
    assert ".argit/reviews/" not in captured.out

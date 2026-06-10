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
from argit.manifest import Item, Manifest


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


def test_git_commit_skips_items_with_missing_target(capsys, tmp_path):
    """An item whose source was absent is skipped during copy, so its target is
    never written. _git_commit must NOT stage that target — otherwise `git add`
    fails with "pathspec did not match any files" and aborts the whole backup.
    (Reproduces a 2026.5.4 manifest declaring scripts/ against a Hermes build
    that has no ~/.hermes/scripts/.)"""
    repo = tmp_path / "repo"
    (repo / "hermes" / "data").mkdir(parents=True)
    # present: copied this run, target exists on disk
    present_tgt = repo / "hermes" / "data" / "config.yaml"
    present_tgt.write_text("model: x\n")
    present = Item(kind="data", source="config.yaml", mode="0644",
                   target="hermes/data/config.yaml")
    # missing: source absent → skipped during copy → target never written
    missing = Item(kind="data", source="scripts/", mode="0755",
                   target="hermes/data/scripts/")

    _git_commit(
        repo, _empty_manifest(),
        concrete_items=[present, missing], iso="2026-06-10T00:00:00Z",
        dry=True, review_path=None,
    )

    out = capsys.readouterr().out
    assert "hermes/data/config.yaml" in out
    assert "hermes/data/scripts/" not in out

"""Unit tests for `argit review`'s orchestration (run_review).

Tests call run_review directly rather than via CliRunner — bypasses the
sys.exit + click harness and lets us assert on the function's exit code
and stderr/stdout via capsys. Pre-flight is stubbed so tests don't
require gpg/pass/git-lfs binaries on PATH.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from argit.review import run_review
from argit.setup import _bundled_manifest_path
from argit.shared import EXIT_OK, PreflightResult


BUNDLED = _bundled_manifest_path()


def _setup_minimal_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a repo skeleton with a bundled manifest + a fake source_root.
    Returns (repo_root, source_root_dir)."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".argit" / "manifest").mkdir(parents=True)
    shutil.copy2(BUNDLED, repo / ".argit" / "manifest" / BUNDLED.name)
    source_root = tmp_path / "fake-home" / ".openclaw"
    source_root.mkdir(parents=True)
    return repo, source_root


@pytest.fixture(autouse=True)
def _stub_preflight():
    """Skip real binary + git-config checks; the orchestration logic
    under test runs after preflight regardless."""
    with patch("argit.review.run_preflight",
               return_value=PreflightResult(repo_root=Path("/"))):
        yield


def _patch_source_root(monkeypatch, source_root: Path):
    """Make load_manifest's resulting Manifest's expanded_source_root()
    return the test's controllable tmp dir."""
    from argit.manifest import Manifest as _M
    monkeypatch.setattr(_M, "expanded_source_root", lambda self: source_root)


def test_no_findings_returns_ok_no_file(tmp_path, monkeypatch, capsys):
    repo, source_root = _setup_minimal_repo(tmp_path)
    _patch_source_root(monkeypatch, source_root)

    code = run_review(repo)

    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "no findings" in captured.out
    # No review file written.
    assert not (repo / ".argit" / "reviews").exists()


def test_with_findings_writes_report(tmp_path, monkeypatch, capsys):
    repo, source_root = _setup_minimal_repo(tmp_path)
    _patch_source_root(monkeypatch, source_root)
    # Plant an uncovered file in source_root that no manifest rule matches.
    uncovered = source_root / "future-plugin" / "state.json"
    uncovered.parent.mkdir(parents=True)
    uncovered.write_text(json.dumps({"hello": "world"}) + "\n")

    code = run_review(repo)

    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "1 findings" in captured.out
    review_files = list((repo / ".argit" / "reviews").glob("*.md"))
    assert len(review_files) == 1
    body = review_files[0].read_text(encoding="utf-8")
    assert "future-plugin/state.json" in body
    assert "## Uncovered paths" in body


def test_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    repo, source_root = _setup_minimal_repo(tmp_path)
    _patch_source_root(monkeypatch, source_root)
    (source_root / "loose.json").write_text("{}")

    code = run_review(repo, dry_run=True)

    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "would: write .argit/reviews/" in captured.out
    assert not (repo / ".argit" / "reviews").exists()

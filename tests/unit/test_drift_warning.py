"""Unit tests for backup-time bundled-manifest drift warning.

Verifies `_warn_on_bundled_drift` in backup.py:
- Warns only on `operator_modified` classification.
- Silent on `clean`, `stale_bundle`, malformed catalog, missing bundled,
  empty filename.
- Reuses setup's `_classify_drift` (single source of truth).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from argit.backup import _warn_on_bundled_drift
from argit.errors import ArgitError
from argit.hashing import canonical_hash
from argit.manifest import Manifest


def _write(path: Path, body: dict) -> Path:
    path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    return path


def _make_rev(tmp_path: Path, rev: int) -> Path:
    body = {
        "agent_type": "openclaw",
        "agent_version": "2026.4.14",
        "manifest_revision": rev,
    }
    name = f"openclaw-2026.4.14-{rev}.manifest.json"
    return _write(tmp_path / name, body)


def _make_repo(tmp_path: Path, manifest_filename: str, manifest_body: dict | None = None) -> Path:
    """Create a repo skeleton with a bundled manifest at the canonical path."""
    repo = tmp_path / "repo"
    mdir = repo / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    body = manifest_body or {
        "agent_type": "openclaw",
        "agent_version": "2026.4.14",
        "manifest_revision": 7,
    }
    _write(mdir / manifest_filename, body)
    return repo


def _manifest(filename: str) -> Manifest:
    """Synthetic Manifest dataclass — only fields the helper reads matter."""
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
        filename=filename,
    )


# ---------- silent paths ----------


def test_empty_filename_is_silent(tmp_path, capsys):
    """Defensive: a Manifest with default filename="" never triggers the
    warning even if catalog/bundled state would otherwise classify."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _warn_on_bundled_drift(repo, _manifest(""))
    captured = capsys.readouterr()
    assert captured.err == ""


def test_bundled_missing_is_silent(tmp_path, capsys):
    """No file at the canonical bundled path → silent (operator removed it,
    or never ran setup; not the helper's job to flag)."""
    repo = tmp_path / "repo"
    (repo / ".argit" / "manifest").mkdir(parents=True)
    # No manifest file written.
    _warn_on_bundled_drift(repo, _manifest("openclaw-2026.4.14-7.manifest.json"))
    captured = capsys.readouterr()
    assert captured.err == ""


def test_clean_classification_is_silent(tmp_path, capsys):
    """Bundled matches catalog's current entry → silent."""
    rev7 = _make_rev(tmp_path, 7)
    repo = _make_repo(tmp_path, "openclaw-2026.4.14-7.manifest.json")
    # Mirror tmp_path bundled into the repo's bundled location.
    (repo / ".argit" / "manifest" / "openclaw-2026.4.14-7.manifest.json").write_text(
        rev7.read_text()
    )
    catalog = {rev7.name: canonical_hash(rev7)}
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        _warn_on_bundled_drift(repo, _manifest("openclaw-2026.4.14-7.manifest.json"))
    captured = capsys.readouterr()
    assert captured.err == ""


def test_stale_bundle_classification_is_silent(tmp_path, capsys):
    """Bundled matches an OLDER catalog entry → silent. Setup's upgrade
    flow handles this; backup is not the surface to warn on it."""
    rev3 = _make_rev(tmp_path, 3)
    rev7 = _make_rev(tmp_path, 7)
    # Repo has rev3 as its bundled (older) — common when operator lags upgrades.
    repo = tmp_path / "repo"
    mdir = repo / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    (mdir / "openclaw-2026.4.14-3.manifest.json").write_text(rev3.read_text())
    catalog = {
        rev3.name: canonical_hash(rev3),
        rev7.name: canonical_hash(rev7),
    }
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        _warn_on_bundled_drift(repo, _manifest("openclaw-2026.4.14-3.manifest.json"))
    captured = capsys.readouterr()
    assert captured.err == ""


def test_malformed_catalog_is_silent(tmp_path, capsys):
    """`_classify_drift` raises ArgitError on malformed catalog → caught,
    silent. Best-effort: never block backup on a corrupt-catalog edge case."""
    repo = _make_repo(tmp_path, "openclaw-2026.4.14-7.manifest.json")
    err = ArgitError("hash catalog malformed", "reinstall argit")
    with patch("argit.setup._load_hash_catalog", side_effect=err):
        _warn_on_bundled_drift(repo, _manifest("openclaw-2026.4.14-7.manifest.json"))
    captured = capsys.readouterr()
    assert captured.err == ""


def test_unknown_filename_classifies_operator_modified(tmp_path, capsys):
    """A bundled-shaped filename not present in the catalog classifies as
    `operator_modified` → warning fires. (This is the agent-misbehavior
    case: the agent edited the manifest enough that the hash no longer
    matches anything in the catalog.)"""
    rev7 = _make_rev(tmp_path, 7)
    repo = _make_repo(tmp_path, "openclaw-2026.4.14-7.manifest.json")
    # Repo's bundled is mutated content (different from rev7).
    (repo / ".argit" / "manifest" / "openclaw-2026.4.14-7.manifest.json").write_text(
        json.dumps({"agent_type": "openclaw", "manifest_revision": 99}) + "\n"
    )
    catalog = {rev7.name: canonical_hash(rev7)}  # mutated content not in catalog
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        _warn_on_bundled_drift(repo, _manifest("openclaw-2026.4.14-7.manifest.json"))
    captured = capsys.readouterr()
    assert "bundled manifest hash mismatch" in captured.err
    assert "openclaw-2026.4.14-7.manifest.json" in captured.err
    assert "Run `argit setup`" in captured.err


# ---------- mutation: real-world hand-edit ----------


def test_hand_edited_manifest_triggers_warning(tmp_path, capsys):
    """End-to-end: the operator (or a misbehaving agent) edited the bundled
    manifest. Mutating one byte changes the canonical hash → catalog miss
    → `operator_modified` → warning."""
    rev7_clean = _make_rev(tmp_path, 7)
    repo = _make_repo(tmp_path, "openclaw-2026.4.14-7.manifest.json")
    bundled = repo / ".argit" / "manifest" / "openclaw-2026.4.14-7.manifest.json"
    bundled.write_text(rev7_clean.read_text())
    # Mutate: append a JSON-valid trailing comment-like field.
    body = json.loads(bundled.read_text())
    body["__operator_note"] = "added a field"
    bundled.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    catalog = {rev7_clean.name: canonical_hash(rev7_clean)}
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        _warn_on_bundled_drift(repo, _manifest("openclaw-2026.4.14-7.manifest.json"))
    captured = capsys.readouterr()
    assert "bundled manifest hash mismatch" in captured.err

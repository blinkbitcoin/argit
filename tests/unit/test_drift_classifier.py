"""Unit tests for Track A drift classifier.

ACs:
- AC-A1: clean → ("clean", None)
- AC-A2: stale bundle → ("stale_bundle", N)
- AC-A3: operator-modified → ("operator_modified", None)
- AC-A25: parse-independence — classifier does NOT invoke load_manifest

The classifier consults the package-shipped `hashes.json`. We monkey-patch
the catalog loader to feed synthetic revisions so the tests don't depend
on which manifests happen to ship at test time.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from argit.hashing import canonical_hash
from argit.setup import _classify_drift


def _write(path: Path, body: dict) -> Path:
    path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    return path


def _make_rev(tmp_path: Path, rev: int, extra_body: dict | None = None) -> Path:
    body = {
        "agent_type": "openclaw",
        "agent_version": "2026.4.14",
        "manifest_revision": rev,
    }
    if extra_body:
        body.update(extra_body)
    name = f"openclaw-2026.4.14-{rev}.manifest.json"
    return _write(tmp_path / name, body)


def _catalog_for(*paths: Path) -> dict[str, str]:
    return {p.name: canonical_hash(p) for p in paths}


# ---------- AC-A1 — clean ----------

def test_ac_a1_clean_matches_current_bundled(tmp_path):
    rev1 = _make_rev(tmp_path, 1)
    rev2 = _make_rev(tmp_path, 2)
    catalog = _catalog_for(rev1, rev2)
    # Repo has the latest (rev2) → clean. Stage at expected filename so
    # parse_filename accepts it.
    target = tmp_path / "repo" / "openclaw-2026.4.14-2.manifest.json"
    target.parent.mkdir()
    target.write_bytes(rev2.read_bytes())

    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        result = _classify_drift(target)
    assert result == ("clean", None)


# ---------- AC-A2 — stale bundle ----------

def test_ac_a2_stale_bundle_older_revision(tmp_path):
    rev1 = _make_rev(tmp_path, 1)
    rev2 = _make_rev(tmp_path, 2)
    catalog = _catalog_for(rev1, rev2)
    # Repo has rev1 while rev2 ships → stale_bundle.
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        result = _classify_drift(rev1)
    assert result == ("stale_bundle", 1)


def test_stale_bundle_multiple_older_revisions(tmp_path):
    rev1 = _make_rev(tmp_path, 1)
    rev2 = _make_rev(tmp_path, 2)
    rev3 = _make_rev(tmp_path, 3)
    catalog = _catalog_for(rev1, rev2, rev3)
    # Repo pinned to rev2 while rev3 is latest → stale at rev2.
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        result = _classify_drift(rev2)
    assert result == ("stale_bundle", 2)


# ---------- AC-A3 — operator-modified ----------

def test_ac_a3_operator_modified_no_catalog_match(tmp_path):
    rev1 = _make_rev(tmp_path, 1)
    rev2 = _make_rev(tmp_path, 2)
    catalog = _catalog_for(rev1, rev2)
    # Repo has a hand-edited manifest with a body the catalog doesn't know.
    hand_edited = _make_rev(tmp_path, 99, extra_body={"custom_field": "operator-inserted"})
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        result = _classify_drift(hand_edited)
    assert result == ("operator_modified", None)


def test_operator_modified_tiny_edit_in_known_manifest(tmp_path):
    rev1 = _make_rev(tmp_path, 1)
    rev2 = _make_rev(tmp_path, 2)
    catalog = _catalog_for(rev1, rev2)
    # Start from rev2 body and add an extra field → hash mismatch.
    body = json.loads(rev2.read_text())
    body["custom_exclude"] = "operator.json"
    modified = _write(tmp_path / "openclaw-2026.4.14-2.manifest.json.mod", body)
    # Rename so parse_filename is happy.
    target = tmp_path / "mod-out" / "openclaw-2026.4.14-2.manifest.json"
    target.parent.mkdir()
    target.write_bytes(modified.read_bytes())
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        result = _classify_drift(target)
    assert result == ("operator_modified", None)


# ---------- empty-catalog fallback ----------

def test_empty_catalog_falls_back_to_operator_modified(tmp_path):
    rev1 = _make_rev(tmp_path, 1)
    with patch("argit.setup._load_hash_catalog", return_value={}):
        result = _classify_drift(rev1)
    assert result == ("operator_modified", None)


# ---------- AC-A25 — parse-independence ----------

def test_ac_a25_classifier_does_not_invoke_load_manifest(tmp_path):
    """A manifest with grammar the current parser would reject must still
    classify. We stage a manifest missing required fields (no 'source_root')
    and confirm the classifier returns a drift result without raising.
    """
    body = {
        "agent_type": "openclaw",
        "agent_version": "2026.4.14",
        "manifest_revision": 99,
        "legacy_field_no_longer_allowed": True,
    }
    path = tmp_path / "openclaw-2026.4.14-99.manifest.json"
    path.write_text(json.dumps(body, sort_keys=True) + "\n")
    catalog: dict[str, str] = {}  # empty → operator_modified
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        # Must not raise any parse / validation error.
        result = _classify_drift(path)
    assert result == ("operator_modified", None)


def test_classifier_rejects_malformed_json(tmp_path):
    """Malformed JSON should raise ArgitError from canonical_hash (first-
    touch quality), not percolate a raw JSONDecodeError."""
    from argit.errors import ArgitError
    path = tmp_path / "openclaw-2026.4.14-1.manifest.json"
    path.write_text("{broken")
    with patch("argit.setup._load_hash_catalog", return_value={}):
        with pytest.raises(ArgitError) as exc:
            _classify_drift(path)
    assert "not valid JSON" in str(exc.value)


# ---------- catalog-entry hygiene ----------

def test_catalog_entries_with_malformed_filenames_skipped(tmp_path):
    """A catalog entry with a filename not matching the agent-type-version-rev
    pattern should be silently skipped, not crash classification."""
    rev1 = _make_rev(tmp_path, 1)
    catalog = {
        "openclaw-2026.4.14-1.manifest.json": canonical_hash(rev1),
        "totally-wrong-name.txt": "a" * 64,
    }
    with patch("argit.setup._load_hash_catalog", return_value=catalog):
        result = _classify_drift(rev1)
    assert result == ("clean", None)

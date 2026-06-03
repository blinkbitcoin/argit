"""Unit tests for `argit drift` (collect_drift / run_drift) — issue #25.

The command is a read-only report over the same hash-only classifier
`setup._classify_drift` consumes. These tests monkeypatch the bundled
manifest path + hash catalog so they don't depend on which revisions
happen to ship at test time; one non-mocked smoke test exercises the real
package wiring (no-manifest case).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from argit.drift import DRIFT_SCHEMA, collect_drift, run_drift
from argit.errors import ArgitError
from argit.hashing import canonical_hash
from argit.shared import EXIT_OK


def _make_manifest(path: Path, rev: int, extra: dict | None = None) -> Path:
    body = {
        "agent_type": "openclaw",
        "agent_version": "2026.4.14",
        "manifest_revision": rev,
    }
    if extra:
        body.update(extra)
    path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    return path


def _stage_repo_manifest(repo_root: Path, source: Path, name: str | None = None) -> Path:
    mdir = repo_root / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    target = mdir / (name or source.name)
    target.write_bytes(source.read_bytes())
    return target


def _scenario(tmp_path: Path, *, bundled_rev: int, repo_source_rev: int,
              catalog_revs: tuple[int, ...], operator_edit: bool = False):
    """Build synthetic bundled manifests + catalog, stage a repo manifest,
    and return (repo_root, patch-context-managers)."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    revs = {r: _make_manifest(scratch / f"openclaw-2026.4.14-{r}.manifest.json", r)
            for r in catalog_revs}
    catalog = {p.name: canonical_hash(p) for p in revs.values()}
    bundled_path = revs[bundled_rev]

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    extra = {"operator_field": "x"} if operator_edit else None
    source = _make_manifest(
        scratch / f"src-openclaw-2026.4.14-{repo_source_rev}.manifest.json",
        repo_source_rev, extra=extra,
    )
    _stage_repo_manifest(repo_root, source, f"openclaw-2026.4.14-{repo_source_rev}.manifest.json")
    return repo_root, catalog, bundled_path


def _collect(repo_root, catalog, bundled_path):
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled_path), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.setup._load_hash_catalog", return_value=catalog):
        return collect_drift(repo_root)


# ---------- clean ----------

def test_clean(tmp_path):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled_rev=3, repo_source_rev=3, catalog_revs=(1, 2, 3))
    payload = _collect(repo_root, catalog, bundled)
    assert payload["schema"] == DRIFT_SCHEMA
    assert payload["state"] == "clean"
    assert payload["repo_revision"] == 3
    assert payload["bundled_revision"] == 3
    assert payload["revisions_behind"] == 0
    assert payload["upgrade_available"] is False
    assert payload["manifest_file"] == "openclaw-2026.4.14-3.manifest.json"


# ---------- stale_bundle ----------

def test_stale_bundle_reports_revisions_behind(tmp_path):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled_rev=3, repo_source_rev=1, catalog_revs=(1, 2, 3))
    payload = _collect(repo_root, catalog, bundled)
    assert payload["state"] == "stale_bundle"
    assert payload["repo_revision"] == 1
    assert payload["bundled_revision"] == 3
    assert payload["revisions_behind"] == 2
    assert payload["upgrade_available"] is True


# ---------- operator_modified ----------

def test_operator_modified(tmp_path):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled_rev=3, repo_source_rev=2, catalog_revs=(1, 2, 3),
        operator_edit=True)
    payload = _collect(repo_root, catalog, bundled)
    assert payload["state"] == "operator_modified"
    assert payload["repo_revision"] is None
    assert payload["revisions_behind"] is None
    assert payload["upgrade_available"] is False
    # filename is still surfaced for the operator
    assert payload["manifest_file"] == "openclaw-2026.4.14-2.manifest.json"


# ---------- no_manifest ----------

def test_no_manifest_uses_real_package(tmp_path):
    """Non-mocked: a fresh repo with no manifest classifies as no_manifest,
    and bundled_revision resolves from the actually-shipped manifests."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    with patch("argit.drift.probe_agent_version", lambda _b: None):
        payload = collect_drift(repo_root)
    assert payload["state"] == "no_manifest"
    assert payload["manifest_file"] is None
    assert payload["repo_revision"] is None
    assert isinstance(payload["bundled_revision"], int)
    assert payload["upgrade_available"] is False


# ---------- agent-type mismatch raises ----------

def test_agent_type_mismatch_raises(tmp_path):
    repo_root = tmp_path / "repo"
    mdir = repo_root / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    (mdir / "hermes-2026.4.14-1.manifest.json").write_text(
        json.dumps({"agent_type": "hermes", "agent_version": "2026.4.14",
                    "manifest_revision": 1}) + "\n")
    with patch("argit.drift.probe_agent_version", lambda _b: None):
        with pytest.raises(ArgitError) as exc:
            collect_drift(repo_root)
    assert "hermes" in str(exc.value)


# ---------- run_drift output + exit code ----------

def test_run_drift_json_parses_and_exits_zero(tmp_path, capsys):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled_rev=3, repo_source_rev=1, catalog_revs=(1, 2, 3))
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.setup._load_hash_catalog", return_value=catalog):
        code = run_drift(repo_root, as_json=True)
    assert code == EXIT_OK  # always 0 even with drift detected (#25)
    out = capsys.readouterr().out
    parsed = json.loads(out)  # single line, json-lines friendly
    assert parsed["state"] == "stale_bundle"
    assert "\n" not in out.strip()


def test_run_drift_human_mirrors_setup_phrasing(tmp_path, capsys):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled_rev=3, repo_source_rev=1, catalog_revs=(1, 2, 3))
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.setup._load_hash_catalog", return_value=catalog):
        code = run_drift(repo_root, as_json=False)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "= manifest drift: stale bundle (rev 1 → 3 available)" in out

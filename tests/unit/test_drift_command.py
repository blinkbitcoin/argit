"""Unit tests for `argit drift` (collect_drift / run_drift) — issue #25.

The command is a read-only report that classifies the in-repo manifest
against the *selected bundled* manifest (NOT the latest revision within the
repo manifest's own version family — see test_stale_across_version_family).
Synthetic-catalog tests monkeypatch the bundled path + catalog; two
non-mocked tests exercise the real shipped manifests.
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


def _make_manifest(path: Path, ver: str, rev: int, extra: dict | None = None) -> Path:
    body = {
        "agent_type": "openclaw",
        "agent_version": ver,
        "manifest_revision": rev,
    }
    if extra:
        body.update(extra)
    path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    return path


def _stage_repo_manifest(repo_root: Path, source: Path, name: str) -> Path:
    mdir = repo_root / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    target = mdir / name
    target.write_bytes(source.read_bytes())
    return target


def _scenario(tmp_path: Path, *, bundled: tuple[str, int], repo: tuple[str, int],
              catalog_revs: tuple[tuple[str, int], ...], operator_edit: bool = False):
    """Build synthetic bundled manifests + catalog and stage a repo manifest.

    `bundled`/`repo` are (agent_version, revision) tuples. Returns
    (repo_root, catalog, bundled_path)."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    by_id: dict[tuple[str, int], Path] = {}
    for ver, rev in catalog_revs:
        by_id[(ver, rev)] = _make_manifest(
            scratch / f"openclaw-{ver}-{rev}.manifest.json", ver, rev)
    catalog = {p.name: canonical_hash(p) for p in by_id.values()}
    bundled_path = by_id[bundled]

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rver, rrev = repo
    extra = {"operator_field": "x"} if operator_edit else None
    source = _make_manifest(
        scratch / f"src-openclaw-{rver}-{rrev}.manifest.json", rver, rrev, extra=extra)
    _stage_repo_manifest(repo_root, source, f"openclaw-{rver}-{rrev}.manifest.json")
    return repo_root, catalog, bundled_path


def _collect(repo_root, catalog, bundled_path):
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled_path), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.drift._load_hash_catalog", return_value=catalog):
        return collect_drift(repo_root)


# ---------- clean ----------

def test_clean(tmp_path):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled=("2026.4.14", 3), repo=("2026.4.14", 3),
        catalog_revs=(("2026.4.14", 1), ("2026.4.14", 2), ("2026.4.14", 3)))
    p = _collect(repo_root, catalog, bundled)
    assert p["schema"] == DRIFT_SCHEMA
    assert p["state"] == "clean"
    assert p["repo_revision"] == 3
    assert p["repo_agent_version"] == "2026.4.14"
    assert p["bundled_revision"] == 3
    assert p["revisions_behind"] == 0
    assert p["upgrade_available"] is False
    assert p["manifest_file"] == "openclaw-2026.4.14-3.manifest.json"


# ---------- stale_bundle (same version family) ----------

def test_stale_bundle_same_family_reports_revisions_behind(tmp_path):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled=("2026.4.14", 3), repo=("2026.4.14", 1),
        catalog_revs=(("2026.4.14", 1), ("2026.4.14", 2), ("2026.4.14", 3)))
    p = _collect(repo_root, catalog, bundled)
    assert p["state"] == "stale_bundle"
    assert p["repo_revision"] == 1
    assert p["repo_agent_version"] == "2026.4.14"
    assert p["bundled_revision"] == 3
    assert p["revisions_behind"] == 2
    assert p["upgrade_available"] is True


# ---------- stale_bundle ACROSS version families (the bot's HIGH finding) ----------

def test_stale_across_version_family_not_reported_clean(tmp_path):
    """Repo pinned to the NEWEST revision of an OLDER version family, while the
    bundled best-fit has moved to a NEWER family. Must be stale_bundle, never
    clean — this is exactly the pinned-vs-bundled gap the command exposes.

    `revisions_behind` is null (rev numbers reset per family); repo_revision
    reflects the repo's TRUE revision (7), not the bundled one."""
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled=("2026.4.26", 2), repo=("2026.4.14", 7),
        catalog_revs=(
            ("2026.4.14", 7), ("2026.4.26", 1), ("2026.4.26", 2)))
    p = _collect(repo_root, catalog, bundled)
    assert p["state"] == "stale_bundle"
    assert p["repo_revision"] == 7
    assert p["repo_agent_version"] == "2026.4.14"
    assert p["bundled_revision"] == 2
    assert p["bundled_agent_version"] == "2026.4.26"
    assert p["revisions_behind"] is None
    assert p["upgrade_available"] is True


def test_stale_across_family_uses_real_package(tmp_path):
    """Non-mocked regression against the shipped manifests: openclaw-2026.4.14-7
    is latest within its family but stale vs the current bundled best-fit."""
    pkg = Path(__file__).resolve().parents[2] / "src" / "argit" / "manifest_templates"
    src = pkg / "openclaw-2026.4.14-7.manifest.json"
    if not src.is_file():
        pytest.skip("expected shipped manifest openclaw-2026.4.14-7 not present")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _stage_repo_manifest(repo_root, src, src.name)
    with patch("argit.drift.probe_agent_version", lambda _b: None):
        p = collect_drift(repo_root)
    assert p["state"] == "stale_bundle"
    assert p["repo_revision"] == 7
    assert p["repo_agent_version"] == "2026.4.14"
    assert p["upgrade_available"] is True


# ---------- operator_modified ----------

def test_operator_modified(tmp_path):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled=("2026.4.14", 3), repo=("2026.4.14", 2),
        catalog_revs=(("2026.4.14", 1), ("2026.4.14", 2), ("2026.4.14", 3)),
        operator_edit=True)
    p = _collect(repo_root, catalog, bundled)
    assert p["state"] == "operator_modified"
    assert p["repo_revision"] is None
    assert p["repo_agent_version"] is None
    assert p["revisions_behind"] is None
    assert p["upgrade_available"] is False
    assert p["manifest_file"] == "openclaw-2026.4.14-2.manifest.json"


# ---------- no_manifest ----------

def test_no_manifest_uses_real_package(tmp_path):
    """Non-mocked: a fresh repo with no manifest classifies as no_manifest,
    and bundled fields resolve from the actually-shipped manifests."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    with patch("argit.drift.probe_agent_version", lambda _b: None):
        p = collect_drift(repo_root)
    assert p["state"] == "no_manifest"
    assert p["manifest_file"] is None
    assert p["repo_revision"] is None
    assert isinstance(p["bundled_revision"], int)
    assert p["upgrade_available"] is False


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
        tmp_path, bundled=("2026.4.14", 3), repo=("2026.4.14", 1),
        catalog_revs=(("2026.4.14", 1), ("2026.4.14", 2), ("2026.4.14", 3)))
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.drift._load_hash_catalog", return_value=catalog):
        code = run_drift(repo_root, as_json=True)
    assert code == EXIT_OK  # always 0 even with drift detected (#25)
    out = capsys.readouterr().out
    parsed = json.loads(out)  # single line, json-lines friendly
    assert parsed["state"] == "stale_bundle"
    assert "\n" not in out.strip()


def test_run_drift_human_same_family_mirrors_setup(tmp_path, capsys):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled=("2026.4.14", 3), repo=("2026.4.14", 1),
        catalog_revs=(("2026.4.14", 1), ("2026.4.14", 2), ("2026.4.14", 3)))
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.drift._load_hash_catalog", return_value=catalog):
        run_drift(repo_root, as_json=False)
    assert "= manifest drift: stale bundle (rev 1 → 3 available)" in capsys.readouterr().out


def test_run_drift_human_cross_family_prints_filenames(tmp_path, capsys):
    repo_root, catalog, bundled = _scenario(
        tmp_path, bundled=("2026.4.26", 2), repo=("2026.4.14", 7),
        catalog_revs=(("2026.4.14", 7), ("2026.4.26", 2)))
    with patch("argit.drift._bundled_manifest_path", lambda **kw: bundled), \
         patch("argit.drift.probe_agent_version", lambda _b: None), \
         patch("argit.drift._load_hash_catalog", return_value=catalog):
        run_drift(repo_root, as_json=False)
    out = capsys.readouterr().out
    assert "openclaw-2026.4.14-7.manifest.json → openclaw-2026.4.26-2.manifest.json" in out

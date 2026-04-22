"""Unit tests for scripts/rebuild_hash_catalog.py — AC-A13, AC-A14.

The script lives outside the `argit` package (it's a maintainer CLI). We
load it from source via importlib so tests don't need the script to be
PYTHONPATH-visible under its import name.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_hash_catalog.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rebuild_hash_catalog", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_manifest(dir_: Path, rev: int) -> Path:
    body = {
        "agent_type": "openclaw",
        "agent_version": "2026.4.14",
        "manifest_revision": rev,
        "payload": f"fixture-rev-{rev}",
    }
    path = dir_ / f"openclaw-2026.4.14-{rev}.manifest.json"
    path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    return path


@pytest.fixture
def tmp_manifest_dir(tmp_path):
    d = tmp_path / "manifest_templates"
    d.mkdir()
    return d


@pytest.fixture
def script_with_tmp_dir(tmp_manifest_dir):
    """Import the script and redirect its MANIFEST_DIR + CATALOG_PATH at
    the module level — the script reads both constants once at function
    entry, so monkey-patching the module attributes is sufficient."""
    script = _load_script()
    with patch.object(script, "MANIFEST_DIR", tmp_manifest_dir), \
         patch.object(script, "CATALOG_PATH", tmp_manifest_dir / "hashes.json"):
        yield script, tmp_manifest_dir


# ---------- AC-A13 — dry-run diff behavior ----------

def test_a13_dry_run_empty_both_reports_in_sync(script_with_tmp_dir, capsys):
    """No manifests, no catalog → in sync, exit 0."""
    script, _ = script_with_tmp_dir
    code = script.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "in sync" in out


def test_a13_dry_run_stale_catalog_exits_1_with_diff(script_with_tmp_dir, capsys):
    """Manifests exist but catalog is empty → exit 1 + `+` diff lines."""
    script, mdir = script_with_tmp_dir
    _write_manifest(mdir, 1)
    _write_manifest(mdir, 2)
    code = script.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "+ openclaw-2026.4.14-1.manifest.json" in out
    assert "+ openclaw-2026.4.14-2.manifest.json" in out
    assert "(new)" in out


def test_a13_dry_run_detects_changed_entry(script_with_tmp_dir, capsys):
    """Committed catalog has wrong hash → exit 1 + `~` diff line."""
    script, mdir = script_with_tmp_dir
    _write_manifest(mdir, 1)
    # Seed catalog with a wrong hash for the existing manifest.
    (mdir / "hashes.json").write_text(json.dumps({
        "openclaw-2026.4.14-1.manifest.json": "0" * 64,
    }))
    code = script.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "~ openclaw-2026.4.14-1.manifest.json" in out
    assert "(changed)" in out


def test_a13_dry_run_detects_removed_entry(script_with_tmp_dir, capsys):
    """Catalog references a manifest that no longer exists → exit 1 + `-` line."""
    script, mdir = script_with_tmp_dir
    (mdir / "hashes.json").write_text(json.dumps({
        "openclaw-2026.4.14-9.manifest.json": "a" * 64,
    }))
    code = script.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "- openclaw-2026.4.14-9.manifest.json" in out
    assert "(removed)" in out


def test_a13_dry_run_synced_catalog_exits_0(script_with_tmp_dir, capsys):
    """After --write, dry-run should report in sync."""
    script, mdir = script_with_tmp_dir
    _write_manifest(mdir, 1)
    _write_manifest(mdir, 2)
    script.main(["--write"])
    capsys.readouterr()  # drain
    code = script.main([])
    assert code == 0
    assert "in sync" in capsys.readouterr().out


# ---------- AC-A14 — remediation message mentions --write command ----------

def test_a14_stale_catalog_remediation_names_script(script_with_tmp_dir, capsys):
    script, mdir = script_with_tmp_dir
    _write_manifest(mdir, 1)
    script.main([])
    out = capsys.readouterr().out
    assert "python scripts/rebuild_hash_catalog.py --write" in out


# ---------- --write produces consumable catalog ----------

def test_write_produces_valid_catalog(script_with_tmp_dir, tmp_manifest_dir):
    """--write creates hashes.json with entries for every manifest."""
    script, mdir = script_with_tmp_dir
    _write_manifest(mdir, 1)
    _write_manifest(mdir, 2)
    _write_manifest(mdir, 3)
    code = script.main(["--write"])
    assert code == 0
    catalog_path = tmp_manifest_dir / "hashes.json"
    assert catalog_path.is_file()
    catalog = json.loads(catalog_path.read_text())
    assert len(catalog) == 3
    # Every entry is a hex digest of the expected length (SHA-256 = 64 hex).
    for name, digest in catalog.items():
        assert name.endswith(".manifest.json")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


def test_write_is_idempotent(script_with_tmp_dir, tmp_manifest_dir):
    """Running --write twice produces byte-identical hashes.json."""
    script, mdir = script_with_tmp_dir
    _write_manifest(mdir, 1)
    script.main(["--write"])
    first = (tmp_manifest_dir / "hashes.json").read_bytes()
    script.main(["--write"])
    second = (tmp_manifest_dir / "hashes.json").read_bytes()
    assert first == second

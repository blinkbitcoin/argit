"""Unit tests for `argit info` (run_info / collect_info).

`info` is repo-independent and dependency-free, so these tests need no
gpg/pass/git harness — they assert on the payload shape and that the
emitted paths actually point at the bundled resources. A gpg-gated test
guards against the declared IT fingerprint drifting from the real key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from argit import __version__
from argit.info import collect_info, run_info
from argit.shared import EXIT_OK, IT_BACKUP_FPR, IT_BACKUP_UID


def test_collect_info_shape():
    info = collect_info()
    assert info["version"] == __version__
    assert info["it_backup_key"] == {
        "fingerprint": IT_BACKUP_FPR,
        "uid": IT_BACKUP_UID,
    }
    assert set(info["resources"]) == {
        "it_backup_pubkey",
        "manifest_templates_dir",
        "hashes_catalog",
    }


def test_resource_paths_exist():
    """The emitted paths must resolve to the actually-bundled files —
    this is the whole point of the command for downstream tooling."""
    res = collect_info()["resources"]
    assert Path(res["it_backup_pubkey"]).is_file()
    assert Path(res["manifest_templates_dir"]).is_dir()
    assert Path(res["hashes_catalog"]).is_file()


def test_manifest_templates_listed():
    templates = collect_info()["manifest_templates"]
    assert templates, "expected at least one bundled manifest"
    assert all(t.endswith(".manifest.json") for t in templates)
    # hashes.json is NOT a manifest template and must be excluded.
    assert "hashes.json" not in templates
    assert templates == sorted(templates)


def test_run_info_human(capsys):
    code = run_info()
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert f"argit {__version__}" in out
    assert IT_BACKUP_FPR in out


def test_run_info_json_parses(capsys):
    code = run_info(as_json=True)
    assert code == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == collect_info()


def test_declared_fingerprint_matches_bundled_key():
    """Guard against IT_BACKUP_FPR drifting from the shipped .asc file.
    Skipped when gpg is unavailable (info itself never shells out)."""
    if not shutil.which("gpg"):
        pytest.skip("gpg not on PATH")
    asc = Path(collect_info()["resources"]["it_backup_pubkey"])
    cp = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(asc)],
        capture_output=True, text=True, check=True,
    )
    fprs = [ln.split(":")[9] for ln in cp.stdout.splitlines() if ln.startswith("fpr:")]
    assert fprs and fprs[0] == IT_BACKUP_FPR

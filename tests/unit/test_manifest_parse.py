"""Manifest parse/validate — happy path + sad paths."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.manifest import find_manifest_file, load_manifest, parse_filename


BUNDLED = Path(str(resources.files("argit.manifest_templates").joinpath("openclaw-2026.4.14-1.manifest.json")))


def _init_repo(tmp_path: Path, manifest_body: dict | str, filename: str) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".argit" / "manifest").mkdir(parents=True)
    body_str = manifest_body if isinstance(manifest_body, str) else json.dumps(manifest_body)
    (tmp_path / ".argit" / "manifest" / filename).write_text(body_str)
    return tmp_path


def test_parse_filename_simple():
    assert parse_filename("openclaw-2026.4.14-1.manifest.json") == ("openclaw", "2026.4.14", 1)


def test_parse_filename_version_with_internal_dash():
    # agent-version "2026.3.23-2" and revision "1"
    assert parse_filename("openclaw-2026.3.23-2-1.manifest.json") == ("openclaw", "2026.3.23-2", 1)


def test_parse_filename_malformed():
    with pytest.raises(ArgitError) as exc:
        parse_filename("openclaw.json")
    assert "does not match" in str(exc.value)


def test_happy_path(tmp_path):
    body = json.loads(BUNDLED.read_text())
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    m = load_manifest(repo)
    assert m.agent_type == "openclaw"
    assert m.agent_version == "2026.4.14"
    assert m.manifest_revision == 1
    assert m.schema_version == 1
    assert len(m.sanitize) == 2
    assert m.lifecycle is not None
    assert m.lifecycle.detect_running is not None
    assert m.lifecycle.stop is not None
    assert m.lifecycle.start is not None


def test_missing_schema_version(tmp_path):
    body = json.loads(BUNDLED.read_text())
    del body["schema_version"]
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "schema version" in str(exc.value).lower()


def test_unsupported_schema_version(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["schema_version"] = 2
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "schema version" in str(exc.value).lower()
    assert "2" in str(exc.value)


def test_filename_body_mismatch(tmp_path):
    body = json.loads(BUNDLED.read_text())
    repo = _init_repo(tmp_path, body, "wrongname-9.9.9-1.manifest.json")
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "filename" in str(exc.value).lower()


def test_zero_manifests(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".argit" / "manifest").mkdir(parents=True)
    with pytest.raises(ArgitError) as exc:
        find_manifest_file(tmp_path)
    assert "no manifest" in str(exc.value).lower()


def test_two_manifests(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".argit" / "manifest").mkdir(parents=True)
    body = BUNDLED.read_text()
    (tmp_path / ".argit" / "manifest" / BUNDLED.name).write_text(body)
    (tmp_path / ".argit" / "manifest" / "openclaw-2026.4.14-2.manifest.json").write_text(body)
    with pytest.raises(ArgitError) as exc:
        find_manifest_file(tmp_path)
    assert "multiple" in str(exc.value).lower()


def test_secret_with_dir_source_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["items"].append({
        "kind": "secret", "source": "bad-dir/", "pass": "argit/x", "mode": "0600",
    })
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "directory" in str(exc.value).lower()


def test_wildcard_in_sanitize_path_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["sanitize"][0]["rules"].append({"path": ".profiles.*.token", "pass": "argit/x"})
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "wildcard" in str(exc.value).lower()


def test_bad_octal_mode_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["source_root_mode"] = "rwxr-xr-x"
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "octal" in str(exc.value).lower()


def test_mode_3digit_accepted(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["source_root_mode"] = "700"
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    m = load_manifest(repo)
    assert m.source_root_mode == "0700"

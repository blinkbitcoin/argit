"""Manifest parse/validate — happy path + sad paths."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.manifest import find_manifest_file, load_manifest, parse_filename
from argit.setup import _bundled_manifest_path


BUNDLED = _bundled_manifest_path()


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
    assert m.agent_version == body["agent_version"]
    assert m.manifest_revision == body["manifest_revision"]
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


def test_all_bundled_revisions_parse_cleanly(tmp_path):
    """Every shipped manifest revision parses without error and matches its
    own filename. Catches bundled-manifest regressions."""
    from argit.setup import _all_bundled_manifest_paths

    bundled = _all_bundled_manifest_paths()
    assert len(bundled) >= 1, "expected at least one bundled manifest"
    for path in bundled:
        # Stage each revision in isolation in its own tmpdir.
        d = tmp_path / path.stem
        d.mkdir()
        (d / ".git").mkdir()
        (d / ".argit" / "manifest").mkdir(parents=True)
        (d / ".argit" / "manifest" / path.name).write_text(path.read_text())
        m = load_manifest(d)
        assert m.filename == path.name


def test_bundled_manifest_path_picks_latest_openclaw_by_default():
    """`_bundled_manifest_path()` defaults to OpenClaw and returns the latest
    available OpenClaw agent_version, then highest revision within it."""
    from argit.setup import _all_bundled_manifest_paths, _bundled_manifest_path

    latest = _bundled_manifest_path()
    all_revisions = [p for p in _all_bundled_manifest_paths() if p.name.startswith("openclaw-")]
    # Build (version_tuple, rev) keys and assert `latest` has the max.
    def _vkey(name):
        _, ver, rev = parse_filename(name)
        return ([int(p) for p in ver.split("-", 1)[0].split(".") if p.isdigit()], rev)
    assert _vkey(latest.name) == max(_vkey(p.name) for p in all_revisions)


def test_bundled_manifest_path_best_fit_below_version():
    """`_bundled_manifest_path(agent_version=X)` returns the highest
    agent_version ≤ X (best-fit), then highest revision within."""
    from argit.setup import _bundled_manifest_path

    # Pinning to "2026.4.14" should pick the highest revision of 2026.4.14
    # even if a newer 2026.4.26 exists.
    p = _bundled_manifest_path(agent_version="2026.4.14")
    _, ver, _ = parse_filename(p.name)
    assert ver == "2026.4.14"


def test_bundled_manifest_path_selects_hermes_agent_type():
    from argit.setup import _bundled_manifest_path

    p = _bundled_manifest_path(agent_type="hermes")
    agent_type, ver, rev = parse_filename(p.name)
    assert (agent_type, ver, rev) == ("hermes", "2026.5.4", 1)


def test_bundled_manifest_path_no_eligible_raises():
    """Requesting a version older than every shipped manifest raises."""
    from argit.setup import _bundled_manifest_path

    with pytest.raises(ArgitError) as exc:
        _bundled_manifest_path(agent_version="2020.1.1")
    assert "at or below" in str(exc.value)


def test_two_manifests(tmp_path):
    """Two arbitrary manifest files in `.argit/manifest/` → fatal error."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".argit" / "manifest").mkdir(parents=True)
    body = BUNDLED.read_text()
    # Stage two distinct revisions; we don't care about body validity here —
    # only that find_manifest_file rejects the multi-file state.
    (tmp_path / ".argit" / "manifest" / "openclaw-2026.4.14-7.manifest.json").write_text(body)
    (tmp_path / ".argit" / "manifest" / "openclaw-2026.4.14-9.manifest.json").write_text(body)
    with pytest.raises(ArgitError) as exc:
        find_manifest_file(tmp_path)
    assert "multiple" in str(exc.value).lower()


def test_secret_with_dir_source_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["items"].append({"kind": "secret", "source": "bad-dir/"})
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "directory" in str(exc.value).lower()


def test_wildcard_whole_segment_in_sanitize_path_accepted(tmp_path):
    """Single whole-segment `*` is now supported — expansion happens at
    sanitize-time, one rule per matched key. The parse-time guards now
    only reject malformed wildcards (partial/multi/leading)."""
    body = json.loads(BUNDLED.read_text())
    body["sanitize"][0]["rules"].append({"path": ".profiles.*.token"})
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    m = load_manifest(repo)
    paths = [r.path for r in m.sanitize[0].rules]
    assert ".profiles.*.token" in paths


def test_wildcard_partial_segment_in_sanitize_path_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["sanitize"][0]["rules"].append({"path": ".profiles.foo*.token"})
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "whole segment" in str(exc.value).lower()


def test_wildcard_multi_in_sanitize_path_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["sanitize"][0]["rules"].append({"path": ".profiles.*.*.token"})
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "at most one" in str(exc.value).lower()


def test_wildcard_leading_in_sanitize_path_rejected(tmp_path):
    body = json.loads(BUNDLED.read_text())
    body["sanitize"][0]["rules"].append({"path": ".*.token"})
    repo = _init_repo(tmp_path, body, BUNDLED.name)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "first segment" in str(exc.value).lower()


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

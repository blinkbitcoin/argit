"""Track C — overlay discovery + merge + wiring tests.

ACs: AC-C1..C15, AC-INT6/INT7.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.manifest import (
    Manifest,
    _find_overlay,
    _load_overlay,
    _merge,
    load_manifest,
)
from argit.setup import _bundled_manifest_path


BUNDLED = _bundled_manifest_path()


# ---------- test harness ----------

def _stage(tmp_path: Path, bundled_body: dict, overlay_body: dict | str | None) -> Path:
    (tmp_path / ".git").mkdir()
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    (mdir / BUNDLED.name).write_text(json.dumps(bundled_body))
    if overlay_body is not None:
        basename = BUNDLED.name[: -len(".manifest.json")]
        overlay_name = f"{basename}.manifest.local.json"
        overlay_text = (
            overlay_body if isinstance(overlay_body, str) else json.dumps(overlay_body)
        )
        (mdir / overlay_name).write_text(overlay_text)
    return tmp_path


def _fresh_bundled() -> dict:
    return json.loads(BUNDLED.read_text())


# ---------- AC-C1 — overlay discovered + merged ----------

def test_c1_overlay_discovered_and_merged(tmp_path):
    overlay = {"exclude": ["operator-extra/"]}
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    m = load_manifest(repo)
    assert m.overlay_path is not None
    assert "operator-extra/" in m.exclude


def test_no_overlay_manifest_path_is_none(tmp_path):
    repo = _stage(tmp_path, _fresh_bundled(), None)
    m = load_manifest(repo)
    assert m.overlay_path is None


# ---------- AC-C2 — identity-field rejection ----------

@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 1),
        ("agent_type", "openclaw"),
        ("agent_version", "2026.4.14"),
        ("manifest_revision", 99),
        ("source_root", "~/.other"),
        ("source_root_mode", "0755"),
        ("blob_backend", "git-lfs"),
    ],
)
def test_c2_identity_field_in_overlay_rejected(tmp_path, field, value):
    overlay = {field: value}
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert field in str(exc.value)
    assert "identity field" in str(exc.value) or "must not specify" in str(exc.value)


# ---------- AC-C3 — duplicate (source, kind) across bundled + overlay ----------

def test_c3_duplicate_source_kind_rejected_with_both_paths(tmp_path):
    overlay = {
        "items": [{"kind": "secret", "source": "identity/device.json"}],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "identity/device.json" in msg
    assert BUNDLED.name in msg
    assert ".manifest.local.json" in msg


# ---------- AC-C4 — duplicate sanitize rule path ----------

def test_c4_duplicate_sanitize_rule_path_rejected(tmp_path):
    overlay = {
        "sanitize": [
            {"file": "openclaw.json", "rules": [{"path": ".gateway.auth.token"}]},
        ],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert ".gateway.auth.token" in msg
    assert BUNDLED.name in msg
    assert ".manifest.local.json" in msg


# ---------- AC-C6 — lifecycle partial override ----------

def test_c6_lifecycle_partial_override(tmp_path):
    body = _fresh_bundled()
    overlay = {
        "lifecycle": {
            "stop": {
                "description": "Operator's custom stop",
                "command": ["sh", "-c", "custom-stop.sh"],
            },
        },
    }
    repo = _stage(tmp_path, body, overlay)
    m = load_manifest(repo)
    assert m.lifecycle is not None
    assert m.lifecycle.stop is not None
    assert m.lifecycle.stop.description == "Operator's custom stop"
    # detect_running + start inherit from bundled.
    assert m.lifecycle.detect_running is not None
    assert "curl" in " ".join(m.lifecycle.detect_running.command)
    assert m.lifecycle.start is not None
    assert "systemctl" in " ".join(m.lifecycle.start.command)


# ---------- AC-C7 — overlay explicit pass/target rejected ----------

def test_c7_overlay_explicit_pass_rejected(tmp_path):
    overlay = {
        "items": [{
            "kind": "secret", "source": "plugin/token.json", "pass": "argit/stale/path",
        }],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "unknown field 'pass'" in str(exc.value)


def test_c7_overlay_explicit_target_rejected(tmp_path):
    overlay = {
        "items": [{"kind": "data", "source": "plugin/state.json", "target": "stale"}],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "unknown field 'target'" in str(exc.value)


# ---------- AC-C9 — empty overlay bytes ----------

def test_c9_empty_overlay_bytes_rejected(tmp_path):
    repo = _stage(tmp_path, _fresh_bundled(), "")
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "empty" in str(exc.value).lower()
    assert "{}" in str(exc.value)


# ---------- AC-C10 — malformed overlay JSON ----------

def test_c10_malformed_overlay_json_rejected(tmp_path):
    repo = _stage(tmp_path, _fresh_bundled(), '{"exclude":[')
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "not valid JSON" in msg
    assert "line" in msg


# ---------- AC-C11 — overlay non-object root ----------

@pytest.mark.parametrize("body", ['[]', '"string"', '42', 'true', 'null'])
def test_c11_non_object_root_rejected(tmp_path, body):
    repo = _stage(tmp_path, _fresh_bundled(), body)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "must be a JSON object" in str(exc.value)


# ---------- AC-C12 — overlay permission denied ----------

def test_c12_overlay_permission_denied(tmp_path):
    repo = _stage(tmp_path, _fresh_bundled(), {"exclude": ["operator-extra/"]})
    mdir = repo / ".argit" / "manifest"
    basename = BUNDLED.name[: -len(".manifest.json")]
    overlay_path = mdir / f"{basename}.manifest.local.json"
    # Strip read permissions. Skip on root / non-POSIX platforms where
    # this can't be enforced.
    try:
        overlay_path.chmod(0)
        readable_after = os.access(overlay_path, os.R_OK)
    except OSError:
        pytest.skip("cannot strip permissions on this filesystem")
    if readable_after:
        pytest.skip("chmod 0 had no effect (running as root?)")
    try:
        with pytest.raises(ArgitError) as exc:
            load_manifest(repo)
        msg = str(exc.value)
        assert "chmod +r" in msg or "not readable" in msg
    finally:
        # Restore so tmp_path cleanup works.
        overlay_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------- AC-C13 — empty object {} overlay ----------

def test_c13_empty_object_overlay_accepted(tmp_path):
    repo = _stage(tmp_path, _fresh_bundled(), {})
    m = load_manifest(repo)
    assert m.overlay_path is not None
    # Merged manifest is identical to bundled.
    twin_dir = tmp_path / "twin"
    twin_dir.mkdir()
    bundled_m = load_manifest(_stage(twin_dir, _fresh_bundled(), None))
    assert m.exclude == bundled_m.exclude
    assert len(m.items) == len(bundled_m.items)
    assert len(m.sanitize) == len(bundled_m.sanitize)


# ---------- AC-C15 — overlay without bundled ----------

def test_c15_overlay_without_bundled_rejected(tmp_path):
    (tmp_path / ".git").mkdir()
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    basename = BUNDLED.name[: -len(".manifest.json")]
    # Only the overlay, no bundled file.
    (mdir / f"{basename}.manifest.local.json").write_text('{"exclude":[]}')
    with pytest.raises(ArgitError) as exc:
        load_manifest(tmp_path)
    assert "no manifest" in str(exc.value).lower()


# ---------- AC-INT6 — cross-source ambiguity at merge ----------

def test_int6_overlay_literal_vs_bundled_glob_overlap_rejected(tmp_path):
    """A bundled glob and an overlay literal whose sources component-wise
    overlap must be rejected at merge-time with both origins named."""
    body = _fresh_bundled()
    body["items"].append({"kind": "data", "source": "agents/*/plugin-state.json"})
    overlay = {
        "items": [{"kind": "data", "source": "agents/main/plugin-state.json"}],
    }
    repo = _stage(tmp_path, body, overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "agents/*/plugin-state.json" in msg or "agents/main/plugin-state.json" in msg
    assert BUNDLED.name in msg
    assert ".manifest.local.json" in msg


# ---------- AC-C1 extensions — exclude dedup + sanitize new-file append ----------

def test_exclude_dedup(tmp_path):
    body = _fresh_bundled()
    # Use an entry that already exists in the bundled exclude list.
    already_in_bundled = body["exclude"][0]
    overlay = {"exclude": [already_in_bundled, "truly-new/"]}
    repo = _stage(tmp_path, body, overlay)
    m = load_manifest(repo)
    assert m.exclude.count(already_in_bundled) == 1
    assert "truly-new/" in m.exclude


def test_sanitize_new_file_appended(tmp_path):
    overlay = {
        "sanitize": [{
            "file": "operator-plugin.json",
            "rules": [{"path": ".plugin.apiKey"}],
        }],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    m = load_manifest(repo)
    added = [sf for sf in m.sanitize if sf.file == "operator-plugin.json"]
    assert len(added) == 1
    # Derived: argit/openclaw/operator-plugin/plugin/api-key
    assert added[0].rules[0].pass_path == "argit/openclaw/operator-plugin/plugin/api-key"


# ---------- _find_overlay + _load_overlay direct tests ----------

def test_find_overlay_absent_returns_none(tmp_path):
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    bundled = mdir / BUNDLED.name
    bundled.write_text("{}")
    assert _find_overlay(bundled) is None


def test_find_overlay_present_returns_path(tmp_path):
    mdir = tmp_path / ".argit" / "manifest"
    mdir.mkdir(parents=True)
    bundled = mdir / BUNDLED.name
    bundled.write_text("{}")
    basename = BUNDLED.name[: -len(".manifest.json")]
    overlay = mdir / f"{basename}.manifest.local.json"
    overlay.write_text("{}")
    assert _find_overlay(bundled) == overlay


def test_load_overlay_valid_dict(tmp_path):
    p = tmp_path / "x.manifest.local.json"
    p.write_text('{"exclude":["a/"]}')
    assert _load_overlay(p) == {"exclude": ["a/"]}


# ---------- AC-C14 — overlay lifecycle structural error attribution ----------

def test_c14_overlay_lifecycle_missing_description_names_overlay(tmp_path):
    """Overlay lifecycle.stop without `description` must raise with the
    overlay path attributed, not just the generic `lifecycle.stop.description`
    location."""
    overlay = {
        "lifecycle": {
            "stop": {"command": ["true"]},  # missing description
        },
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "overlay" in msg.lower()
    assert ".manifest.local.json" in msg
    assert "description" in msg


def test_c14_overlay_lifecycle_missing_command_names_overlay(tmp_path):
    overlay = {
        "lifecycle": {
            "start": {"description": "custom start"},  # missing command
        },
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "overlay" in msg.lower()
    assert ".manifest.local.json" in msg
    assert "command" in msg


def test_c14_overlay_lifecycle_non_object_rejected(tmp_path):
    overlay = {"lifecycle": "not-an-object"}
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    assert "overlay" in str(exc.value).lower()
    assert ".manifest.local.json" in str(exc.value)


# ---------- AC-INT7 — within-overlay ambiguity names overlay as origin ----------

def test_int7_within_overlay_literal_dup_names_overlay(tmp_path):
    """Two identical overlay literals must raise with the overlay file
    named as the sole origin (not "in both overlay and overlay")."""
    overlay = {
        "items": [
            {"kind": "data", "source": "plugin/state.json"},
            {"kind": "data", "source": "plugin/state.json"},
        ],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "plugin/state.json" in msg
    assert ".manifest.local.json" in msg
    # Must NOT imply the bundled is involved.
    assert "bundled" not in msg.lower() or "within" in msg


@pytest.mark.skip(
    reason="Track D rejects globs in items[].source at parse; glob-vs-literal "
    "within-overlay overlap becomes testable once Track B enables glob sources"
)
def test_int7_within_overlay_glob_vs_literal_overlap(tmp_path):
    overlay = {
        "items": [
            {"kind": "data", "source": "plugin/*/state.json"},
            {"kind": "data", "source": "plugin/main/state.json"},
        ],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    with pytest.raises(ArgitError) as exc:
        load_manifest(repo)
    msg = str(exc.value)
    assert "overlay" in msg
    assert "both in overlay" in msg or "overlay + overlay" in msg


# ---------- origin threading — downstream Item/SanitizeFile carry overlay origin ----------

def test_overlay_items_tagged_overlay_on_dataclass(tmp_path):
    overlay = {
        "items": [
            {"kind": "data", "source": "operator-plugin/state.json"},
            {"kind": "secret", "source": "operator-plugin/token.json"},
        ],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    m = load_manifest(repo)
    overlay_items = [i for i in m.items if i.source.startswith("operator-plugin/")]
    assert len(overlay_items) == 2
    assert all(i.origin == "overlay" for i in overlay_items)
    # Bundled items keep "bundled".
    bundled_items = [i for i in m.items if not i.source.startswith("operator-plugin/")]
    assert all(i.origin == "bundled" for i in bundled_items)


def test_overlay_sanitize_tagged_overlay(tmp_path):
    overlay = {
        "sanitize": [{
            "file": "operator-plugin.json",
            "rules": [{"path": ".plugin.token"}],
        }],
    }
    repo = _stage(tmp_path, _fresh_bundled(), overlay)
    m = load_manifest(repo)
    overlay_sf = [sf for sf in m.sanitize if sf.file == "operator-plugin.json"]
    assert len(overlay_sf) == 1
    assert overlay_sf[0].origin == "overlay"
    bundled_sf = [sf for sf in m.sanitize if sf.file != "operator-plugin.json"]
    assert all(sf.origin == "bundled" for sf in bundled_sf)

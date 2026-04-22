"""Track D grammar negative-case tests — exercise every unknown-field scope,
every kind-default, and the parse-time ambiguity check end-to-end via
load_manifest (not just the pure-function helpers).

AC references: AC-D8, AC-D11, AC-D13, AC-D14, AC-D19. These ACs were
previously covered only transitively by `test_all_bundled_revisions_parse_cleanly`;
this file gives each a direct regression anchor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.manifest import load_manifest
from argit.setup import _bundled_manifest_path


BUNDLED = _bundled_manifest_path()


def _stage(tmp_path: Path, body: dict) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".argit" / "manifest").mkdir(parents=True)
    (tmp_path / ".argit" / "manifest" / BUNDLED.name).write_text(json.dumps(body))
    return tmp_path


def _fresh_body() -> dict:
    return json.loads(BUNDLED.read_text())


# ---------- AC-D8 — mode defaults per kind ----------

def test_ac_d8_secret_mode_defaults_to_0600(tmp_path):
    body = _fresh_body()
    body["items"].append({"kind": "secret", "source": "test/new-secret.json"})
    m = load_manifest(_stage(tmp_path, body))
    added = [i for i in m.items if i.source == "test/new-secret.json"][0]
    assert added.mode == "0600"


def test_ac_d8_data_mode_defaults_to_0644(tmp_path):
    body = _fresh_body()
    body["items"].append({"kind": "data", "source": "test/new-data.json"})
    m = load_manifest(_stage(tmp_path, body))
    added = [i for i in m.items if i.source == "test/new-data.json"][0]
    assert added.mode == "0644"


def test_ac_d8_sqlite_mode_defaults_to_0600(tmp_path):
    body = _fresh_body()
    body["items"].append({"kind": "sqlite", "source": "test/new-db.sqlite"})
    m = load_manifest(_stage(tmp_path, body))
    added = [i for i in m.items if i.source == "test/new-db.sqlite"][0]
    assert added.mode == "0600"


def test_ac_d8_blob_mode_defaults_to_0644(tmp_path):
    body = _fresh_body()
    body["items"].append({"kind": "blob", "source": "test/new-blob/"})
    m = load_manifest(_stage(tmp_path, body))
    added = [i for i in m.items if i.source == "test/new-blob/"][0]
    assert added.mode == "0644"


def test_ac_d8_explicit_mode_override(tmp_path):
    body = _fresh_body()
    body["items"].append({"kind": "secret", "source": "test/strict.json", "mode": "0400"})
    m = load_manifest(_stage(tmp_path, body))
    added = [i for i in m.items if i.source == "test/strict.json"][0]
    assert added.mode == "0400"


# ---------- AC-D11 — source_root_mode optional, defaults to 0700 ----------

def test_ac_d11_source_root_mode_defaults_to_0700(tmp_path):
    body = _fresh_body()
    body.pop("source_root_mode", None)
    m = load_manifest(_stage(tmp_path, body))
    assert m.source_root_mode == "0700"


# ---------- AC-D13 — sanitize mode optional, defaults to 0600 ----------

def test_ac_d13_sanitize_mode_defaults_to_0600(tmp_path):
    body = _fresh_body()
    body["sanitize"].append({
        "file": "new-config.json",
        "rules": [{"path": ".secret"}],
    })
    m = load_manifest(_stage(tmp_path, body))
    added = [sf for sf in m.sanitize if sf.file == "new-config.json"][0]
    assert added.mode == "0600"


# ---------- AC-D14 — strict unknown-field rejection at every scope ----------

def test_ac_d14_top_level_unknown_field(tmp_path):
    body = _fresh_body()
    body["typo_field"] = "oops"
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "unknown field 'typo_field' in manifest" in str(exc.value)


def test_ac_d14_top_level_blob_backend_rejected(tmp_path):
    body = _fresh_body()
    body["blob_backend"] = "git-lfs"
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "blob_backend" in str(exc.value)


def test_ac_d14_item_unknown_field(tmp_path):
    body = _fresh_body()
    body["items"].append({"kind": "data", "source": "test/x.json", "extra": 1})
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "unknown field 'extra'" in str(exc.value)


@pytest.mark.parametrize("field", ["pass", "target", "blob_backend"])
def test_ac_d14_item_legacy_fields_rejected(tmp_path, field):
    body = _fresh_body()
    body["items"].append({"kind": "data", "source": "test/y.json", field: "stale"})
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert f"unknown field '{field}'" in str(exc.value)


def test_ac_d14_sanitize_block_unknown_field(tmp_path):
    body = _fresh_body()
    body["sanitize"].append({
        "file": "new.json",
        "rules": [{"path": ".x"}],
        "bogus": 1,
    })
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "unknown field 'bogus'" in str(exc.value)


def test_ac_d14_sanitize_block_legacy_target_rejected(tmp_path):
    body = _fresh_body()
    body["sanitize"].append({
        "file": "new.json",
        "rules": [{"path": ".x"}],
        "target": "stale/path",
    })
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "unknown field 'target'" in str(exc.value)


def test_ac_d14_sanitize_rule_unknown_field(tmp_path):
    body = _fresh_body()
    body["sanitize"].append({
        "file": "new.json",
        "rules": [{"path": ".x", "bogus_rule_key": 1}],
    })
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "unknown field 'bogus_rule_key'" in str(exc.value)


def test_ac_d14_sanitize_rule_legacy_pass_rejected(tmp_path):
    body = _fresh_body()
    body["sanitize"].append({
        "file": "new.json",
        "rules": [{"path": ".x", "pass": "argit/stale"}],
    })
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "unknown field 'pass'" in str(exc.value)


# ---------- AC-D19 — parse-time within-source ambiguity rejection ----------

def test_ac_d19_literal_vs_literal_same_source_kind(tmp_path):
    """Same source + same kind produces identical derived targets."""
    body = _fresh_body()
    body["items"].append({"kind": "data", "source": "agents/main/duplicate.json"})
    body["items"].append({"kind": "data", "source": "agents/main/duplicate.json"})
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "overlapping targets" in str(exc.value)
    assert "agents/main/duplicate.json" in str(exc.value)


# ---------- sanitize file uniqueness (closing spec unresolved-question #4) ----------

def test_duplicate_sanitize_file_rejected(tmp_path):
    body = _fresh_body()
    # Two blocks targeting the same config file — would derive identical
    # sanitize target and identical rule pass paths, a silent-overwrite hazard.
    body["sanitize"].append({
        "file": "openclaw.json",
        "rules": [{"path": ".new.rule"}],
    })
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "duplicate sanitize.file 'openclaw.json'" in str(exc.value)


# ---------- Copilot review: non-object entries in items[]/sanitize[]/rules[] ----------

@pytest.mark.parametrize(
    "bad_entry", [
        "a-plain-string",
        ["not", "an", "object"],
        42,
        None,
    ],
)
def test_non_object_item_entry_rejected(tmp_path, bad_entry):
    body = _fresh_body()
    body["items"].append(bad_entry)
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "expected a JSON object" in str(exc.value)


@pytest.mark.parametrize(
    "bad_entry", ["string-entry", ["list-entry"], 0, None],
)
def test_non_object_sanitize_entry_rejected(tmp_path, bad_entry):
    body = _fresh_body()
    body["sanitize"].append(bad_entry)
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "expected a JSON object" in str(exc.value)


@pytest.mark.parametrize(
    "bad_entry", ["string-rule", ["list-rule"], 0, None],
)
def test_non_object_sanitize_rule_rejected(tmp_path, bad_entry):
    body = _fresh_body()
    body["sanitize"].append({
        "file": "new.json",
        "rules": [bad_entry],
    })
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    assert "expected a JSON object" in str(exc.value)


# ---------- Copilot review: Track D rejects globbed sources ----------
# Track D validates the glob grammar via path_conventions but does not ship
# the expansion pipeline — accepting globs at parse would let an operator
# author an item that silently misbehaves at backup/restore. Track B's PR
# removes this rejection.

@pytest.mark.parametrize(
    "globbed_source,kind", [
        ("agents/*/agent/auth-profiles.json", "secret"),
        ("agents/*/agent/auth-state.json", "data"),
        ("agents/*/", "blob"),
        ("plugins/*/x.sqlite", "sqlite"),
    ],
)
def test_globbed_source_rejected_in_track_d(tmp_path, globbed_source, kind):
    body = _fresh_body()
    body["items"].append({"kind": kind, "source": globbed_source})
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    msg = str(exc.value)
    assert "globs in items[].source not supported" in msg
    assert globbed_source in msg


def test_globbed_source_passes_grammar_check_but_rejected_at_item_level(tmp_path):
    """Defence in depth: path_conventions.validate_glob_source would accept
    `agents/*/x.json` (valid grammar) but _parse_items rejects it with a
    distinct message pointing at release scope, not grammar."""
    body = _fresh_body()
    body["items"].append({"kind": "data", "source": "agents/*/x.json"})
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    msg = str(exc.value)
    # The message distinguishes "grammar invalid" from "release scope":
    assert "not supported in this release" in msg or "globs in items" in msg


# ---------- Copilot review: directory-prefix ambiguity check ----------

def test_ambiguity_dir_source_vs_file_under_it_rejected(tmp_path):
    """Bundled `telegram/` (dir copy) + bundled `telegram/foo.json` (file
    copy) would both write to `openclaw/data/telegram/foo.json` at backup —
    double-handling. Parse-time ambiguity check now catches this via the
    directory-prefix overlap rule."""
    body = _fresh_body()
    body["items"].append({"kind": "data", "source": "plugin-data/"})
    body["items"].append({"kind": "data", "source": "plugin-data/state.json"})
    with pytest.raises(ArgitError) as exc:
        load_manifest(_stage(tmp_path, body))
    msg = str(exc.value)
    assert "overlapping targets" in msg
    assert "plugin-data" in msg

"""Track B — runtime duplicate detection at backup + restore.

ACs: AC-INT5, plus backup-time concrete-source collision checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.manifest import (
    Item,
    Manifest,
    expand_items_for_backup,
    expand_items_for_restore,
)


def _item(kind: str, source: str, origin: str = "bundled") -> Item:
    mode = {"secret": "0600", "data": "0644", "sqlite": "0600", "blob": "0644"}[kind]
    return Item(kind=kind, source=source, mode=mode, target=None, pass_path=None, origin=origin)


def _manifest(items: list[Item], exclude: list[str] | None = None) -> Manifest:
    return Manifest(
        schema_version=1,
        agent_type="openclaw",
        agent_version="2026.4.14",
        manifest_revision=1,
        source_root="/tmp",
        source_root_mode="0700",
        sanitize=[],
        items=items,
        exclude=exclude or [],
        lifecycle=None,
        filename="openclaw-2026.4.14-1.manifest.json",
    )


# ---------- AC-INT5 — bundled glob + overlay literal → same concrete source ----------

def test_int5_bundled_glob_overlay_literal_collide_at_backup(tmp_path):
    """Bundled `agents/*/x.json` expands to `agents/main/x.json`. Overlay
    adds literal `agents/main/x.json`. Both resolve to the same concrete
    source at expansion time → ArgitError naming both origins."""
    (tmp_path / "agents" / "main").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "x.json").write_text("{}")

    manifest = _manifest([
        _item("data", "agents/*/x.json", origin="bundled"),
        _item("data", "agents/main/x.json", origin="overlay"),
    ])
    with pytest.raises(ArgitError) as exc:
        expand_items_for_backup(manifest, tmp_path)
    msg = str(exc.value)
    assert "runtime duplicate" in msg
    assert "agents/main/x.json" in msg
    assert "bundled" in msg
    assert "overlay" in msg


def test_int5_two_globs_expanding_to_same_concrete(tmp_path):
    """Two globs with overlapping coverage → post-expansion collision."""
    (tmp_path / "agents" / "main").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "x.json").write_text("{}")

    manifest = _manifest([
        _item("data", "agents/*/x.json", origin="bundled"),
        _item("data", "*/main/x.json", origin="overlay"),
    ])
    with pytest.raises(ArgitError) as exc:
        expand_items_for_backup(manifest, tmp_path)
    assert "runtime duplicate" in str(exc.value)


# ---------- no collision → expanded list returned ----------

def test_no_collision_returns_expanded_list(tmp_path):
    (tmp_path / "agents" / "main").mkdir(parents=True)
    (tmp_path / "agents" / "erbot").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "x.json").write_text("{}")
    (tmp_path / "agents" / "erbot" / "x.json").write_text("{}")

    manifest = _manifest([
        _item("data", "agents/*/x.json", origin="bundled"),
        _item("data", "plugin/other.json", origin="overlay"),  # disjoint, missing file is OK at expansion
    ])
    # The overlay literal has no file on disk, but expand doesn't filter
    # literals — that's backup phase's warn-on-missing-source.
    result = expand_items_for_backup(manifest, tmp_path)
    sources = sorted(r.source for r in result)
    assert sources == [
        "agents/erbot/x.json",
        "agents/main/x.json",
        "plugin/other.json",
    ]


# ---------- restore-side duplicate (via repo filesystem) ----------

def test_int5_restore_side_collision_via_repo_filesystem(tmp_path):
    """Two globbed items whose repo-side derived targets collide at
    enumeration time."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openclaw" / "data" / "agents" / "main").mkdir(parents=True)
    (repo / "openclaw" / "data" / "agents" / "main" / "x.json").write_text("{}")

    manifest = _manifest([
        _item("data", "agents/*/x.json", origin="bundled"),
        _item("data", "*/main/x.json", origin="overlay"),
    ])
    with pytest.raises(ArgitError) as exc:
        expand_items_for_restore(manifest, repo)
    assert "runtime duplicate at restore" in str(exc.value)


def test_runtime_dup_error_names_both_manifest_files(tmp_path):
    """AC-INT5 docstring promises both origin manifest files are named in
    the error — verify the error includes the bundled filename AND the
    overlay file's name, not just the origin labels."""
    (tmp_path / "agents" / "main").mkdir(parents=True)
    (tmp_path / "agents" / "main" / "x.json").write_text("{}")

    overlay_path = tmp_path / ".argit" / "manifest" / "openclaw-2026.4.14-6.manifest.local.json"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text("{}")

    manifest = Manifest(
        schema_version=1,
        agent_type="openclaw",
        agent_version="2026.4.14",
        manifest_revision=6,
        source_root="/tmp",
        source_root_mode="0700",
        sanitize=[],
        items=[
            _item("data", "agents/*/x.json", origin="bundled"),
            _item("data", "agents/main/x.json", origin="overlay"),
        ],
        exclude=[],
        lifecycle=None,
        filename="openclaw-2026.4.14-6.manifest.json",
        overlay_path=overlay_path,
    )
    with pytest.raises(ArgitError) as exc:
        expand_items_for_backup(manifest, tmp_path)
    msg = str(exc.value)
    assert "openclaw-2026.4.14-6.manifest.json" in msg
    assert "openclaw-2026.4.14-6.manifest.local.json" in msg


def test_restore_non_globbed_items_pass_through(tmp_path):
    """Non-globbed items don't need enumeration — they pass through as-is
    (repo-filesystem presence check is the downstream restore phase's job)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = _manifest([
        _item("data", "identity/device.json", origin="bundled"),
    ])
    result = expand_items_for_restore(manifest, repo)
    assert len(result) == 1
    assert result[0].source == "identity/device.json"

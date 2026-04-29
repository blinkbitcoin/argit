"""Unit tests for review.py's pure functions: generate_review + helpers."""

from __future__ import annotations

from argit.review import WORKSPACE_DOC_URL, generate_review


def test_empty_list_returns_none():
    assert generate_review([], "2026-04-29T12:00:00Z", "openclaw-2026.4.26-1.manifest.json") is None


def test_single_finding_renders_full_report():
    out = generate_review(
        ["plugins/foo.json"],
        "2026-04-29T12:00:00Z",
        "openclaw-2026.4.26-1.manifest.json",
    )
    assert out is not None
    assert out.startswith("# argit review report — 2026-04-29T12:00:00Z\n")
    assert "**Manifest:** `openclaw-2026.4.26-1.manifest.json`" in out
    assert "**Uncovered:** 1 path\n" in out  # singular
    assert "## Uncovered paths" in out
    assert "- `plugins/foo.json`" in out
    assert "## Workspace coexistence" in out
    assert WORKSPACE_DOC_URL in out


def test_multiple_findings_pluralizes():
    out = generate_review(
        ["b.txt", "a.json"],
        "2026-04-29T12:00:00Z",
        "openclaw-2026.4.26-1.manifest.json",
    )
    assert out is not None
    assert "**Uncovered:** 2 paths\n" in out  # plural


def test_findings_are_sorted_for_diff_stability():
    """Output order must be stable (sorted) so consecutive reports diff
    cleanly even when the walker yields paths in different orders."""
    out = generate_review(
        ["zeta.json", "alpha.json", "middle/x.txt"],
        "2026-04-29T12:00:00Z",
        "openclaw-2026.4.26-1.manifest.json",
    )
    assert out is not None
    # Find where the bulleted list lands and read its order.
    lines = out.splitlines()
    paths_idx = lines.index("## Uncovered paths")
    bullets = [ln for ln in lines[paths_idx:] if ln.startswith("- `")]
    assert bullets == ["- `alpha.json`", "- `middle/x.txt`", "- `zeta.json`"]


# ---------- enriched template content ----------


def test_overlay_status_present():
    out = generate_review(
        ["x.json"], "2026-04-29T12:00:00Z", "openclaw-2026.4.26-1.manifest.json",
        overlay_present=True,
    )
    assert out is not None
    assert "(overlay: `openclaw-2026.4.26-1.manifest.local.json` — present)" in out


def test_overlay_status_not_present():
    out = generate_review(
        ["x.json"], "2026-04-29T12:00:00Z", "openclaw-2026.4.26-1.manifest.json",
        overlay_present=False,
    )
    assert out is not None
    assert "(overlay: `openclaw-2026.4.26-1.manifest.local.json` — not present yet)" in out


def test_manifest_grammar_examples_present():
    """Every report includes the manifest-grammar quick reference so an
    agent reading cold has all the syntax it needs."""
    out = generate_review(
        ["x.json"], "2026-04-29T12:00:00Z", "openclaw-2026.4.26-1.manifest.json",
    )
    assert out is not None
    # One example per kind, plus sanitize and exclude.
    assert "### `kind: data`" in out
    assert "### `kind: secret`" in out
    assert "### `kind: sqlite`" in out
    assert "### `kind: blob`" in out
    assert "### `sanitize`" in out
    assert "### `exclude`" in out
    # Concrete fragment snippets (one canonical example per kind).
    assert '"kind": "data"' in out
    assert '"kind": "secret"' in out
    assert '"kind": "sqlite"' in out
    assert '"kind": "blob"' in out


def test_after_editing_section_present():
    out = generate_review(
        ["x.json"], "2026-04-29T12:00:00Z", "openclaw-2026.4.26-1.manifest.json",
    )
    assert out is not None
    assert "## After editing" in out
    assert "Re-run `argit review`" in out

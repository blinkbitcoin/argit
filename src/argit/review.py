"""argit review — emit a markdown report of uncovered paths.

Walks `source_root`, collects paths not matched by `items[]`, `sanitize[]`,
or `exclude[]`, and emits a self-contained markdown report at
`.argit/reviews/<iso>.md`. Read-only — argit never edits the manifest.
The report is informational; the operator/agent decides what to do.

Report shape lives in `src/argit/templates/review-report.md` and is
rendered via stdlib `string.Template` (no third-party templating dep).
The template is shipped in package-data so it lands in the wheel.

Two callers:
  - `argit review` CLI verb (manual)
  - `argit backup` auto-emit hook (when uncovered files exist)
Both invoke `generate_review` (pure function); `run_review` is the
verb's orchestration wrapper.
"""

from __future__ import annotations

import datetime as dt
from importlib import resources
from pathlib import Path
from string import Template

import click

from .errors import ArgitError  # noqa: F401 — re-exported for callers that catch it
from .manifest import load_manifest
from .shared import (
    EXIT_OK,
    acquire_lock,
    check_no_partial_state,
    covered_by_items,
    covered_by_sanitize,
    matches_exclude,
    run_preflight,
    walk_relative,
)


WORKSPACE_DOC_URL = "https://github.com/blinkbitcoin/argit/blob/main/WORKSPACE.md"


def _load_template() -> Template:
    """Load the bundled review-report template via importlib.resources.

    Uses the `argit` package + path-suffix form (NOT `argit.templates`) —
    `templates/` has no `__init__.py` and isn't an importable subpackage
    under setuptools' `packages.find`. Mirrors setup.py's catalog-loading
    workaround.
    """
    text = resources.files("argit").joinpath("templates/review-report.md").read_text(encoding="utf-8")
    return Template(text)


def _overlay_basename(manifest_filename: str) -> str:
    """Strip `.manifest.json` to get the basename used in the overlay
    filename (`<basename>.manifest.local.json`). Same convention as
    manifest._find_overlay."""
    suffix = ".manifest.json"
    if manifest_filename.endswith(suffix):
        return manifest_filename[: -len(suffix)]
    # Defensive — Manifest.filename always carries the suffix per
    # load_manifest, but synthetic test inputs might not.
    return manifest_filename


def collect_uncovered(repo_root: Path, manifest) -> list[str]:  # noqa: ANN001 — Manifest type
    """Walk `manifest.expanded_source_root()` and return relative paths
    NOT matched by `items[]`, `sanitize[]`, or `exclude[]`. Same logic as
    backup.py's phase-2 walk; lifted helpers in shared.py make this
    callable without duplicating the loop."""
    source_root = manifest.expanded_source_root()
    uncovered: list[str] = []
    for rel in walk_relative(source_root):
        if matches_exclude(rel, manifest.exclude):
            continue
        if covered_by_items(rel, manifest.items):
            continue
        if covered_by_sanitize(rel, manifest.sanitize):
            continue
        uncovered.append(str(rel))
    return uncovered


def generate_review(
    uncovered: list[str],
    iso_timestamp: str,
    manifest_filename: str,
    *,
    overlay_present: bool = False,
) -> str | None:
    """Pure function: render the markdown report from the bundled template.
    Returns None when `uncovered` is empty (caller writes nothing).

    The template provides intro + manifest-grammar quick-reference (one
    example per `kind` and for `sanitize` / `exclude`) so a fresh agent
    reading the report cold has everything it needs to act on each path.
    Per-path heuristics + severity classification stay deliberately out
    of scope (see tech-spec-04 §Deliberately omitted).

    Output is sorted bullets for diff-stability across consecutive reports.
    """
    if not uncovered:
        return None
    sorted_paths = sorted(uncovered)
    n = len(sorted_paths)
    plural = "" if n == 1 else "s"
    overlay_basename = _overlay_basename(manifest_filename)
    overlay_status = "present" if overlay_present else "not present yet"
    paths_block = "\n".join(f"- `{p}`" for p in sorted_paths)

    template = _load_template()
    return template.safe_substitute(
        iso=iso_timestamp,
        manifest_filename=manifest_filename,
        overlay_basename=overlay_basename,
        overlay_status=overlay_status,
        count=n,
        plural=plural,
        uncovered_paths=paths_block,
        workspace_doc_url=WORKSPACE_DOC_URL,
    )


def write_review(repo_root: Path, report: str, iso_timestamp: str) -> Path:
    """Write `report` to `.argit/reviews/<iso>.md`, creating parents.
    Returns the written path. Caller decides whether to call this (e.g.,
    skip when `--dry-run` or `report is None`)."""
    out = repo_root / ".argit" / "reviews" / f"{iso_timestamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    return out


def _detect_overlay_present(repo_root: Path, manifest) -> bool:  # noqa: ANN001 — Manifest type
    """Return True if `<basename>.manifest.local.json` exists alongside
    the bundled manifest. Same convention as manifest._find_overlay.
    Read-only filesystem check — caller decides what to do with the
    boolean."""
    if not manifest.filename:
        return False
    overlay_basename = _overlay_basename(manifest.filename)
    overlay_path = repo_root / ".argit" / "manifest" / f"{overlay_basename}.manifest.local.json"
    return overlay_path.is_file()


def run_review(repo_root: Path, *, dry_run: bool = False) -> int:
    """Manual `argit review` verb. Walks source_root, emits report if
    uncovered paths exist, exits 0 either way."""
    check_no_partial_state(repo_root, "review")
    run_preflight(repo_root, require_manifest=True, require_gpg_id=False)
    manifest = load_manifest(repo_root)

    with acquire_lock(repo_root):
        uncovered = collect_uncovered(repo_root, manifest)
        if not uncovered:
            click.echo("✓ no findings — manifest covers source_root completely")
            return EXIT_OK
        iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        overlay_present = _detect_overlay_present(repo_root, manifest)
        report = generate_review(
            uncovered, iso, manifest.filename, overlay_present=overlay_present,
        )
        if report is None:
            # Defensive: collect_uncovered returned non-empty but generator
            # returned None — only possible if the empty-check in
            # generate_review ever diverges from collect_uncovered's. No-op.
            return EXIT_OK
        if dry_run:
            click.echo(f"would: write .argit/reviews/{iso}.md ({len(uncovered)} findings)")
            return EXIT_OK
        out = write_review(repo_root, report, iso)
        click.echo(f"✓ review: {out.relative_to(repo_root)} ({len(uncovered)} findings)")
        return EXIT_OK

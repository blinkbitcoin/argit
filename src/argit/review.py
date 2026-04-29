"""argit review — emit a flat list of uncovered paths as markdown.

Walks `source_root`, collects paths not matched by `items[]`, `sanitize[]`,
or `exclude[]`, and emits the list as a markdown report at
`.argit/reviews/<iso>.md`. Read-only — argit never edits the manifest.
The report is informational; the operator/agent decides what to do.

Two callers:
  - `argit review` CLI verb (manual)
  - `argit backup` auto-emit hook (when uncovered files exist)
Both invoke `generate_review` (pure function); `run_review` is the
verb's orchestration wrapper.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click

from .errors import ArgitError
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
) -> str | None:
    """Pure function: render the markdown report. Returns None when
    `uncovered` is empty (caller writes nothing in that case).

    Deliberately flat — no per-finding heuristics, severity, or suggested
    manifest fragments. The reader/agent decides what to do with each
    path. Format optimized for diff-readability across consecutive
    reports: one bullet per uncovered path; stable sort.
    """
    if not uncovered:
        return None
    sorted_paths = sorted(uncovered)
    n = len(sorted_paths)
    plural = "" if n == 1 else "s"
    lines = [
        f"# argit review report — {iso_timestamp}",
        "",
        f"- **Backup:** `{iso_timestamp}`",
        f"- **Manifest:** `{manifest_filename}`",
        f"- **Uncovered:** {n} path{plural}",
        "",
        "The paths below exist under `source_root` but are not matched by any",
        "`items[]`, `sanitize[]`, or `exclude[]` rule in the manifest. Decide",
        "what to do with each: extend the `<basename>.manifest.local.json`",
        "overlay (per AGENTS.md, never modify the bundled manifest) or add an",
        "`exclude[]` pattern for noise.",
        "",
        "## Uncovered paths",
        "",
    ]
    lines.extend(f"- `{p}`" for p in sorted_paths)
    lines.extend([
        "",
        "## Workspace coexistence",
        "",
        f"If you maintain a separate git-backed workspace directory (e.g., `~/workspace`",
        f"referenced by `openclaw.json.workspace`), see [WORKSPACE.md]({WORKSPACE_DOC_URL})",
        f"for the recommended layout.",
        "",
    ])
    return "\n".join(lines)


def write_review(repo_root: Path, report: str, iso_timestamp: str) -> Path:
    """Write `report` to `.argit/reviews/<iso>.md`, creating parents.
    Returns the written path. Caller decides whether to call this (e.g.,
    skip when `--dry-run` or `report is None`)."""
    out = repo_root / ".argit" / "reviews" / f"{iso_timestamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    return out


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
        report = generate_review(uncovered, iso, manifest.filename)
        if report is None:
            # Defensive: collect_uncovered returned non-empty but generator
            # returned None — would only happen if generate_review's empty
            # check ever diverges from collect_uncovered's. Treat as no-op.
            return EXIT_OK
        if dry_run:
            click.echo(f"would: write .argit/reviews/{iso}.md ({len(uncovered)} findings)")
            return EXIT_OK
        out = write_review(repo_root, report, iso)
        click.echo(f"✓ review: {out.relative_to(repo_root)} ({len(uncovered)} findings)")
        return EXIT_OK

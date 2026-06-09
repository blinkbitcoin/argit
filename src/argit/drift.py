"""argit drift — machine-readable bundled-vs-in-repo manifest drift report.

The `_handle_drift` flow in setup.py surfaces drift only as human status
lines (the `= manifest drift: …` text), which automation can't reliably
consume (see issue #25). This module exposes a read-only, hash-only drift
report — a stable JSON object for fleet drift monitoring, plus a one-line
human summary that mirrors setup's phrasing for the common rev-bump case.

Crucially this compares the repo manifest against the *selected bundled*
manifest, NOT (as `setup._classify_drift` does) against the latest revision
within the repo manifest's own `agent_version` family. The two answer
different questions: `setup` asks "is an in-place same-family rev-bump
available?"; `drift` asks "does the pinned manifest match the current
bundled best-fit?" — so a repo pinned to the newest revision of an *older*
version family (e.g. openclaw-2026.4.14-7) while the bundled best-fit has
moved to a newer family (openclaw-2026.4.26-2) is correctly reported as
`stale_bundle`, not `clean`.

Read-only and non-mutating, like `doctor` / `info` / `review`. The shared
`collect_drift` core is also consumed by `doctor`.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .errors import ArgitError
from .hashing import canonical_hash
from .manifest import parse_filename
from .setup import _bundled_manifest_path, _load_hash_catalog
from .shared import EXIT_OK, probe_agent_version

DRIFT_SCHEMA = "argit.drift/v1"


def collect_drift(repo_root: Path, *, agent_type: str = "openclaw") -> dict:
    """Classify bundled-vs-in-repo manifest drift. Pure hash-only — never
    invokes load_manifest, so a grammar-incompatible repo manifest still
    classifies (the F2 invariant the upgrade path relies on).

    Raises ArgitError when the repo manifest targets a different agent than
    requested (mirrors `setup._handle_drift`'s first-touch behavior).

    States:
      clean             — repo manifest IS the selected bundled manifest.
      stale_bundle      — repo manifest is a known (catalog) manifest, but
                          NOT the selected bundled one → an upgrade exists.
      operator_modified — repo manifest matches nothing in the catalog.
      no_manifest       — no manifest installed yet.
    """
    # Probe the live agent version so the selected bundled manifest is the
    # same best-fit `setup` would pick (operators on an older agent shouldn't
    # be pointed at a manifest targeting a newer schema they can't yet use).
    agent_version = probe_agent_version("openclaw") if agent_type == "openclaw" else None

    manifest_dir = repo_root / ".argit" / "manifest"
    existing = sorted(manifest_dir.glob("*.manifest.json")) if manifest_dir.is_dir() else []

    bundled = _bundled_manifest_path(agent_type=agent_type, agent_version=agent_version)
    _, bundled_ver, bundled_rev = parse_filename(bundled.name)

    payload: dict = {
        "schema": DRIFT_SCHEMA,
        "agent_type": agent_type,
        "agent_version": agent_version,
        "manifest_file": None,
        "repo_agent_version": None,
        "repo_revision": None,
        "state": "no_manifest",
        "bundled_manifest_file": bundled.name,
        "bundled_agent_version": bundled_ver,
        "bundled_revision": bundled_rev,
        "revisions_behind": None,
        "upgrade_available": False,
    }

    if not existing:
        return payload

    repo_manifest_path = existing[0]
    existing_type, _, _ = parse_filename(repo_manifest_path.name)
    if existing_type != agent_type:
        raise ArgitError(
            f"repo manifest is for {existing_type}, but drift was requested for {agent_type}",
            "use the matching --agent-type or query a separate backup repo for this agent",
        )
    payload["manifest_file"] = repo_manifest_path.name

    # Identify the repo manifest by hash against the SELECTED bundled manifest
    # and the catalog — comparing content, not filenames (an operator could
    # rename a file). `canonical_hash` raises ArgitError on malformed JSON.
    repo_digest = canonical_hash(repo_manifest_path)

    if repo_digest == canonical_hash(bundled):
        payload["state"] = "clean"
        payload["repo_agent_version"] = bundled_ver
        payload["repo_revision"] = bundled_rev
        payload["revisions_behind"] = 0
        return payload

    # Reverse-lookup the repo digest in the shipped catalog to recover the
    # matched manifest's true (version, revision).
    matched = None  # (agent_version, revision)
    for name, digest in _load_hash_catalog().items():
        if digest != repo_digest:
            continue
        try:
            mtype, mver, mrev = parse_filename(name)
        except ArgitError:
            continue  # catalog entry with a non-conforming name — skip
        if mtype == agent_type:
            matched = (mver, mrev)
            break

    if matched is None:
        payload["state"] = "operator_modified"
        return payload

    matched_ver, matched_rev = matched
    payload["state"] = "stale_bundle"
    payload["repo_agent_version"] = matched_ver
    payload["repo_revision"] = matched_rev
    payload["upgrade_available"] = True
    # `revisions_behind` only has meaning within one agent_version family;
    # across families (e.g. 2026.4.14 → 2026.4.26) revision numbers reset and
    # aren't comparable, so leave it null and let consumers diff the versions.
    if matched_ver == bundled_ver:
        payload["revisions_behind"] = bundled_rev - matched_rev
    return payload


def human_summary(payload: dict) -> str:
    """One-line summary. Mirrors setup's `= manifest drift: …` phrasing for
    the common same-family rev-bump; cross-family stale prints both filenames
    (revision numbers alone would be ambiguous across version families)."""
    state = payload["state"]
    name = payload["manifest_file"]
    if state == "no_manifest":
        return "= no manifest installed (run `argit setup`)"
    if state == "clean":
        return f"= manifest drift: clean ({name})"
    if state == "operator_modified":
        return (
            f"= manifest drift: operator-modified ({name}). Leaving alone. If you "
            f"intended operator extensions, move them to `.manifest.local.json` — "
            f"see MANIFEST.md §Overlay."
        )
    # stale_bundle
    if payload["revisions_behind"] is not None:
        return (
            f"= manifest drift: stale bundle (rev {payload['repo_revision']} → "
            f"{payload['bundled_revision']} available)"
        )
    return (
        f"= manifest drift: stale bundle ({name} → "
        f"{payload['bundled_manifest_file']} available)"
    )


def run_drift(repo_root: Path, *, as_json: bool = False, agent_type: str = "openclaw") -> int:
    """Report manifest drift. Always exits 0 for any classified state — drift
    is a queryable condition, not a failure (issue #25). Only genuine errors
    (agent-type mismatch, malformed catalog) raise ArgitError → first-touch."""
    payload = collect_drift(repo_root, agent_type=agent_type)
    if as_json:
        click.echo(json.dumps(payload))
    else:
        click.echo(human_summary(payload))
    return EXIT_OK

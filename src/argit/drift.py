"""argit drift — machine-readable bundled-vs-in-repo manifest drift report.

The `_handle_drift` flow in setup.py surfaces drift only as human status
lines (the `= manifest drift: …` text), which automation can't reliably
consume (see issue #25). This module exposes the SAME hash-only
classification (`setup._classify_drift`) as a read-only report — a stable
JSON object for fleet drift monitoring, and a one-line human summary that
mirrors setup's existing phrasing.

Read-only and non-mutating, like `doctor` / `info` / `review`. The shared
`collect_drift` core is also consumed by `doctor` so the two never disagree.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .errors import ArgitError
from .manifest import parse_filename
from .setup import _bundled_manifest_path, _classify_drift
from .shared import EXIT_OK, probe_agent_version

DRIFT_SCHEMA = "argit.drift/v1"


def collect_drift(repo_root: Path, *, agent_type: str = "openclaw") -> dict:
    """Classify bundled-vs-in-repo manifest drift. Pure hash-only — never
    invokes load_manifest, so a grammar-incompatible repo manifest still
    classifies (the F2 invariant the upgrade path relies on).

    Raises ArgitError when the repo manifest targets a different agent than
    requested (mirrors `setup._handle_drift`'s first-touch behavior).

    States: clean | stale_bundle | operator_modified | no_manifest.
    """
    # Probe the live agent version so `bundled_revision` reflects the same
    # best-fit manifest `setup` would pick (operators on an older agent
    # shouldn't be told they're "behind" a manifest targeting a newer schema).
    agent_version = probe_agent_version("openclaw") if agent_type == "openclaw" else None

    manifest_dir = repo_root / ".argit" / "manifest"
    existing = sorted(manifest_dir.glob("*.manifest.json")) if manifest_dir.is_dir() else []

    bundled = _bundled_manifest_path(agent_type=agent_type, agent_version=agent_version)
    _, _, bundled_rev = parse_filename(bundled.name)

    payload: dict = {
        "schema": DRIFT_SCHEMA,
        "agent_type": agent_type,
        "agent_version": agent_version,
        "manifest_file": None,
        "state": "no_manifest",
        "repo_revision": None,
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

    drift, matched_rev = _classify_drift(repo_manifest_path)
    payload["manifest_file"] = repo_manifest_path.name
    payload["state"] = drift

    if drift == "clean":
        payload["repo_revision"] = matched_rev if matched_rev is not None else bundled_rev
        payload["revisions_behind"] = 0
    elif drift == "stale_bundle":
        payload["repo_revision"] = matched_rev
        payload["revisions_behind"] = bundled_rev - matched_rev if matched_rev is not None else None
        payload["upgrade_available"] = True
    # operator_modified: repo_revision/revisions_behind stay null; no upgrade.

    return payload


def human_summary(payload: dict) -> str:
    """One-line summary mirroring setup's `= manifest drift: …` phrasing."""
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
    return (
        f"= manifest drift: stale bundle (rev {payload['repo_revision']} → "
        f"{payload['bundled_revision']} available)"
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

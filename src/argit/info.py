"""argit info — emit argit's bundled-resource locations + metadata.

Read-only, repo-independent, and dependency-free: `info` resolves paths
inside argit's own interpreter, so it works identically under pipx, uv,
pip --user, and source installs without the caller reconstructing
install-layout-specific paths. Deliberately does NOT shell out to gpg —
it must run on minimal hosts. The bundled IT-backup key's fingerprint/uid
are surfaced from the in-package constants; the `it_backup_pubkey` path
lets a paranoid consumer recompute the file's real fingerprint themselves.

Two output modes:
  - human-readable (default) — aligned key: value lines
  - `--json` — a stable machine-readable object (the integration use case)
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import click

from . import __version__
from .shared import EXIT_OK, IT_BACKUP_FPR, IT_BACKUP_UID


def _pkg_path(suffix: str) -> Path:
    """Resolve a path inside the installed `argit` package.

    Addresses via the known-importable `argit` package + a path suffix
    rather than via `argit.keys` / `argit.manifest_templates` — those
    dirs have no `__init__.py` and aren't importable subpackages under
    setuptools' `packages.find`, so they're not guaranteed resolvable
    after `pip install`. Same workaround as setup.py / review.py.
    """
    return Path(str(resources.files("argit").joinpath(suffix)))


def collect_info() -> dict:
    """Build the info payload. Pure — no I/O beyond path resolution +
    directory listing of bundled manifests."""
    pkg_root = _pkg_path("")
    it_key = _pkg_path("keys/it-backup-pubkey.asc")
    manifest_dir = _pkg_path("manifest_templates")
    hashes = _pkg_path("manifest_templates/hashes.json")

    templates = (
        sorted(p.name for p in manifest_dir.iterdir() if p.name.endswith(".manifest.json"))
        if manifest_dir.is_dir()
        else []
    )

    return {
        "version": __version__,
        "package_root": str(pkg_root),
        "resources": {
            "it_backup_pubkey": str(it_key),
            "manifest_templates_dir": str(manifest_dir),
            "hashes_catalog": str(hashes),
        },
        "it_backup_key": {
            "fingerprint": IT_BACKUP_FPR,
            "uid": IT_BACKUP_UID,
        },
        "manifest_templates": templates,
    }


def _render_human(info: dict) -> str:
    res = info["resources"]
    it = info["it_backup_key"]
    lines = [
        f"argit {info['version']}",
        f"package root:           {info['package_root']}",
        f"it-backup pubkey:       {res['it_backup_pubkey']}",
        f"manifest templates dir: {res['manifest_templates_dir']}",
        f"hashes catalog:         {res['hashes_catalog']}",
        f"it-backup fingerprint:  {it['fingerprint']}",
        f"it-backup uid:          {it['uid']}",
        f"bundled manifests:      {len(info['manifest_templates'])}",
    ]
    return "\n".join(lines)


def run_info(as_json: bool = False) -> int:
    """Emit argit's resource locations. Repo-independent — argit need not
    be run from inside a backup repo."""
    info = collect_info()
    if as_json:
        click.echo(json.dumps(info, indent=2))
    else:
        click.echo(_render_human(info))
    return EXIT_OK

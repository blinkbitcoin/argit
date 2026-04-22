#!/usr/bin/env python3
"""Rebuild src/argit/manifest_templates/hashes.json.

Reads every `*.manifest.json` under `src/argit/manifest_templates/`,
computes `hashing.canonical_hash` for each, writes the result as a sorted
`{filename: hex_digest}` mapping.

Default: dry-run with a structured +/~/- diff against the committed catalog;
exit 1 on drift. `--write` commits the new catalog.

CI contract (spec §Dependencies): this script runs without `--write` and
fails the job on exit 1, guaranteeing the committed catalog matches the
shipped manifest set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the in-repo src/ importable without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from argit.hashing import canonical_hash  # noqa: E402


MANIFEST_DIR = _REPO_ROOT / "src" / "argit" / "manifest_templates"
CATALOG_PATH = MANIFEST_DIR / "hashes.json"


def _compute_catalog() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(MANIFEST_DIR.glob("*.manifest.json")):
        out[p.name] = canonical_hash(p)
    return out


def _load_committed_catalog() -> dict[str, str]:
    if not CATALOG_PATH.is_file():
        return {}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _diff(committed: dict[str, str], computed: dict[str, str]) -> list[str]:
    lines: list[str] = []
    all_keys = sorted(set(committed) | set(computed))
    for k in all_keys:
        if k not in committed:
            lines.append(f"+ {k}: {computed[k]}  (new)")
        elif k not in computed:
            lines.append(f"- {k}: {committed[k]}  (removed)")
        elif committed[k] != computed[k]:
            lines.append(f"~ {k}: {committed[k]} → {computed[k]}  (changed)")
    return lines


def _write_catalog(catalog: dict[str, str]) -> None:
    # Committed catalog is pretty-printed for human diff readability; its
    # internal ordering is deterministic (sort_keys). The canonical-hash
    # algorithm is decoupled from this formatting (see hashing.py) so editor
    # autoformat of hashes.json itself doesn't matter — we only consume
    # parsed dict keys.
    body = json.dumps(catalog, sort_keys=True, indent=2) + "\n"
    CATALOG_PATH.write_text(body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--write", action="store_true",
                   help="Write the computed catalog to hashes.json. Default: dry-run + diff.")
    args = p.parse_args(argv)

    computed = _compute_catalog()
    committed = _load_committed_catalog()

    if args.write:
        _write_catalog(computed)
        try:
            rel = CATALOG_PATH.relative_to(_REPO_ROOT)
        except ValueError:
            rel = CATALOG_PATH  # tests may monkey-patch CATALOG_PATH outside the repo
        print(f"wrote {len(computed)} entries to {rel}")
        return 0

    diff = _diff(committed, computed)
    if not diff:
        print(f"hashes.json in sync ({len(computed)} entries)")
        return 0

    print("hashes.json is stale. Run `python scripts/rebuild_hash_catalog.py --write` to update.")
    for line in diff:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())

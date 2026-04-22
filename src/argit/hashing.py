"""Canonical-hash helper for Track A drift detection.

The canonical form is **decoupled from the committed on-disk form**: editor
autoformat, BOM prefixes, trailing whitespace, and key reordering must NOT
produce false drift. Canonicalization:

  1. Read bytes as `utf-8-sig` (silently strips a leading UTF-8 BOM).
  2. Parse via `json.loads`.
  3. Re-serialize via `json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
  4. SHA-256 the resulting `.encode("utf-8")` bytes.
  5. Return hex digest.

`ensure_ascii=True` is locked — non-ASCII always escapes to `\\uXXXX`, so
determinism holds across platforms with different default locales.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import ArgitError


def canonical_hash(path: Path) -> str:
    """Return SHA-256 hex digest of the canonicalized manifest at `path`.

    Raises ArgitError on unreadable path or invalid JSON with first-touch
    diagnosis + remediation.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ArgitError(
            f"canonical_hash: file not found: {path}",
            "verify the path exists and is readable",
        ) from exc
    except OSError as exc:
        raise ArgitError(
            f"canonical_hash: cannot read {path}: {exc}",
            f"check permissions (chmod +r {path})",
        ) from exc
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArgitError(
            f"canonical_hash: {path.name} is not valid JSON: {exc.msg} (line {exc.lineno})",
            "fix the JSON syntax; argit uses stdlib json — strict double-quoted keys, no trailing commas",
        ) from exc
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

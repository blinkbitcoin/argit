"""Path derivation conventions bound to schema_version: 1.

Pure-function helpers: every call site in manifest.py / backup.py / restore.py
/ setup.py uses these helpers so the convention lives in exactly one place.

Convention table (see MANIFEST.md §Path conventions):

  sanitize[].file                 → sanitize[].target       = <agent_type>/config/<file>
  sanitize[].file + rules[].path  → rules[].pass            = argit/<agent_type>/<file_stem>/<segments>
  items[] kind: secret + source   → items[].pass            = argit/<agent_type>/<source_sans_.json>
  items[] kind: data|sqlite|blob  → items[].target          = <agent_type>/<kind>/<source>
"""

from __future__ import annotations

import re

from .errors import ArgitError

BLOB_BACKEND = "git-lfs"

LFS_MANAGED_KINDS = ("blob", "sqlite")
LFS_LINE_TEMPLATE = "{agent_type}/{kind}/** filter=lfs diff=lfs merge=lfs -text"
LFS_PATTERN_TEMPLATE = "{agent_type}/{kind}/**"


def lfs_lines(agent_type: str) -> list[str]:
    return [LFS_LINE_TEMPLATE.format(agent_type=agent_type, kind=kind) for kind in LFS_MANAGED_KINDS]


def lfs_patterns(agent_type: str) -> list[str]:
    return [LFS_PATTERN_TEMPLATE.format(agent_type=agent_type, kind=kind) for kind in LFS_MANAGED_KINDS]

_KIND_DEFAULT_MODE = {
    "secret": "0600",
    "data": "0644",
    "sqlite": "0600",
    "blob": "0644",
}

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def default_mode(kind: str) -> str:
    if kind not in _KIND_DEFAULT_MODE:
        raise ArgitError(
            f"default_mode: unknown kind {kind!r}",
            f"use one of: {sorted(_KIND_DEFAULT_MODE)}",
        )
    return _KIND_DEFAULT_MODE[kind]


def _camel_to_kebab(s: str) -> str:
    return _CAMEL_SPLIT_RE.sub("-", s).lower()


def derive_sanitize_target(agent_type: str, file: str) -> str:
    return f"{agent_type}/config/{file}"


def derive_pass(agent_type: str, file: str, dotted_path: str) -> str:
    """Sanitize-rule pass path.

    `dotted_path` like `.channels.telegram.botToken` → segments
    `["channels", "telegram", "bot-token"]`. Leading `.` stripped; each
    segment has camelCase split to kebab via `[a-z0-9](?=[A-Z])`. Result:
    `argit/<agent_type>/<file_stem>/<segments joined with "/">`.

    `file_stem` is the filename without `.json` (kept even when it equals
    `agent_type` — dumb-uniform rule, no special-case elision).
    """
    stripped = dotted_path.lstrip(".")
    segments = [_camel_to_kebab(seg) for seg in stripped.split(".") if seg]
    file_stem = file[: -len(".json")] if file.endswith(".json") else file
    joined = "/".join(segments)
    return f"argit/{agent_type}/{file_stem}/{joined}"


def derive_item_pass(agent_type: str, source: str) -> str:
    """kind: secret — `argit/<agent_type>/<source_sans_.json>` (path seps preserved)."""
    stem = source[: -len(".json")] if source.endswith(".json") else source
    return f"argit/{agent_type}/{stem}"


def derive_item_target(agent_type: str, kind: str, source: str) -> str:
    """kind: data|sqlite|blob — `<agent_type>/<kind>/<source>` (literal concat).

    Path separators preserved; trailing `/` for dir sources preserved.
    `kind: secret` has no target (pass_path only).
    """
    if kind == "secret":
        raise ArgitError(
            "derive_item_target: kind=secret has no target (use derive_item_pass)",
            "call derive_item_pass for kind=secret items",
        )
    if kind not in ("data", "sqlite", "blob"):
        raise ArgitError(
            f"derive_item_target: unknown kind {kind!r}",
            "use one of: data, sqlite, blob (or derive_item_pass for secret)",
        )
    return f"{agent_type}/{kind}/{source}"


def invert_item_target(agent_type: str, kind: str, target: str) -> str:
    """Reverse of derive_item_target — `target` MUST be a concrete path (no `*`).

    Callers enumerate concrete matches via Path.glob(pattern) FIRST and call
    this on each concrete result. Because forward derivation preserves path
    separators for every kind, the inverse is a trivial prefix strip.
    """
    if "*" in target:
        raise ArgitError(
            "invert_item_target received a glob pattern, not a concrete target",
            "enumerate matches first via Path.glob then invert each concrete result",
        )
    prefix = f"{agent_type}/{kind}/"
    if not target.startswith(prefix):
        raise ArgitError(
            f"invert_item_target: target {target!r} does not start with {prefix!r}",
            "verify the target was produced by derive_item_target for this agent_type/kind",
        )
    return target[len(prefix):]


def validate_glob_source(source: str) -> None:
    """Apply the grammar rules from tech-spec §Globs in items:

    - `*` only as a whole path component (regex `[^/]+`).
    - `**` (crossing separators) rejected.
    - `*` inside a filename rejected (no `foo*.json`, `*.json`, `foo-*-bar.json`).
    - Multi-`*` across components allowed.
    - Leading / trailing `*` component allowed.

    Non-globbed sources return without error. Raises ArgitError naming the
    offending component + rule.
    """
    if "*" not in source:
        return
    if "**" in source:
        raise ArgitError(
            f"source {source!r} contains '**' — cross-separator glob not supported",
            "use single-component '*' only; see MANIFEST.md §Globs in items",
        )
    components = source.split("/")
    for comp in components:
        if "*" not in comp:
            continue
        if comp != "*":
            raise ArgitError(
                f"source {source!r}: component {comp!r} mixes '*' with other characters — "
                "glob is only valid as a whole path component",
                "replace the component with bare '*' or use a literal source; "
                "see MANIFEST.md §Globs in items",
            )


def targets_overlap(source_a: str, source_b: str) -> bool:
    """Component-wise overlap check for two item sources, with
    directory-prefix semantics.

    Two sources overlap iff their shared path prefix is component-wise
    compatible (equal OR at-least-one-is-`*` at every shared position) AND
    EITHER:
      - they have the same length (equal-depth overlap: literal dup, or a
        `*` component hitting a literal), OR
      - the shorter source is a directory (ends with `/`) and so is a
        prefix of the longer source.

    This handles the real-world backup-time collision where a dir source
    like `telegram/` and a file source like `telegram/foo.json` BOTH
    forward-derive into nested paths under the same repo target tree —
    `copytree` of the dir plus explicit-copy of the file would double-write
    the same file.

    Literal-literal at identical paths → True.
    Literal vs star at same depth → True.
    `telegram/` vs `telegram/foo.json` → True (dir-prefix overlap).
    `telegram/` vs `other/foo.json` → False (prefixes diverge at position 0).
    `a/b/c` vs `a/b/c/d` where neither ends in `/` → False (neither is a dir
    prefix of the other; literal file at `a/b/c` can't contain files).
    """
    norm_a = source_a.rstrip("/")
    norm_b = source_b.rstrip("/")
    a_is_dir = source_a.endswith("/")
    b_is_dir = source_b.endswith("/")

    comps_a = norm_a.split("/") if norm_a else []
    comps_b = norm_b.split("/") if norm_b else []

    shared = min(len(comps_a), len(comps_b))
    for ca, cb in zip(comps_a[:shared], comps_b[:shared]):
        if ca == cb:
            continue
        if ca == "*" or cb == "*":
            continue
        return False

    if len(comps_a) == len(comps_b):
        return True
    shorter_is_dir = a_is_dir if len(comps_a) < len(comps_b) else b_is_dir
    return shorter_is_dir

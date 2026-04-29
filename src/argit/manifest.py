"""Manifest loader + validator.

Locates `.argit/manifest/*.manifest.json` (exactly one), parses it with stdlib
`json`, and validates structure + filename↔body coherence. Returns a typed
`Manifest` dataclass.

Path derivation for `items[].pass` / `items[].target` / `sanitize[].target` /
`sanitize.rules[].pass` is handled by `path_conventions.py` — this module
populates the dataclass fields from the conventions rather than reading them
from the manifest. Explicit `pass`/`target` in the manifest is rejected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import path_conventions
from .errors import ArgitError
from .shared import matches_exclude

VALID_KINDS = {"secret", "data", "sqlite", "blob"}
SUPPORTED_SCHEMA_VERSION = 1
INSTALL_HINT = (
    "upgrade argit: curl -fsSL https://raw.githubusercontent.com/blinkbitcoin/argit/main/install.sh | bash"
)
DEFAULT_SOURCE_ROOT_MODE = "0700"
DEFAULT_SANITIZE_MODE = "0600"

_ALLOWED_TOP_LEVEL = {
    "schema_version", "agent_type", "agent_version", "manifest_revision",
    "source_root", "source_root_mode", "sanitize", "items", "exclude",
    "lifecycle",
}
_ALLOWED_ITEM_KEYS = {"kind", "source", "mode"}
_ALLOWED_SANITIZE_KEYS = {"file", "mode", "rules"}
_ALLOWED_RULE_KEYS = {"path", "subtree"}


@dataclass(frozen=True)
class SanitizeRule:
    path: str
    pass_path: str
    subtree: bool = False


@dataclass(frozen=True)
class SanitizeFile:
    file: str
    target: str
    mode: str
    rules: list[SanitizeRule]
    origin: str = "bundled"


@dataclass(frozen=True)
class Item:
    kind: str
    source: str
    mode: str
    target: str | None = None
    pass_path: str | None = None
    origin: str = "bundled"

    @property
    def is_dir_source(self) -> bool:
        return self.source.endswith("/")

    @property
    def is_globbed(self) -> bool:
        return "*" in self.source


@dataclass(frozen=True)
class LifecycleCommand:
    description: str
    command: list[str]
    running_exit_code: int = 0
    timeout_sec: int = 30
    poll_interval_ms: int = 500


@dataclass(frozen=True)
class Lifecycle:
    detect_running: LifecycleCommand | None = None
    stop: LifecycleCommand | None = None
    start: LifecycleCommand | None = None


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    agent_type: str
    agent_version: str
    manifest_revision: int
    source_root: str
    source_root_mode: str
    sanitize: list[SanitizeFile]
    items: list[Item]
    exclude: list[str]
    lifecycle: Lifecycle | None = None
    filename: str = ""
    overlay_path: Path | None = None

    @property
    def blob_backend(self) -> str:
        """Only git-lfs is supported under schema_version: 1."""
        return path_conventions.BLOB_BACKEND

    def expanded_source_root(self) -> Path:
        return Path(self.source_root).expanduser()


# Filename: split on the LAST `-` before `.manifest.json` to extract revision,
# allowing agent versions with internal dashes (e.g. Debian "2026.3.23-2").
_FILENAME_RE = re.compile(r"^(?P<type>[a-z][a-z0-9_-]*?)-(?P<ver>.+)-(?P<rev>\d+)\.manifest\.json$")


def parse_filename(name: str) -> tuple[str, str, int]:
    m = _FILENAME_RE.match(name)
    if not m:
        raise ArgitError(
            f"manifest filename '{name}' does not match <agent-type>-<agent-version>-<revision>.manifest.json",
            "rename the manifest file to follow the convention; see MANIFEST.md",
        )
    return m["type"], m["ver"], int(m["rev"])


def _normalize_mode(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArgitError(
            f"{field_name}: mode must be a string (got {type(value).__name__})",
            "use a 3- or 4-digit octal string like \"0600\"",
        )
    s = value.strip()
    if not re.fullmatch(r"0?[0-7]{3}", s):
        raise ArgitError(
            f"{field_name}: invalid octal mode '{value}'",
            "use a 3- or 4-digit octal string like \"0600\" or \"700\"",
        )
    return s if len(s) == 4 else "0" + s


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ArgitError(f"{where}: missing required field '{key}'", f"add '{key}' to {where}")
    return d[key]


def _check_unknown_keys(d: dict, allowed: set[str], where: str) -> None:
    for key in d:
        if key not in allowed:
            raise ArgitError(
                f"unknown field '{key}' in {where}",
                f"remove it; allowed fields in {where}: {sorted(allowed)} — see MANIFEST.md",
            )


def _parse_lifecycle_cmd(d: dict, where: str) -> LifecycleCommand:
    desc = _require(d, "description", where)
    cmd = _require(d, "command", where)
    if not isinstance(cmd, list) or len(cmd) == 0 or not all(isinstance(x, str) for x in cmd):
        raise ArgitError(
            f"{where}.command must be a non-empty list of strings",
            f"set {where}.command to an argv list, e.g. [\"sh\", \"-c\", \"...\"]",
        )
    return LifecycleCommand(
        description=str(desc),
        command=list(cmd),
        running_exit_code=int(d.get("running_exit_code", 0)),
        timeout_sec=int(d.get("timeout_sec", 30)),
        poll_interval_ms=int(d.get("poll_interval_ms", 500)),
    )


def _parse_lifecycle(d: dict | None) -> Lifecycle | None:
    if d is None:
        return None
    if not isinstance(d, dict):
        raise ArgitError("manifest.lifecycle must be an object", "see MANIFEST.md §Lifecycle")
    return Lifecycle(
        detect_running=_parse_lifecycle_cmd(d["detect_running"], "lifecycle.detect_running")
        if "detect_running" in d
        else None,
        stop=_parse_lifecycle_cmd(d["stop"], "lifecycle.stop") if "stop" in d else None,
        start=_parse_lifecycle_cmd(d["start"], "lifecycle.start") if "start" in d else None,
    )


def _validate_sanitize_path(path: str, loc: str) -> None:
    """Sanitize-path grammar — narrow wildcard support.

      - `*` MUST be a whole dotted segment (reject `foo*`, `*bar`, `f*o`).
      - At most one `*` per path.
      - `*` MUST NOT be the first segment (top-level wildcard would
        fan out across every key in the file — explicit subtree:true is the
        right tool for that and avoids accidental over-sanitization).

    Wildcard at the LAST segment is allowed: `.foo.*` with subtree:true means
    "every value under foo is its own subtree pass entry".
    """
    if "*" not in path:
        return
    segments = path.lstrip(".").split(".")
    star_count = sum(1 for s in segments if s == "*")
    raw_star_segments = sum(1 for s in segments if "*" in s)
    if raw_star_segments != star_count:
        raise ArgitError(
            f"{loc}.path '{path}': '*' must be a whole segment, not part of one",
            "use '.foo.*.bar' (whole-segment wildcard); '.foo*'/'.f*o' are not supported",
        )
    if star_count > 1:
        raise ArgitError(
            f"{loc}.path '{path}': at most one '*' segment per path",
            "split into multiple rules; nested wildcards are not supported",
        )
    if segments[0] == "*":
        raise ArgitError(
            f"{loc}.path '{path}': '*' may not be the first segment",
            "anchor the path with a literal first segment; use a kind:secret on the whole file for top-level fan-out",
        )


def _parse_sanitize_rules(
    rules: list, where: str, agent_type: str, file: str, source_label: str
) -> list[SanitizeRule]:
    if not isinstance(rules, list) or len(rules) == 0:
        raise ArgitError(f"{where}: rules must be a non-empty list", "add at least one rule")
    out: list[SanitizeRule] = []
    for i, r in enumerate(rules):
        loc = f"{where}.rules[{i}]"
        if not isinstance(r, dict):
            raise ArgitError(
                f"{loc}: expected a JSON object (got {type(r).__name__})",
                "each sanitize rule must be an object like {\"path\": \".x.y\"}",
            )
        _check_unknown_keys(r, _ALLOWED_RULE_KEYS, loc)
        path = _require(r, "path", loc)
        _validate_sanitize_path(path, loc)
        pass_p = path_conventions.derive_pass(agent_type, file, path)
        out.append(SanitizeRule(path=path, pass_path=pass_p, subtree=bool(r.get("subtree", False))))
    return out


_ORIGIN_SENTINEL = "_origin"
_VALID_ORIGINS = {"bundled", "overlay"}


def _consume_origin(entry: dict, source_label: str) -> str:
    """Return the per-entry origin for `entry`, popping the sentinel when
    it was set internally by `_merge`. A user-authored `_origin` key with a
    value outside the internal allowlist is left in place so
    `_check_unknown_keys` rejects it downstream (defence against operator
    injection of `_origin: "bundled"` in an overlay to misattribute errors).
    """
    if _ORIGIN_SENTINEL in entry and entry[_ORIGIN_SENTINEL] in _VALID_ORIGINS:
        return entry.pop(_ORIGIN_SENTINEL)
    return source_label


def _parse_sanitize(arr: Any, agent_type: str, source_label: str) -> list[SanitizeFile]:
    """Parse sanitize[]. Per-entry origin may override `source_label` via the
    `_ORIGIN_SENTINEL` key (set by `_merge` when combining bundled + overlay).
    """
    if not isinstance(arr, list):
        raise ArgitError("manifest.sanitize must be a list", "see MANIFEST.md §Sanitize rules")
    out: list[SanitizeFile] = []
    seen_files: dict[str, tuple[int, str]] = {}  # file → (index, origin)
    for i, sf in enumerate(arr):
        if not isinstance(sf, dict):
            raise ArgitError(
                f"sanitize[{i}] ({source_label}): expected a JSON object (got {type(sf).__name__})",
                "each sanitize entry must be an object like "
                "{\"file\": \"...\", \"rules\": [...]}",
            )
        per_entry_origin = _consume_origin(sf, source_label)
        where = f"sanitize[{i}] ({per_entry_origin})"
        _check_unknown_keys(sf, _ALLOWED_SANITIZE_KEYS, where)
        file = _require(sf, "file", where)
        # Uniqueness invariant: Track D derives sanitize target from `file`, so
        # two blocks with the same `file` would produce identical derived
        # targets. Track C's overlay merge relies on this invariant — enforce
        # it at parse time within-source.
        if file in seen_files:
            prev_idx, prev_origin = seen_files[file]
            raise ArgitError(
                f"{where}: duplicate sanitize.file '{file}' "
                f"(also at sanitize[{prev_idx}] ({prev_origin}))",
                "each sanitize block must target a unique file; merge the rules[] "
                "arrays into a single block or rename one of the files",
            )
        seen_files[file] = (i, per_entry_origin)
        mode_raw = sf.get("mode", DEFAULT_SANITIZE_MODE)
        out.append(
            SanitizeFile(
                file=file,
                target=path_conventions.derive_sanitize_target(agent_type, file),
                mode=_normalize_mode(mode_raw, f"{where}.mode"),
                rules=_parse_sanitize_rules(
                    _require(sf, "rules", where), where, agent_type, file, per_entry_origin,
                ),
                origin=per_entry_origin,
            )
        )
    return out


def _parse_items(arr: Any, agent_type: str, source_label: str) -> list[Item]:
    """Parse items[]. Per-entry origin may override `source_label` via the
    `_ORIGIN_SENTINEL` key (set by `_merge` when combining bundled + overlay)."""
    if not isinstance(arr, list) or len(arr) == 0:
        raise ArgitError("manifest.items must be a non-empty list", "add at least one item")
    out: list[Item] = []
    for i, it in enumerate(arr):
        if not isinstance(it, dict):
            raise ArgitError(
                f"items[{i}] ({source_label}): expected a JSON object (got {type(it).__name__})",
                "each item must be an object like {\"kind\": \"data\", \"source\": \"foo.json\"}",
            )
        per_entry_origin = _consume_origin(it, source_label)
        where = f"items[{i}] ({per_entry_origin})"
        _check_unknown_keys(it, _ALLOWED_ITEM_KEYS, where)
        kind = _require(it, "kind", where)
        if kind not in VALID_KINDS:
            raise ArgitError(
                f"{where}.kind '{kind}' invalid",
                f"use one of: {sorted(VALID_KINDS)}",
            )
        source = _require(it, "source", where)
        path_conventions.validate_glob_source(source)
        is_dir = source.endswith("/")
        is_globbed = "*" in source
        # Kind-specific shape validation.
        if kind == "secret":
            if is_dir:
                raise ArgitError(
                    f"{where}: kind=secret with directory source ('{source}') is not supported",
                    "list individual files or use kind=data for directories",
                )
        elif kind == "sqlite":
            if is_dir:
                raise ArgitError(
                    f"{where}: kind=sqlite source must be a file, not a directory",
                    "remove trailing '/' from source",
                )
        elif kind == "blob":
            if not is_dir and not is_globbed:
                raise ArgitError(
                    f"{where}: kind=blob source must be a directory (trailing '/')",
                    "add trailing '/' to source",
                )
        mode_raw = it.get("mode", path_conventions.default_mode(kind))
        mode = _normalize_mode(mode_raw, f"{where}.mode")
        if kind == "secret":
            target = None
            pass_p = path_conventions.derive_item_pass(agent_type, source)
        else:
            target = path_conventions.derive_item_target(agent_type, kind, source)
            pass_p = None
        out.append(
            Item(
                kind=kind,
                source=source,
                mode=mode,
                target=target,
                pass_path=pass_p,
                origin=per_entry_origin,
            )
        )
    return out


def _check_target_ambiguity(items: list[Item], source_label: str) -> None:
    """AC-D19 / AC-INT7: pairwise targets_overlap check. Error attribution
    uses each item's actual origin (bundled vs overlay) rather than the
    source_label arg — so within-overlay collisions name the overlay."""
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a.kind != b.kind:
                # Different kinds produce different target prefixes — no overlap.
                # (secret has pass_path, not target; but kind must still match for
                # a meaningful overlap claim.)
                continue
            if path_conventions.targets_overlap(a.source, b.source):
                # Origin attribution: name each item's actual origin so
                # within-overlay collisions (AC-INT7) direct the operator
                # to the right file.
                if a.origin == b.origin:
                    scope = f"(both in {a.origin})"
                else:
                    scope = f"({a.origin} + {b.origin})"
                raise ArgitError(
                    f"items[{i}] source '{a.source}' ({a.origin}) and "
                    f"items[{j}] source '{b.source}' ({b.origin}) "
                    f"forward-derive to overlapping targets {scope}",
                    "disambiguate the sources; see MANIFEST.md §Path conventions",
                )


_IDENTITY_FIELDS = {
    "schema_version", "agent_type", "agent_version", "manifest_revision",
    "source_root", "source_root_mode",
}


def _find_overlay(manifest_path: Path) -> Path | None:
    """Return the sibling `<basename>.manifest.local.json` if it exists.

    Convention: `.argit/manifest/foo.manifest.json` → sibling
    `.argit/manifest/foo.manifest.local.json`. Absent → None (no overlay).
    """
    # `<basename>.manifest.json` → replace final `.manifest.json` with
    # `.manifest.local.json`. Basename split via `with_suffix` twice is
    # fragile because `.manifest.json` is two suffixes; do the replacement
    # directly on the name string.
    name = manifest_path.name
    suffix = ".manifest.json"
    if not name.endswith(suffix):
        return None
    overlay_name = name[: -len(suffix)] + ".manifest.local.json"
    overlay = manifest_path.parent / overlay_name
    return overlay if overlay.is_file() else None


def _load_overlay(overlay_path: Path) -> dict:
    """Read + parse the overlay file.

    Branches (matches anchor-spec bundled-malformed handling):
      - unreadable (PermissionError) → ArgitError with chmod+r remediation
      - empty bytes → ArgitError with "remove or add {}" remediation
      - JSONDecodeError → ArgitError wrapping with file + line + column
      - root not a dict → ArgitError naming the observed type
      - valid dict (incl. `{}`) → returned as-is
    """
    try:
        raw = overlay_path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise ArgitError(
            f"overlay {overlay_path} is not readable: {exc}",
            f"run `chmod +r {overlay_path}` to grant read permission",
        ) from exc
    except OSError as exc:
        raise ArgitError(
            f"overlay {overlay_path} cannot be read: {exc}",
            "check filesystem state; the overlay file must be a regular, readable file",
        ) from exc
    if raw.strip() == "":
        raise ArgitError(
            f"overlay {overlay_path} is empty",
            "remove the file or add `{}` to make it a valid empty overlay",
        )
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArgitError(
            f"overlay {overlay_path.name} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})",
            "fix the JSON syntax; argit uses stdlib json — strict double-quoted keys, no trailing commas",
        ) from exc
    if not isinstance(body, dict):
        raise ArgitError(
            f"overlay {overlay_path.name} root must be a JSON object "
            f"(got {type(body).__name__})",
            "wrap the overlay contents in `{ ... }` — see MANIFEST.md §Overlay",
        )
    return body


def _merge(
    bundled: dict, overlay: dict, bundled_path: Path, overlay_path: Path,
) -> dict:
    """Merge overlay into bundled per the fixed rules table (Track C).

    Merge runs BEFORE the existing validators so merged content flows
    through the same parse path uniformly. Conflict detection operates on
    raw-dict source tuples (per Track D strict-derivation, which forbids
    explicit `pass`/`target` in the bundled manifest; overlay inherits the
    rule via unknown-field rejection downstream).

    Rules:
      - Identity fields (schema_version, agent_type, agent_version,
        manifest_revision, source_root, source_root_mode, blob_backend)
        MUST NOT appear in overlay — raise naming offending field.
      - exclude[] → union, bundled-order-first, append local, dedup.
      - items[] → append bundled + overlay; literal-literal duplicate
        (source, kind) → ArgitError naming both origin paths.
      - sanitize[] → per-file union of rules[]; duplicate file across
        bundled + overlay merges their rules (dup rule path → error);
        file-only-in-overlay appended as new block.
      - lifecycle.<sub> → overlay wins per sub-key (partial override OK).
      - Cross-source ambiguity check on items[] + sanitize[] (AC-INT6).

    Error messages always name BOTH bundled_path and overlay_path (F7
    origin attribution).
    """
    for field_name in _IDENTITY_FIELDS | {"blob_backend"}:
        if field_name in overlay:
            raise ArgitError(
                f"overlay {overlay_path.name} must not specify identity field "
                f"'{field_name}' (bundled {bundled_path.name} owns it)",
                f"remove '{field_name}' from {overlay_path}; identity fields come from the bundled manifest",
            )

    merged = dict(bundled)

    # Reject non-list bundled/overlay array fields up-front. Without this,
    # `list(<str>)` silently expands to a list of characters and slips past
    # downstream `isinstance(..., list)` validators.
    def _require_list(source_label: str, source_path: Path, body: dict, key: str) -> list:
        val = body.get(key, [])
        if not isinstance(val, list):
            raise ArgitError(
                f"{source_label} {source_path.name}: manifest.{key} must be a list "
                f"(got {type(val).__name__})",
                f"set {key} in {source_path} to a JSON array",
            )
        return val

    # exclude[] — union with dedup, preserve bundled order, append local.
    bundled_excl = _require_list("bundled", bundled_path, bundled, "exclude")
    overlay_excl = _require_list("overlay", overlay_path, overlay, "exclude")
    seen = set(bundled_excl)
    merged_excl = list(bundled_excl)
    for e in overlay_excl:
        if e not in seen:
            merged_excl.append(e)
            seen.add(e)
    merged["exclude"] = merged_excl

    # items[] — append with literal duplicate check. Tag each item with
    # _ORIGIN_SENTINEL so _parse_items can attribute errors correctly.
    def _tag(it: Any, origin: str) -> Any:
        if isinstance(it, dict):
            return {**it, _ORIGIN_SENTINEL: origin}
        return it

    bundled_items = [_tag(it, "bundled") for it in _require_list("bundled", bundled_path, bundled, "items")]
    overlay_items = [_tag(it, "overlay") for it in _require_list("overlay", overlay_path, overlay, "items")]

    # Literal-duplicate check — (source, kind) pairs must be unique across
    # bundled + overlay. Three error shapes for operator clarity:
    #   - within-overlay (both overlay)  → names overlay file twice + AC-INT7 hint
    #   - within-bundled (both bundled)  → names bundled file + usual hint
    #   - cross-source                   → names both origins explicitly
    literal_index: dict[tuple[str, str], tuple[str, str]] = {}  # key → (origin, file)
    all_literal = [("bundled", it, bundled_path.name) for it in bundled_items] + \
                  [("overlay", it, overlay_path.name) for it in overlay_items]
    for origin, it, file_name in all_literal:
        if not isinstance(it, dict):
            continue
        src, kind = it.get("source"), it.get("kind")
        if not (isinstance(src, str) and isinstance(kind, str) and "*" not in src):
            continue
        key = (src, kind)
        if key in literal_index:
            prev_origin, prev_file = literal_index[key]
            if prev_origin == origin == "overlay":
                raise ArgitError(
                    f"overlay conflict: items[] has two entries with "
                    f"(source='{src}', kind='{kind}') within {overlay_path.name}",
                    f"remove the duplicate from {overlay_path}",
                )
            if prev_origin == origin == "bundled":
                raise ArgitError(
                    f"bundled manifest has two items[] entries with "
                    f"(source='{src}', kind='{kind}') within {bundled_path.name}",
                    "each literal (source, kind) pair must be unique",
                )
            raise ArgitError(
                f"overlay conflict: items[] entry (source='{src}', kind='{kind}') "
                f"appears in both {prev_file} and {file_name}",
                f"remove the duplicate from {overlay_path} or the bundled manifest",
            )
        literal_index[key] = (origin, file_name)
    merged["items"] = bundled_items + overlay_items

    # sanitize[] — per-file union of rules[]. Tag with _ORIGIN_SENTINEL;
    # malformed entries (non-dict or missing "file") collect in a sidecar
    # list and are appended at the end for downstream validators to catch.
    bundled_san = _require_list("bundled", bundled_path, bundled, "sanitize")
    overlay_san = _require_list("overlay", overlay_path, overlay, "sanitize")
    by_file: dict[str, dict] = {}
    san_origin: dict[str, str] = {}
    malformed_san: list[Any] = []
    for sf in bundled_san:
        if not isinstance(sf, dict) or "file" not in sf:
            malformed_san.append(_tag(sf, "bundled"))
            continue
        if sf["file"] in by_file:
            raise ArgitError(
                f"bundled manifest has two sanitize[] entries with "
                f"file='{sf['file']}' within {bundled_path.name}",
                "each sanitize file must be unique",
            )
        by_file[sf["file"]] = {**sf, _ORIGIN_SENTINEL: "bundled"}
        san_origin[sf["file"]] = bundled_path.name
    overlay_seen_files: set[str] = set()
    for osf in overlay_san:
        if not isinstance(osf, dict) or "file" not in osf:
            malformed_san.append(_tag(osf, "overlay"))
            continue
        file = osf["file"]
        if file in overlay_seen_files:
            raise ArgitError(
                f"overlay conflict: sanitize[] has two entries with "
                f"file='{file}' within {overlay_path.name}",
                f"remove the duplicate from {overlay_path}",
            )
        overlay_seen_files.add(file)
        if file not in by_file:
            by_file[file] = {**osf, _ORIGIN_SENTINEL: "overlay"}
            san_origin[file] = overlay_path.name
            continue
        # Merge rules[] with duplicate-path check.
        existing = by_file[file]
        b_rules = list(existing.get("rules", []))
        o_rules = list(osf.get("rules", []))
        b_paths = {r["path"] for r in b_rules if isinstance(r, dict) and "path" in r}
        for r in o_rules:
            if not isinstance(r, dict) or "path" not in r:
                b_rules.append(r)
                continue
            if r["path"] in b_paths:
                raise ArgitError(
                    f"overlay conflict: sanitize.file='{file}' has duplicate "
                    f"rule path '{r['path']}' in both {san_origin[file]} "
                    f"and {overlay_path.name}",
                    f"remove the duplicate rule from {overlay_path}",
                )
            b_rules.append(r)
            b_paths.add(r["path"])
        existing["rules"] = b_rules
    merged["sanitize"] = list(by_file.values()) + malformed_san

    # lifecycle.<sub> — overlay wins per sub-key. AC-C14: validate overlay's
    # sub-command structure at merge time so ArgitError names the overlay
    # file, not just the generic "lifecycle.stop.description" location.
    if "lifecycle" in overlay:
        if not isinstance(overlay["lifecycle"], dict):
            raise ArgitError(
                f"overlay {overlay_path.name}: lifecycle must be an object",
                "see MANIFEST.md §Lifecycle",
            )
        merged_life = dict(bundled.get("lifecycle", {}) or {})
        for sub in ("detect_running", "stop", "start"):
            if sub in overlay["lifecycle"]:
                sub_body = overlay["lifecycle"][sub]
                try:
                    # Pre-validate the overlay sub-command with an overlay-
                    # attributed `where` string. If structure is bad, we
                    # raise here with the operator-friendly overlay path.
                    _parse_lifecycle_cmd(
                        sub_body, f"overlay {overlay_path.name}: lifecycle.{sub}",
                    )
                except ArgitError:
                    raise
                merged_life[sub] = sub_body
        merged["lifecycle"] = merged_life

    # AC-INT6: cross-source ambiguity check on merged items[]. Within-source
    # overlaps are caught later by _check_target_ambiguity after parsing.
    # Here we compare bundled items × overlay items and fail with origin
    # attribution on any pairwise overlap (that isn't already a literal-
    # literal duplicate, which was caught above).
    bundled_sources = [
        (it.get("source"), it.get("kind"))
        for it in bundled_items
        if isinstance(it, dict)
        and isinstance(it.get("source"), str)
        and isinstance(it.get("kind"), str)
    ]
    for oit in overlay_items:
        if not isinstance(oit, dict):
            continue
        osrc, okind = oit.get("source"), oit.get("kind")
        if not (isinstance(osrc, str) and isinstance(okind, str)):
            continue
        for bsrc, bkind in bundled_sources:
            if bkind != okind:
                continue
            if bsrc == osrc and "*" not in bsrc:
                continue  # literal-literal duplicate, already raised above
            if path_conventions.targets_overlap(bsrc, osrc):
                raise ArgitError(
                    f"overlay conflict: items source '{osrc}' in {overlay_path.name} "
                    f"overlaps with source '{bsrc}' in {bundled_path.name} "
                    f"(same kind '{okind}', component-wise ambiguity)",
                    f"disambiguate the sources; see MANIFEST.md §Path conventions + §Overlay",
                )

    return merged


def find_manifest_file(repo_root: Path) -> Path:
    manifest_dir = repo_root / ".argit" / "manifest"
    if not manifest_dir.is_dir():
        raise ArgitError(
            f"no manifest directory at {manifest_dir}",
            "run `argit setup` first",
        )
    candidates = sorted(manifest_dir.glob("*.manifest.json"))
    if len(candidates) == 0:
        raise ArgitError(
            f"no manifest in {manifest_dir}",
            "run `argit setup` first",
        )
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise ArgitError(
            f"multiple manifests in {manifest_dir}: {names}",
            "MVP supports exactly one manifest per repo; remove the extras",
        )
    return candidates[0]


def load_manifest(repo_root: Path) -> Manifest:
    path = find_manifest_file(repo_root)
    file_type, file_ver, file_rev = parse_filename(path.name)

    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArgitError(
            f"manifest {path.name} is not valid JSON: {exc.msg} (line {exc.lineno})",
            "fix the JSON syntax; argit uses stdlib json — strict double-quoted keys, no trailing commas",
        ) from exc

    schema = body.get("schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise ArgitError(
            f"manifest schema version {schema!r} not supported by this argit release",
            INSTALL_HINT,
        )

    _check_unknown_keys(body, _ALLOWED_TOP_LEVEL, "manifest")

    for required in ("agent_type", "agent_version", "manifest_revision", "source_root",
                     "sanitize", "items", "exclude"):
        if required not in body:
            raise ArgitError(
                f"manifest missing required top-level field '{required}'",
                f"add '{required}' (see MANIFEST.md §Top-level structure)",
            )

    if body["agent_type"] != file_type:
        raise ArgitError(
            f"manifest filename agent-type '{file_type}' does not match body.agent_type '{body['agent_type']}'",
            "rename the manifest file or correct the body field",
        )
    if body["agent_version"] != file_ver:
        raise ArgitError(
            f"manifest filename agent-version '{file_ver}' does not match body.agent_version '{body['agent_version']}'",
            "rename the manifest file or correct the body field",
        )
    if int(body["manifest_revision"]) != file_rev:
        raise ArgitError(
            f"manifest filename revision '{file_rev}' does not match body.manifest_revision '{body['manifest_revision']}'",
            "rename the manifest file or correct the body field",
        )

    if not isinstance(body.get("exclude", []), list):
        raise ArgitError("manifest.exclude must be a list", "see MANIFEST.md §Exclude")

    # Overlay discovery + merge — happens BEFORE per-item/per-sanitize
    # validators so merged content flows through the same validation path
    # uniformly. The merge is a raw-dict operation; validators run after.
    overlay_path = _find_overlay(path)
    if overlay_path is not None:
        overlay_body = _load_overlay(overlay_path)
        body = _merge(body, overlay_body, path, overlay_path)
        # Re-check unknown top-level keys on merged body (overlay may have
        # introduced e.g. a stray typo that the caller expects rejected).
        _check_unknown_keys(body, _ALLOWED_TOP_LEVEL, "manifest (merged)")

    excludes = body.get("exclude", [])
    agent_type = body["agent_type"]
    source_root_mode = _normalize_mode(
        body.get("source_root_mode", DEFAULT_SOURCE_ROOT_MODE), "source_root_mode",
    )
    # Origin attribution is done per-entry by `_merge`, which tags each
    # item/sanitize block with _ORIGIN_SENTINEL before parse. Here we pass
    # "bundled" as the fallback origin — it applies only to entries the
    # merge step did not tag (i.e. bundled-only bodies, where no overlay
    # exists). `_parse_items`/`_parse_sanitize` consume the sentinel via
    # `_consume_origin`. AC-INT7 within-overlay ambiguity is caught by
    # `_check_target_ambiguity` on the merged items list.
    sanitize = _parse_sanitize(body["sanitize"], agent_type, "bundled")
    items = _parse_items(body["items"], agent_type, "bundled")
    _check_target_ambiguity(items, "bundled")

    return Manifest(
        schema_version=schema,
        agent_type=agent_type,
        agent_version=body["agent_version"],
        manifest_revision=int(body["manifest_revision"]),
        source_root=body["source_root"],
        source_root_mode=source_root_mode,
        sanitize=sanitize,
        items=items,
        exclude=[str(x) for x in excludes],
        lifecycle=_parse_lifecycle(body.get("lifecycle")),
        filename=path.name,
        overlay_path=overlay_path,
    )


# ---------- Track B — glob expansion ----------

def _origin_file(manifest: Manifest, origin: str) -> str:
    """Map an item/rule origin label to its source manifest filename, for
    runtime-duplicate error messages. Overlay path is optional; bundled
    filename is always present."""
    if origin == "overlay" and manifest.overlay_path is not None:
        return manifest.overlay_path.name
    return manifest.filename


def expand_globbed_item(
    item: Item, root: Path, agent_type: str, exclude_patterns: list[str] | None = None,
) -> list[Item]:
    """Expand a globbed item into concrete Item instances.

    Args:
      item: the (possibly globbed) source item from the manifest. If
        `item.is_globbed` is False, returns [item] unchanged.
      root: the filesystem root against which the glob matches. Backup
        callers pass `source_root`; restore-side enumeration is handled by
        `enumerate_restore_targets` — this helper is source-side only.
      agent_type: passed through to path_conventions for target/pass derivation.
      exclude_patterns: manifest.exclude list; concrete expansions matching
        any pattern are silently dropped (AC-B9).

    Returns:
      list[Item] — concrete items with derived target/pass_path, origin
      preserved. Empty list when zero matches (caller warn-and-continues).
      Sorted deterministically for stable git diffs.
    """
    if not item.is_globbed:
        return [item]

    # `Path.glob` needs its pattern relative to the root. `item.source` is
    # already relative. Trailing slash (dir glob) is stripped — Path.glob
    # doesn't accept it, we restore the slash on matched-dir sources below.
    pattern = item.source.rstrip("/")
    root_path = Path(root)
    matches = sorted(root_path.glob(pattern))
    out: list[Item] = []
    dir_glob = item.source.endswith("/")
    excludes = exclude_patterns or []
    for m in matches:
        try:
            rel = m.relative_to(root_path)
        except ValueError:
            continue
        if dir_glob and not m.is_dir():
            continue
        source_rel = str(rel)
        if dir_glob:
            source_rel = source_rel + "/"
        if matches_exclude(Path(source_rel), excludes):
            continue
        if item.kind == "secret":
            target = None
            pass_p = path_conventions.derive_item_pass(agent_type, source_rel)
        else:
            target = path_conventions.derive_item_target(agent_type, item.kind, source_rel)
            pass_p = None
        out.append(Item(
            kind=item.kind,
            source=source_rel,
            mode=item.mode,
            target=target,
            pass_path=pass_p,
            origin=item.origin,
        ))
    return out


def enumerate_restore_targets(
    item: Item, repo_root: Path, agent_type: str,
) -> list[Item]:
    """Restore-side expansion — enumerates concrete target paths already
    written to the repo (not source_root, which may be empty in DR scenarios).

    For each globbed item: (1) derive the target-pattern via
    path_conventions.derive_item_target (which yields a pattern containing
    `*`), (2) enumerate via repo_root.glob, (3) for each concrete match,
    invert_item_target to reconstruct the on-disk source, (4) synthesize
    a concrete Item. `kind=secret` globs are not supported here —
    secret pass entries are enumerated by the caller via pass list+filter,
    not via repo-filesystem walk.

    For non-globbed items, returns [item] unchanged.
    """
    if not item.is_globbed:
        return [item]
    if item.kind == "secret":
        raise ArgitError(
            "enumerate_restore_targets: kind=secret glob enumeration must "
            "go through pass_store.ls (not repo-filesystem glob)",
            "use expand_globbed_item(item, source_root, ...) at backup time "
            "and a pass-based enumeration at restore time",
        )
    dir_glob = item.source.endswith("/")
    pattern = path_conventions.derive_item_target(agent_type, item.kind, item.source)
    pattern = pattern.rstrip("/")
    repo_root_p = Path(repo_root)
    matches = sorted(repo_root_p.glob(pattern))
    out: list[Item] = []
    for m in matches:
        if dir_glob and not m.is_dir():
            continue
        try:
            rel = m.relative_to(repo_root_p)
        except ValueError:
            continue
        target_rel = str(rel)
        if dir_glob:
            target_rel = target_rel + "/"
        # invert_item_target precondition — concrete (no *). The Path.glob
        # enumeration guarantees this.
        source_rel = path_conventions.invert_item_target(agent_type, item.kind, target_rel)
        out.append(Item(
            kind=item.kind,
            source=source_rel,
            mode=item.mode,
            target=target_rel,
            pass_path=None,
            origin=item.origin,
        ))
    return out


def expand_items_for_backup(
    manifest: Manifest,
    source_root: Path,
    warn: Any = None,
) -> list[Item]:
    """Expand every globbed item in `manifest.items` against `source_root`,
    flatten, and detect runtime duplicates across the expanded set.

    Args:
      manifest, source_root: as expected.
      warn: optional callable(str). If provided, zero-match globs emit
        `"globbed item '<source>' matched nothing — skipping"` before
        being dropped. If None, zero-match is silent (unit-test convenience).

    AC-INT5: two items (bundled glob + overlay explicit, or two globs) that
    expand to the same concrete source raise ArgitError naming both origin
    items and their source manifest files.
    """
    out: list[Item] = []
    by_source: dict[tuple[str, str], Item] = {}
    for it in manifest.items:
        expanded = expand_globbed_item(
            it, source_root, manifest.agent_type, manifest.exclude,
        )
        if it.is_globbed and len(expanded) == 0 and warn is not None:
            warn(f"globbed item '{it.source}' matched nothing — skipping")
        for exp in expanded:
            key = (exp.source, exp.kind)
            if key in by_source:
                prev = by_source[key]
                raise ArgitError(
                    f"runtime duplicate: concrete (source='{exp.source}', kind='{exp.kind}') "
                    f"expanded from two items — "
                    f"one ({prev.origin} in {_origin_file(manifest, prev.origin)}), "
                    f"one ({exp.origin} in {_origin_file(manifest, exp.origin)})",
                    "disambiguate by removing the conflicting overlay or bundled item; "
                    "see MANIFEST.md §Globs in items",
                )
            by_source[key] = exp
            out.append(exp)
    return out


def enumerate_secret_glob_from_pass(
    item: Item, agent_type: str, pass_entries: list[str],
) -> list[Item]:
    """Restore-side enumeration for a globbed `kind=secret` item.

    Derives the pass pattern from `item.source` + agent_type, then filters
    `pass_entries` component-wise (single-component `*` wildcard). For each
    match, substitutes the captured components back into `item.source` to
    reconstruct the concrete on-disk source.
    """
    if not item.is_globbed:
        return [item]
    if item.kind != "secret":
        raise ArgitError(
            "enumerate_secret_glob_from_pass: non-secret kind passed",
            "use enumerate_restore_targets for kind=data|sqlite|blob",
        )
    pass_pattern = path_conventions.derive_item_pass(agent_type, item.source)
    pattern_parts = pass_pattern.split("/")
    src_template = item.source.split("/")
    out: list[Item] = []
    for entry in sorted(pass_entries):
        entry_parts = entry.split("/")
        if len(entry_parts) != len(pattern_parts):
            continue
        captures: list[str] = []
        matched = True
        for pp, ep in zip(pattern_parts, entry_parts):
            if pp == "*":
                captures.append(ep)
                continue
            if pp != ep:
                matched = False
                break
        if not matched:
            continue
        cap_iter = iter(captures)
        concrete_parts = [next(cap_iter) if p == "*" else p for p in src_template]
        concrete_source = "/".join(concrete_parts)
        out.append(Item(
            kind="secret",
            source=concrete_source,
            mode=item.mode,
            target=None,
            pass_path=entry,
            origin=item.origin,
        ))
    return out


def expand_items_for_restore(
    manifest: Manifest,
    repo_root: Path,
    pass_entries: list[str] | None = None,
    warn: Any = None,
) -> list[Item]:
    """Restore-side companion to expand_items_for_backup.

    - Non-secret globbed items enumerate via repo-filesystem glob (AC-B7:
      fresh-DR scenario where source_root is empty but the repo has every
      concrete target).
    - Secret globbed items enumerate from `pass_entries` (PassWrap.ls()
      result). If pass_entries is None and a globbed secret is present,
      returns the globbed item unchanged (caller must handle).

    Args:
      warn: optional callable(str). Emits a warning on zero-match globs so
        operator-confusion cases (repo missing expected multi-agent data)
        surface instead of silently skipping.
    """
    out: list[Item] = []
    by_key: dict[tuple[str, str], Item] = {}
    for it in manifest.items:
        if it.is_globbed and it.kind == "secret":
            if pass_entries is None:
                out.append(it)
                continue
            expanded = enumerate_secret_glob_from_pass(
                it, manifest.agent_type, pass_entries,
            )
        else:
            expanded = enumerate_restore_targets(it, repo_root, manifest.agent_type)
        if it.is_globbed and len(expanded) == 0 and warn is not None:
            warn(f"globbed item '{it.source}' matched nothing at restore — no data to restore for this pattern")
        for exp in expanded:
            key = (exp.source, exp.kind)
            if key in by_key:
                prev = by_key[key]
                raise ArgitError(
                    f"runtime duplicate at restore: concrete (source='{exp.source}', "
                    f"kind='{exp.kind}') from two items — "
                    f"one ({prev.origin} in {_origin_file(manifest, prev.origin)}), "
                    f"one ({exp.origin} in {_origin_file(manifest, exp.origin)})",
                    "inspect the repo / pass-store for stale entries or manifest conflicts",
                )
            by_key[key] = exp
            out.append(exp)
    return out

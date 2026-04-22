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


def _parse_sanitize_rules(
    rules: list, where: str, agent_type: str, file: str, source_label: str
) -> list[SanitizeRule]:
    if not isinstance(rules, list) or len(rules) == 0:
        raise ArgitError(f"{where}: rules must be a non-empty list", "add at least one rule")
    out: list[SanitizeRule] = []
    for i, r in enumerate(rules):
        loc = f"{where}.rules[{i}]"
        _check_unknown_keys(r, _ALLOWED_RULE_KEYS, loc)
        path = _require(r, "path", loc)
        if "*" in path:
            raise ArgitError(
                f"{loc}.path '{path}' contains wildcard '*'",
                "wildcards are unsupported; store the whole file as kind: secret instead",
            )
        pass_p = path_conventions.derive_pass(agent_type, file, path)
        out.append(SanitizeRule(path=path, pass_path=pass_p, subtree=bool(r.get("subtree", False))))
    return out


def _parse_sanitize(arr: Any, agent_type: str, source_label: str) -> list[SanitizeFile]:
    if not isinstance(arr, list):
        raise ArgitError("manifest.sanitize must be a list", "see MANIFEST.md §Sanitize rules")
    out: list[SanitizeFile] = []
    seen_files: dict[str, int] = {}
    for i, sf in enumerate(arr):
        where = f"sanitize[{i}] ({source_label})"
        _check_unknown_keys(sf, _ALLOWED_SANITIZE_KEYS, where)
        file = _require(sf, "file", where)
        # Uniqueness invariant: Track D derives sanitize target from `file`, so
        # two blocks with the same `file` would produce identical derived
        # targets. Track C's overlay merge relies on this invariant — enforce
        # it at parse time within-source.
        if file in seen_files:
            raise ArgitError(
                f"{where}: duplicate sanitize.file '{file}' "
                f"(also at sanitize[{seen_files[file]}] ({source_label}))",
                "each sanitize block must target a unique file; merge the rules[] "
                "arrays into a single block or rename one of the files",
            )
        seen_files[file] = i
        mode_raw = sf.get("mode", DEFAULT_SANITIZE_MODE)
        out.append(
            SanitizeFile(
                file=file,
                target=path_conventions.derive_sanitize_target(agent_type, file),
                mode=_normalize_mode(mode_raw, f"{where}.mode"),
                rules=_parse_sanitize_rules(
                    _require(sf, "rules", where), where, agent_type, file, source_label,
                ),
                origin=source_label,
            )
        )
    return out


def _parse_items(arr: Any, agent_type: str, source_label: str) -> list[Item]:
    if not isinstance(arr, list) or len(arr) == 0:
        raise ArgitError("manifest.items must be a non-empty list", "add at least one item")
    out: list[Item] = []
    for i, it in enumerate(arr):
        where = f"items[{i}] ({source_label})"
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
                origin=source_label,
            )
        )
    return out


def _check_target_ambiguity(items: list[Item], source_label: str) -> None:
    """AC-D19: pairwise targets_overlap over items in the same source."""
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a.kind != b.kind:
                # Different kinds produce different target prefixes — no overlap.
                # (secret has pass_path, not target; but kind must still match for
                # a meaningful overlap claim.)
                continue
            if path_conventions.targets_overlap(a.source, b.source):
                raise ArgitError(
                    f"items[{i}] source '{a.source}' and items[{j}] source '{b.source}' "
                    f"forward-derive to overlapping targets ({source_label})",
                    "disambiguate the sources; see MANIFEST.md §Path conventions",
                )


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

    excludes = body.get("exclude", [])
    if not isinstance(excludes, list):
        raise ArgitError("manifest.exclude must be a list", "see MANIFEST.md §Exclude")

    agent_type = body["agent_type"]
    source_root_mode = _normalize_mode(
        body.get("source_root_mode", DEFAULT_SOURCE_ROOT_MODE), "source_root_mode",
    )
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
    )

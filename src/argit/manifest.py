"""Manifest loader + validator.

Locates `.argit/manifest/*.manifest.json` (exactly one), parses it with stdlib
`json`, and validates structure + filename↔body coherence. Returns a typed
`Manifest` dataclass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ArgitError

VALID_KINDS = {"secret", "data", "sqlite", "blob"}
SUPPORTED_SCHEMA_VERSION = 1
INSTALL_HINT = (
    "upgrade argit: curl -fsSL https://raw.githubusercontent.com/blinkbitcoin/argit/main/install.sh | bash"
)


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


@dataclass(frozen=True)
class Item:
    kind: str
    source: str
    mode: str
    target: str | None = None
    pass_path: str | None = None
    blob_backend: str | None = None

    @property
    def is_dir_source(self) -> bool:
        return self.source.endswith("/")


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
    blob_backend: str
    sanitize: list[SanitizeFile]
    items: list[Item]
    exclude: list[str]
    lifecycle: Lifecycle | None = None
    filename: str = ""

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


def _parse_sanitize_rules(rules: list, where: str) -> list[SanitizeRule]:
    if not isinstance(rules, list) or len(rules) == 0:
        raise ArgitError(f"{where}: rules must be a non-empty list", "add at least one rule")
    out: list[SanitizeRule] = []
    for i, r in enumerate(rules):
        loc = f"{where}.rules[{i}]"
        path = _require(r, "path", loc)
        pass_p = _require(r, "pass", loc)
        if "*" in path:
            raise ArgitError(
                f"{loc}.path '{path}' contains wildcard '*'",
                "wildcards are unsupported; store the whole file as kind: secret instead",
            )
        out.append(SanitizeRule(path=path, pass_path=pass_p, subtree=bool(r.get("subtree", False))))
    return out


def _parse_sanitize(arr: Any) -> list[SanitizeFile]:
    if not isinstance(arr, list):
        raise ArgitError("manifest.sanitize must be a list", "see MANIFEST.md §Sanitize rules")
    out: list[SanitizeFile] = []
    for i, sf in enumerate(arr):
        where = f"sanitize[{i}]"
        out.append(
            SanitizeFile(
                file=_require(sf, "file", where),
                target=_require(sf, "target", where),
                mode=_normalize_mode(_require(sf, "mode", where), f"{where}.mode"),
                rules=_parse_sanitize_rules(_require(sf, "rules", where), where),
            )
        )
    return out


def _parse_items(arr: Any) -> list[Item]:
    if not isinstance(arr, list) or len(arr) == 0:
        raise ArgitError("manifest.items must be a non-empty list", "add at least one item")
    out: list[Item] = []
    for i, it in enumerate(arr):
        where = f"items[{i}]"
        kind = _require(it, "kind", where)
        if kind not in VALID_KINDS:
            raise ArgitError(
                f"{where}.kind '{kind}' invalid",
                f"use one of: {sorted(VALID_KINDS)}",
            )
        source = _require(it, "source", where)
        mode = _normalize_mode(_require(it, "mode", where), f"{where}.mode")
        target = it.get("target")
        pass_p = it.get("pass")
        is_dir = source.endswith("/")
        if kind == "secret":
            if pass_p is None:
                raise ArgitError(f"{where}: kind=secret requires 'pass'", "add a pass-store path")
            if is_dir:
                raise ArgitError(
                    f"{where}: kind=secret with directory source ('{source}') is not supported in MVP",
                    "list individual files or use kind=data + per-file kind=secret entries",
                )
        elif kind == "sqlite":
            if is_dir:
                raise ArgitError(
                    f"{where}: kind=sqlite source must be a file, not a directory",
                    "remove trailing '/' from source",
                )
            if target is None:
                raise ArgitError(f"{where}: kind=sqlite requires 'target'", "add a repo-target path")
        elif kind == "blob":
            if not is_dir:
                raise ArgitError(
                    f"{where}: kind=blob source must be a directory (trailing '/')",
                    "add trailing '/' to source",
                )
            if target is None:
                raise ArgitError(f"{where}: kind=blob requires 'target'", "add a repo-target path")
        elif kind == "data":
            if target is None:
                raise ArgitError(f"{where}: kind=data requires 'target'", "add a repo-target path")
        out.append(
            Item(
                kind=kind,
                source=source,
                mode=mode,
                target=target,
                pass_path=pass_p,
                blob_backend=it.get("blob_backend"),
            )
        )
    return out


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

    for required in ("agent_type", "agent_version", "manifest_revision", "source_root",
                     "source_root_mode", "blob_backend", "sanitize", "items", "exclude"):
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

    if body["blob_backend"] != "git-lfs":
        raise ArgitError(
            f"manifest.blob_backend '{body['blob_backend']}' not supported (MVP supports git-lfs only)",
            "set blob_backend to \"git-lfs\"",
        )

    excludes = body.get("exclude", [])
    if not isinstance(excludes, list):
        raise ArgitError("manifest.exclude must be a list", "see MANIFEST.md §Exclude")

    return Manifest(
        schema_version=schema,
        agent_type=body["agent_type"],
        agent_version=body["agent_version"],
        manifest_revision=int(body["manifest_revision"]),
        source_root=body["source_root"],
        source_root_mode=_normalize_mode(body["source_root_mode"], "source_root_mode"),
        blob_backend=body["blob_backend"],
        sanitize=_parse_sanitize(body["sanitize"]),
        items=_parse_items(body["items"]),
        exclude=[str(x) for x in excludes],
        lifecycle=_parse_lifecycle(body.get("lifecycle")),
        filename=path.name,
    )

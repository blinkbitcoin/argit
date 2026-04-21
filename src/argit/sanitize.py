"""Pure-Python sanitize / re-inject round-trip.

`sanitize()` walks a config dict, extracts secret values per the manifest's
sanitize rules, and replaces them with `${pass:<pass-path>}` placeholders.
`reinject()` is the inverse: walks a sanitized config and substitutes
placeholders with secrets retrieved via a caller-supplied lookup callable.

Subtree rules (`subtree: true`) JSON-serialize the whole subtree at the
target path as one pass entry; non-subtree (leaf) rules store the leaf value
as a string. Walking a sanitized object never mutates the input.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

from .errors import ArgitError
from .manifest import SanitizeRule

PLACEHOLDER_RE = re.compile(r"^\$\{pass:(?P<path>[^}]+)\}$")


def _split_path(dotted: str) -> list[str]:
    if "*" in dotted:
        raise ArgitError(
            f"sanitize path '{dotted}' contains wildcard '*'",
            "wildcards are unsupported; store the whole file as kind: secret instead",
        )
    s = dotted.lstrip(".")
    if not s:
        raise ArgitError(f"sanitize path '{dotted}' is empty", "use a non-empty dotted path")
    return s.split(".")


def resolve(obj: Any, dotted_path: str) -> Any:
    parts = _split_path(dotted_path)
    cur = obj
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(f"path '{dotted_path}' not found at component '{p}'")
        cur = cur[p]
    return cur


def _set_path(obj: Any, dotted_path: str, value: Any) -> None:
    parts = _split_path(dotted_path)
    cur = obj
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(f"path '{dotted_path}' not found at component '{p}'")
        cur = cur[p]
    if not isinstance(cur, dict):
        raise KeyError(f"path '{dotted_path}' parent is not an object")
    cur[parts[-1]] = value


def _placeholder(pass_path: str) -> str:
    return f"${{pass:{pass_path}}}"


def sanitize(config: dict, rules: list[SanitizeRule]) -> tuple[dict, dict[str, str]]:
    """Returns (sanitized_config, {pass_path: value_to_store}).

    Subtree rules JSON-serialize the subtree (sorted keys, no whitespace); leaf
    rules `str()`-coerce the leaf. Missing paths raise ArgitError so author
    typos are loud — sanitize-time is the right place to catch them.
    """
    out = copy.deepcopy(config)
    extracted: dict[str, str] = {}
    for rule in rules:
        try:
            value = resolve(out, rule.path)
        except KeyError as exc:
            raise ArgitError(
                f"sanitize path '{rule.path}' not present in source",
                "remove the rule from the manifest, or fix the path; the source file may have changed schema",
            ) from exc
        if rule.subtree:
            extracted[rule.pass_path] = json.dumps(value, sort_keys=True, separators=(",", ":"))
        else:
            if isinstance(value, (dict, list)):
                raise ArgitError(
                    f"sanitize path '{rule.path}' resolves to a {type(value).__name__}, not a leaf",
                    "set 'subtree: true' on this rule, or refine the path to point at a leaf value",
                )
            extracted[rule.pass_path] = "" if value is None else str(value)
        _set_path(out, rule.path, _placeholder(rule.pass_path))
    return out, extracted


def reinject(sanitized: dict, secret_lookup: Callable[[str], str]) -> dict:
    """Walk sanitized config, substitute every `${pass:<path>}` placeholder.

    Subtree placeholders (the looked-up value parses as JSON) are deserialized.
    Leaf placeholders return the raw string. Non-string leaves are passed
    through unchanged.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, str):
            m = PLACEHOLDER_RE.match(node)
            if m:
                pass_path = m["path"]
                raw = secret_lookup(pass_path)
                # Subtree round-trip: try to parse JSON; on failure treat as leaf.
                try:
                    return json.loads(raw)
                except (ValueError, TypeError):
                    return raw
        return node

    return walk(sanitized)


def find_placeholders(node: Any, _path: str = "$") -> list[tuple[str, str]]:
    """Return [(json-pointer-ish path, pass_path), ...] for every leftover
    `${pass:...}` placeholder in a (re-injected) config — used by the verify
    phase to detect leaks.
    """
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(find_placeholders(v, f"{_path}.{k}"))
    elif isinstance(node, list):
        for i, x in enumerate(node):
            out.extend(find_placeholders(x, f"{_path}[{i}]"))
    elif isinstance(node, str):
        m = PLACEHOLDER_RE.match(node)
        if m:
            out.append((_path, m["path"]))
    return out

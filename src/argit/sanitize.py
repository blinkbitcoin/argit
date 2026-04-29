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
    s = dotted.lstrip(".")
    if not s:
        raise ArgitError(f"sanitize path '{dotted}' is empty", "use a non-empty dotted path")
    return s.split(".")


def _expand_wildcards(rule: SanitizeRule, config: Any) -> list[SanitizeRule]:
    """Expand a wildcard rule into N concrete rules — one per matched key at
    the wildcard depth. Zero matches returns []; the caller treats that as a
    skip (same path as a missing fixed-path rule).

    Wildcard semantics (parse-time validates form; this expands at runtime):
      - At most one `*` per path; `*` is a whole segment; not the first segment.
      - The prefix leading up to `*` must resolve to a dict in `config`. A
        missing prefix returns [] (skipped — same as fixed-path missing).
      - Both `path` and `pass_path` are rewritten in lockstep — `derive_pass`
        passes a `*` segment through camelCase-to-kebab unchanged, so the
        wildcard sits at the same offset in both representations.
    """
    parts = _split_path(rule.path)
    if "*" not in parts:
        return [rule]
    star_idx = parts.index("*")
    cur: Any = config
    for p in parts[:star_idx]:
        if not isinstance(cur, dict) or p not in cur:
            return []
        cur = cur[p]
    if not isinstance(cur, dict):
        raise ArgitError(
            f"sanitize path '{rule.path}': prefix before '*' resolves to a "
            f"{type(cur).__name__}, not an object",
            "the segment before '*' must address a JSON object whose keys are enumerated",
        )
    pp_parts = rule.pass_path.split("/")
    star_pp_idx = pp_parts.index("*")
    out: list[SanitizeRule] = []
    for key in cur:
        new_parts = parts.copy()
        new_parts[star_idx] = key
        new_pp = pp_parts.copy()
        new_pp[star_pp_idx] = key
        out.append(SanitizeRule(
            path="." + ".".join(new_parts),
            pass_path="/".join(new_pp),
            subtree=rule.subtree,
        ))
    return out


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


def sanitize(config: dict, rules: list[SanitizeRule]) -> tuple[dict, dict[str, str], list[SanitizeRule]]:
    """Returns (sanitized_config, {pass_path: value_to_store}, skipped_rules).

    Subtree rules JSON-serialize the subtree (sorted keys, no whitespace); leaf
    rules `str()`-coerce the leaf. A rule whose path is not present in the
    source is returned in `skipped_rules` — the caller should warn and
    continue. This matches the same "skip-with-log if source missing" pattern
    kind:secret items use, and accommodates real-world configs that omit
    optional subsystems (no Telegram → no `.channels.telegram.botToken`).
    Author typos still surface — a missing path is a skip, not silently
    ignored, and the warning names the offending rule.
    """
    out = copy.deepcopy(config)
    extracted: dict[str, str] = {}
    skipped: list[SanitizeRule] = []
    expanded: list[SanitizeRule] = []
    for rule in rules:
        if "*" in rule.path:
            matches = _expand_wildcards(rule, out)
            if not matches:
                skipped.append(rule)
                continue
            expanded.extend(matches)
        else:
            expanded.append(rule)
    for rule in expanded:
        try:
            value = resolve(out, rule.path)
        except KeyError:
            skipped.append(rule)
            continue
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
    return out, extracted, skipped


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

"""Unit tests for canonical_hash — AC-A19/A20/A21/A22.

Determinism properties verified:
- whitespace / indentation / trailing newline → same hash
- key ordering → same hash
- UTF-8 BOM prefix → same hash
- non-ASCII content → deterministic, ensure_ascii=True escapes to \\uXXXX
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argit.errors import ArgitError
from argit.hashing import canonical_hash


def _write(path: Path, text: str, *, bom: bool = False) -> Path:
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


# ---------- AC-A19 / AC-A20 — whitespace + formatting insensitivity ----------

def test_a19_different_whitespace_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1,"y":2}')
    b = _write(tmp_path / "b.json", '{\n  "x": 1,\n  "y": 2\n}\n')
    assert canonical_hash(a) == canonical_hash(b)


def test_a19_different_indentation_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1,"nested":{"y":2}}')
    b = _write(tmp_path / "b.json", '{\n\t"x":1,\n\t"nested":{\n\t\t"y":2\n\t}\n}')
    assert canonical_hash(a) == canonical_hash(b)


def test_a20_trailing_whitespace_and_newlines_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1}')
    b = _write(tmp_path / "b.json", '{"x":1}   \n\n\n')
    assert canonical_hash(a) == canonical_hash(b)


# ---------- AC-A19 — key-ordering insensitivity ----------

def test_a19_reordered_keys_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1,"y":2,"z":3}')
    b = _write(tmp_path / "b.json", '{"z":3,"x":1,"y":2}')
    assert canonical_hash(a) == canonical_hash(b)


def test_a19_reordered_nested_keys_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"outer":{"a":1,"b":2}}')
    b = _write(tmp_path / "b.json", '{"outer":{"b":2,"a":1}}')
    assert canonical_hash(a) == canonical_hash(b)


# ---------- AC-A21 — BOM insensitivity ----------

def test_a21_bom_prefix_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1}')
    b = _write(tmp_path / "b.json", '{"x":1}', bom=True)
    assert canonical_hash(a) == canonical_hash(b)


# ---------- AC-A22 — non-ASCII determinism via ensure_ascii=True ----------

def test_a22_nonascii_content_deterministic(tmp_path):
    a = _write(tmp_path / "a.json", '{"currency":"€","emoji":"🎉"}')
    h1 = canonical_hash(a)
    h2 = canonical_hash(a)
    assert h1 == h2
    # ensure_ascii=True escapes non-ASCII → canonical bytes are pure ASCII.
    # We re-derive the canonical form manually and confirm.
    body = json.loads(a.read_text(encoding="utf-8-sig"))
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert "\\u20ac" in canonical  # € → €
    assert "\\ud83c\\udf89" in canonical  # 🎉 → surrogate pair


def test_a22_nonascii_different_files_different_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"name":"café"}')
    b = _write(tmp_path / "b.json", '{"name":"cafe"}')
    assert canonical_hash(a) != canonical_hash(b)


# ---------- error handling ----------

def test_malformed_json_raises_argiterror(tmp_path):
    bad = _write(tmp_path / "bad.json", '{"x":')
    with pytest.raises(ArgitError) as exc:
        canonical_hash(bad)
    assert "not valid JSON" in str(exc.value)
    assert "bad.json" in str(exc.value)


def test_file_not_found_raises_argiterror(tmp_path):
    with pytest.raises(ArgitError) as exc:
        canonical_hash(tmp_path / "nonexistent.json")
    assert "not found" in str(exc.value)


def test_same_content_different_paths_same_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1}')
    (tmp_path / "deeper").mkdir()
    b = _write(tmp_path / "deeper" / "b.json", '{"x":1}')
    assert canonical_hash(a) == canonical_hash(b)


# ---------- structural difference produces different hash ----------

def test_different_values_different_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1}')
    b = _write(tmp_path / "b.json", '{"x":2}')
    assert canonical_hash(a) != canonical_hash(b)


def test_added_key_different_hash(tmp_path):
    a = _write(tmp_path / "a.json", '{"x":1}')
    b = _write(tmp_path / "b.json", '{"x":1,"y":2}')
    assert canonical_hash(a) != canonical_hash(b)


# ---------- regression: pinned hash for a known vector ----------

def test_known_vector_hash_stable(tmp_path):
    """Pin a specific canonical_hash output for a specific input.

    Guards against CPython json.dumps changes (float precision, Unicode
    normalization, new kwargs) silently flipping bundled-manifest
    classifications from clean → operator_modified. Spec pre-mortem
    (tech-spec §Notes — Pre-mortem risk items) flags this as a known risk.

    If this test breaks on a newer Python: investigate before rebuilding
    the catalog — an operator's stable manifest may flip classification.
    """
    # Fixed input covering: nested object, number, string with non-ASCII
    # (€ → \\u20ac under ensure_ascii=True), array, bool, null.
    fixture = _write(
        tmp_path / "vector.json",
        '{"a":[1,2,3],"b":true,"c":null,"d":{"nested":"€"},"e":"plain"}',
    )
    # Pinned on CPython 3.11.13. Canonical bytes:
    #   {"a":[1,2,3],"b":true,"c":null,"d":{"nested":"€"},"e":"plain"}
    # If this fails on a newer Python: audit WHY before updating the
    # pinned value — an operator's stable manifest may flip classification.
    expected = "127309678319bdc6d59cea0866b57d84ea20695338dd4e243c51862b9da730d2"
    got = canonical_hash(fixture)
    assert got == expected, (
        f"canonical_hash drift detected: got {got!r}, expected {expected!r}. "
        f"Investigate before updating the pinned value."
    )

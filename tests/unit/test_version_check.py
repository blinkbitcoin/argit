"""Issue #9: openclaw --version parse must find the version token,
not assume token[0] is the version.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from argit.backup import _version_check
from argit.manifest import Manifest


def _manifest(agent_version: str = "2026.3.28") -> Manifest:
    return Manifest(
        schema_version=1,
        agent_type="openclaw",
        agent_version=agent_version,
        manifest_revision=6,
        source_root="/tmp",
        source_root_mode="0700",
        sanitize=[],
        items=[],
        exclude=[],
        lifecycle=None,
        filename="openclaw-2026.3.28-6.manifest.json",
    )


def _mock_run(stdout: str, returncode: int = 0):
    def _fn(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )
    return _fn


@pytest.mark.parametrize(
    "output,expected_warning_fragment,expected_newer_older",
    [
        # Issue #9 — real-world format: name + version + commit.
        ("OpenClaw 2026.3.28 (f9b1079)\n", None, "equal"),
        # Bare version, leading v.
        ("v2026.3.28\n", None, "equal"),
        # Older than manifest → "older than manifest" warning.
        ("OpenClaw 2026.3.27 (abc123)\n", "older than manifest", "older"),
        # Newer than manifest → "newer than manifest" warning.
        ("OpenClaw 2026.3.29 (abc123)\n", "newer than manifest", "newer"),
        # Name-only, no version anywhere → warn unparseable and skip.
        ("OpenClaw prerelease\n", "no parseable version token", "skip"),
        # Empty output → existing behavior (different code path).
        ("\n", "produced no output", "skip"),
    ],
)
def test_version_parse_finds_version_token(
    output, expected_warning_fragment, expected_newer_older,
):
    manifest = _manifest("2026.3.28")
    warnings: list[str] = []
    with patch("argit.backup.shutil.which", return_value="/usr/bin/openclaw"):
        with patch("argit.backup.subprocess.run", _mock_run(output)):
            with patch("argit.backup._warn", side_effect=lambda m: warnings.append(m)):
                _version_check(manifest)

    joined = "\n".join(warnings)
    if expected_warning_fragment is None:
        assert joined == "", f"expected no warnings, got: {joined}"
    else:
        assert expected_warning_fragment in joined, (
            f"expected substring '{expected_warning_fragment}' in: {joined}"
        )

"""install.sh branch coverage. Pytest driver — runs the script with PATH stubbed.

Three cases: only `uv`, only `pipx`, neither. We stub the binaries with thin
shell scripts that record their invocation and exit 0.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"


def _make_stub(dir_: Path, name: str, body: str = "echo STUB:$0 \"$@\"; exit 0\n") -> Path:
    p = dir_ / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _run(env_path: str) -> subprocess.CompletedProcess:
    env = {"PATH": env_path, "ARGIT_TAG": "v1"}
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        capture_output=True, text=True, timeout=10, env=env,
    )


def test_uv_path(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _make_stub(stub_dir, "uv", body="echo STUB:uv \"$@\"; exit 0\n")
    _make_stub(stub_dir, "argit", body="echo argit 1.0.0; exit 0\n")
    cp = _run(f"{stub_dir}:/usr/bin:/bin")
    assert cp.returncode == 0, cp.stderr
    assert "STUB:uv tool install git+https://github.com/blinkbitcoin/argit@v1" in cp.stdout


def test_pipx_path(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _make_stub(stub_dir, "pipx", body="echo STUB:pipx \"$@\"; exit 0\n")
    _make_stub(stub_dir, "argit", body="echo argit 1.0.0; exit 0\n")
    cp = _run(f"{stub_dir}:/usr/bin:/bin")
    assert cp.returncode == 0, cp.stderr
    assert "STUB:pipx install git+https://github.com/blinkbitcoin/argit@v1" in cp.stdout


def test_neither_present(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    cp = _run(f"{stub_dir}:/usr/bin:/bin")
    assert cp.returncode == 1
    assert "Neither uv nor pipx found" in cp.stdout


def test_uv_installs_but_argit_not_on_path(tmp_path):
    """Realistic: `uv tool install` succeeds but ~/.local/bin isn't on PATH yet.
    install.sh's trailing `argit --version` should fail, with `set -euo pipefail`
    causing the script to exit non-zero."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _make_stub(stub_dir, "uv", body="echo STUB:uv \"$@\"; exit 0\n")
    # No `argit` stub — PATH does not contain the freshly-installed binary.
    cp = _run(f"{stub_dir}:/usr/bin:/bin")
    assert cp.returncode != 0, f"expected non-zero exit; got: stdout={cp.stdout} stderr={cp.stderr}"

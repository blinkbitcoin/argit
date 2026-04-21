"""End-to-end roundtrip: backup → wipe → restore → diff.

Uses real subprocess invocation of `python -m argit.cli` so cwd, env, and
exit codes are exactly what an operator would see. Skipped when host
binaries are missing (see conftest.py).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from argit.setup import _bundled_manifest_path

from .conftest import git_init_repo

BUNDLED = _bundled_manifest_path()
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _build_fixture(target: Path) -> None:
    script = Path(__file__).resolve().parent / "fixtures" / "build_fixture.py"
    subprocess.run(
        [sys.executable, str(script), "--out", str(target)],
        check=True, capture_output=True,
    )


def _pass_init(secrets_dir: Path, fpr: str, env: dict[str, str]) -> None:
    pass_env = {**env, "PASSWORD_STORE_DIR": str(secrets_dir)}
    subprocess.run(["pass", "init", fpr], cwd=str(secrets_dir.parent), env=pass_env, check=True, capture_output=True)


def _setup_repo(repo: Path, fpr: str, env: dict[str, str]) -> None:
    mdir = repo / ".argit" / "manifest"
    mdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUNDLED, mdir / BUNDLED.name)
    (repo / ".gitattributes").write_text("openclaw/media/** filter=lfs diff=lfs merge=lfs -text\n")
    (repo / ".gitignore").write_text(".argit/in-progress\n.argit/lock\n")
    secrets = repo / "secrets"
    secrets.mkdir(exist_ok=True)
    _pass_init(secrets, fpr, env)


def _argit(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    pythonpath = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "argit.cli", *args],
        cwd=str(cwd),
        env={**env, "PYTHONPATH": pythonpath},
        capture_output=True, text=True, timeout=180,
    )


def test_full_roundtrip(tmp_path, monkeypatch, ephemeral_gpg_key, gnupg_home):
    """Setup → fixture → backup → restore --target → diff."""
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)

    env = {
        **os.environ,
        "HOME": str(home),
        "GNUPGHOME": str(gnupg_home),
    }

    fpr = ephemeral_gpg_key
    source = home / ".openclaw"
    _build_fixture(source)
    _setup_repo(repo, fpr, env)

    # backup
    cp = _argit(["backup"], cwd=repo, env=env)
    assert cp.returncode == 0, f"backup stdout={cp.stdout}\nstderr={cp.stderr}"

    # AC 9: no plaintext fake tokens leaked into openclaw/ tree
    leaked = []
    needles = [b"ghu_FAKE", b"xoxb-fake-slack-bot", b"xapp-fake-slack-app", b"sk-proj-FAKE",
               b"-----BEGIN PRIVATE KEY-----", b"FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"]
    for f in (repo / "openclaw").rglob("*"):
        if not f.is_file():
            continue
        body = f.read_bytes()
        for needle in needles:
            if needle in body:
                leaked.append((str(f.relative_to(repo)), needle))
    assert not leaked, f"plaintext-secret leak detected: {leaked}"

    # AC 8: state files exist with binary content
    for sqlite_target in ("openclaw/state/memory-main.sqlite", "openclaw/state/tasks-runs.sqlite",
                          "openclaw/state/flows-registry.sqlite"):
        p = repo / sqlite_target
        assert p.is_file() and p.stat().st_size > 0, f"missing/empty: {sqlite_target}"
        # SQLite magic header
        assert p.read_bytes()[:16] == b"SQLite format 3\x00", f"not SQLite: {sqlite_target}"

    # restore to scratch target
    scratch = tmp_path / "scratch"
    cp = _argit(["restore", "--target", str(scratch)], cwd=repo, env=env)
    assert cp.returncode == 0, f"restore stdout={cp.stdout}\nstderr={cp.stderr}"

    # AC 16: re-injected openclaw.json has the original token, not a placeholder
    restored = json.loads((scratch / "openclaw.json").read_text())
    assert restored["gateway"]["auth"]["token"].startswith("fake-gateway-bearer")
    assert "${pass:" not in (scratch / "openclaw.json").read_text()
    assert restored["env"] == {
        "OPENAI_API_KEY": "sk-proj-FAKExxxxxxxxxxxxxxxxxxxxxxxxxxFAKE",
        "ANTHROPIC_API_KEY": "sk-ant-FAKExxxxxxxxxxxxxxxxxxxxxxxxxxxFAKE",
    }

    # AC 16: SQLite files open and PRAGMA integrity_check returns ok
    for sqlite_path in ("memory/main.sqlite", "tasks/runs.sqlite", "flows/registry.sqlite"):
        p = scratch / sqlite_path
        assert p.is_file()
        con = sqlite3.connect(str(p))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            assert row[0] == "ok", f"{sqlite_path}: {row}"
        finally:
            con.close()
        # AC 16: mode 0600 on sqlite items
        assert oct(p.stat().st_mode & 0o777) == "0o600"

    # AC 16: scratch root is mode 0700
    assert oct(scratch.stat().st_mode & 0o777) == "0o700"

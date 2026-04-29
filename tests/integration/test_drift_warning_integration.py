"""Integration: backup-time bundled-manifest drift warning end-to-end.

Real `argit backup` subprocess against a fully-set-up repo + ephemeral GPG +
real pass store. The bundled manifest is mutated in-place; backup must:
  - exit 0 (warning is non-blocking)
  - emit "bundled manifest hash mismatch" to stderr
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from argit.setup import _bundled_manifest_path

from .conftest import git_init_repo

BUNDLED = _bundled_manifest_path()
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _argit(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    pythonpath = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "argit.cli", *args],
        cwd=str(cwd),
        env={**env, "PYTHONPATH": pythonpath},
        capture_output=True, text=True, timeout=180,
    )


def _build_fixture(target: Path) -> None:
    script = Path(__file__).resolve().parent / "fixtures" / "build_fixture.py"
    subprocess.run(
        [sys.executable, str(script), "--out", str(target)],
        check=True, capture_output=True,
    )


def _setup_repo(tmp_path: Path, gnupg_home: Path, fpr: str) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "HOME": str(home), "GNUPGHOME": str(gnupg_home)}

    mdir = repo / ".argit" / "manifest"; mdir.mkdir(parents=True)
    shutil.copy2(BUNDLED, mdir / BUNDLED.name)
    (repo / ".gitattributes").write_text("openclaw/blob/** filter=lfs diff=lfs merge=lfs -text\n")
    (repo / ".gitignore").write_text(".argit/in-progress\n.argit/lock\n")
    secrets = repo / "secrets"; secrets.mkdir()
    pass_env = {**env, "PASSWORD_STORE_DIR": str(secrets)}
    subprocess.run(["pass", "init", fpr], cwd=str(repo), env=pass_env, check=True, capture_output=True)
    _build_fixture(home / ".openclaw")
    return repo, env


def test_backup_warns_on_hand_edited_bundled(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo(tmp_path, gnupg_home, ephemeral_gpg_key)
    bundled = repo / ".argit" / "manifest" / BUNDLED.name
    # Schema-valid mutation: append an exclude pattern. Keeps load_manifest
    # happy (so the drift helper actually runs), changes canonical hash (so
    # drift classifies operator_modified). This mimics what an in-place
    # agent edit would look like in real life.
    body = json.loads(bundled.read_text())
    body.setdefault("exclude", []).append("argit-test-mutation/")
    bundled.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")

    cp = _argit(["backup"], cwd=repo, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "bundled manifest hash mismatch" in cp.stderr
    assert BUNDLED.name in cp.stderr
    assert "Run `argit setup`" in cp.stderr


def test_backup_silent_on_clean_bundled(tmp_path, gnupg_home, ephemeral_gpg_key):
    """No false-positive on the unmodified shipped bundled manifest."""
    repo, env = _setup_repo(tmp_path, gnupg_home, ephemeral_gpg_key)
    cp = _argit(["backup"], cwd=repo, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "bundled manifest hash mismatch" not in cp.stderr

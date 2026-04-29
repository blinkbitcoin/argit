"""Integration: argit review + auto-emit-during-backup end-to-end."""

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


def test_backup_auto_emits_review_on_uncovered(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo(tmp_path, gnupg_home, ephemeral_gpg_key)
    # Plant an uncovered file: a path no manifest rule matches.
    uncovered = Path(env["HOME"]) / ".openclaw" / "future-plugin" / "state.json"
    uncovered.parent.mkdir(parents=True)
    uncovered.write_text(json.dumps({"hello": "world"}) + "\n")

    cp = _argit(["backup"], cwd=repo, env=env)

    assert cp.returncode == 0, cp.stdout + cp.stderr
    reviews = list((repo / ".argit" / "reviews").glob("*.md"))
    assert len(reviews) == 1
    body = reviews[0].read_text(encoding="utf-8")
    assert "future-plugin/state.json" in body
    assert "Manifest:" in body


def test_backup_no_review_when_source_clean(tmp_path, gnupg_home, ephemeral_gpg_key):
    """Default fixture (build_fixture.py) plus the bundled manifest cover
    each other → no uncovered files → no review file emitted."""
    repo, env = _setup_repo(tmp_path, gnupg_home, ephemeral_gpg_key)

    cp = _argit(["backup"], cwd=repo, env=env)

    assert cp.returncode == 0, cp.stdout + cp.stderr
    # If a previous test left a reviews dir, it would still be there but
    # this test must not produce a NEW report from a clean source.
    reviews_dir = repo / ".argit" / "reviews"
    if reviews_dir.exists():
        # It's only created by argit on emit; a clean run should never
        # create it. If it exists, it should be empty.
        assert list(reviews_dir.iterdir()) == []


# test_backup_commit_stages_review_file is covered at unit level (see
# tests/unit/test_backup_review_staging.py). End-to-end `--commit` is
# blocked on a pre-existing fixture-vs-manifest mismatch (PR #13's
# bundled manifest expects nodes/paired.json which build_fixture.py
# doesn't create) — orthogonal to this PR's review feature.


def test_argit_review_verb_emits_same_shape(tmp_path, gnupg_home, ephemeral_gpg_key):
    """Manual `argit review` produces the same kind of report as auto-emit
    (single source of truth — both invoke generate_review)."""
    repo, env = _setup_repo(tmp_path, gnupg_home, ephemeral_gpg_key)
    uncovered = Path(env["HOME"]) / ".openclaw" / "future-plugin" / "state.json"
    uncovered.parent.mkdir(parents=True)
    uncovered.write_text("{}")

    cp = _argit(["review"], cwd=repo, env=env)

    assert cp.returncode == 0, cp.stdout + cp.stderr
    reviews = list((repo / ".argit" / "reviews").glob("*.md"))
    assert len(reviews) == 1
    body = reviews[0].read_text(encoding="utf-8")
    assert "future-plugin/state.json" in body
    assert "## Uncovered paths" in body

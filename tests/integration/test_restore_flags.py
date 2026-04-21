"""Integration: flag-gated restore scenarios (AC 17–22)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

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


def _setup_repo_and_backup(tmp_path: Path, gnupg_home: Path, fpr: str) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "HOME": str(home), "GNUPGHOME": str(gnupg_home)}

    mdir = repo / ".argit" / "manifest"; mdir.mkdir(parents=True)
    shutil.copy2(BUNDLED, mdir / BUNDLED.name)
    (repo / ".gitattributes").write_text("openclaw/media/** filter=lfs diff=lfs merge=lfs -text\n")
    (repo / ".gitignore").write_text(".argit/in-progress\n.argit/lock\n")
    secrets = repo / "secrets"; secrets.mkdir()
    pass_env = {**env, "PASSWORD_STORE_DIR": str(secrets)}
    subprocess.run(["pass", "init", fpr], cwd=str(repo), env=pass_env, check=True, capture_output=True)

    _build_fixture(home / ".openclaw")
    cp = _argit(["backup"], cwd=repo, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return repo, env


def test_restore_refuses_non_empty_target(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    scratch = tmp_path / "scratch"; scratch.mkdir()
    (scratch / "stranger.txt").write_text("preexisting")
    cp = _argit(["restore", "--target", str(scratch)], cwd=repo, env=env)
    assert cp.returncode != 0
    assert "non-empty" in (cp.stdout + cp.stderr).lower()
    # stranger.txt unchanged
    assert (scratch / "stranger.txt").read_text() == "preexisting"


def test_restore_overwrite(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    scratch = tmp_path / "scratch"; scratch.mkdir()
    (scratch / "stranger.txt").write_text("preexisting")
    cp = _argit(["restore", "--target", str(scratch), "--overwrite", "--yes"], cwd=repo, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert not (scratch / "stranger.txt").exists()
    assert (scratch / "openclaw.json").is_file()


def test_restore_merge_preserves_unmanaged(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    scratch = tmp_path / "scratch"; scratch.mkdir()
    (scratch / "stranger.txt").write_text("preexisting")
    cp = _argit(["restore", "--target", str(scratch), "--merge"], cwd=repo, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (scratch / "stranger.txt").read_text() == "preexisting"
    assert (scratch / "openclaw.json").is_file()


def test_restore_overwrite_and_merge_mutually_exclusive(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    cp = _argit(["restore", "--overwrite", "--merge"], cwd=repo, env=env)
    assert cp.returncode == 2
    assert "mutually exclusive" in (cp.stdout + cp.stderr).lower()


def test_backup_strict_fails_on_unspecified(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    # Add an unmanaged file to the source tree
    home = Path(env["HOME"])
    (home / ".openclaw" / "future-plugin").mkdir()
    (home / ".openclaw" / "future-plugin" / "state.json").write_text("{}")
    cp = _argit(["backup", "--strict"], cwd=repo, env=env)
    assert cp.returncode != 0
    assert "future-plugin" in (cp.stdout + cp.stderr)


def test_backup_default_warns_on_unspecified(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    home = Path(env["HOME"])
    (home / ".openclaw" / "future-plugin").mkdir()
    (home / ".openclaw" / "future-plugin" / "state.json").write_text("{}")
    cp = _argit(["backup"], cwd=repo, env=env)
    assert cp.returncode == 0
    assert "future-plugin" in (cp.stdout + cp.stderr)


def test_backup_dryrun(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    # Wipe any backup output to verify dry-run writes nothing
    if (repo / "openclaw").exists():
        shutil.rmtree(repo / "openclaw")
    cp = _argit(["backup", "--dry-run"], cwd=repo, env=env)
    assert cp.returncode == 0
    assert "would:" in cp.stdout
    assert not (repo / "openclaw").exists()


def test_restore_verify_catches_placeholder_leak(tmp_path, gnupg_home, ephemeral_gpg_key):
    """AC 21: corrupt the committed sanitize file by re-introducing a ${pass:} placeholder."""
    import json as _json
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)
    # Force a leftover placeholder in the SANITIZED repo file.
    sanitized = repo / "openclaw" / "config" / "openclaw.json"
    body = _json.loads(sanitized.read_text())
    # Manually inject a fake placeholder pointing at a pass path that doesn't exist.
    body["gateway"]["auth"]["token"] = "${pass:argit/openclaw/nonexistent/leak}"
    sanitized.write_text(_json.dumps(body))

    scratch = tmp_path / "scratch"
    cp = _argit(["restore", "--target", str(scratch)], cwd=repo, env=env)
    # Either reinject errors out or the verify phase catches the leak.
    assert cp.returncode != 0
    output = cp.stdout + cp.stderr
    assert "argit/openclaw/nonexistent/leak" in output or "verify" in output.lower()


def test_restore_verify_catches_lfs_pointer(tmp_path, gnupg_home, ephemeral_gpg_key):
    """AC 22: a manifest with a kind:blob whose committed copy is an LFS pointer."""
    import json as _json
    from importlib import resources
    repo, env = _setup_repo_and_backup(tmp_path, gnupg_home, ephemeral_gpg_key)

    # Inject a kind:blob item with a pointer-file in its repo target.
    manifest_path = repo / ".argit" / "manifest" / BUNDLED.name
    body = _json.loads(manifest_path.read_text())
    body["items"].append({
        "kind": "blob",
        "source": "media/inbound/",
        "target": "openclaw/media/inbound/",
        "mode": "0644",
        "blob_backend": "git-lfs",
    })
    manifest_path.write_text(_json.dumps(body))

    blob_dir = repo / "openclaw" / "media" / "inbound"
    blob_dir.mkdir(parents=True)
    (blob_dir / "image.bin").write_bytes(
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:deadbeef\n"
        b"size 99\n"
    )

    scratch = tmp_path / "scratch"
    cp = _argit(["restore", "--target", str(scratch)], cwd=repo, env=env)
    assert cp.returncode != 0
    assert "git lfs" in (cp.stdout + cp.stderr).lower() or "lfs pointer" in (cp.stdout + cp.stderr).lower()

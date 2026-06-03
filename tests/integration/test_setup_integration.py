"""Integration: argit setup against an ephemeral GPG keyring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import git_init_repo

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _argit(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    pythonpath = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "argit.cli", *args],
        cwd=str(cwd),
        env={**env, "PYTHONPATH": pythonpath},
        capture_output=True, text=True, timeout=60,
    )


def _generate_extra_key(gnupg_home: Path, name: str) -> str:
    batch = f"""
%no-protection
Key-Type: RSA
Key-Length: 2048
Name-Real: {name}
Name-Email: {name}@example.invalid
Expire-Date: 0
%commit
"""
    subprocess.run(
        ["gpg", "--batch", "--pinentry-mode", "loopback", "--gen-key"],
        input=batch, capture_output=True, text=True, timeout=120, check=True,
        env={**os.environ, "GNUPGHOME": str(gnupg_home)},
    )
    cp = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        capture_output=True, text=True, check=True,
        env={**os.environ, "GNUPGHOME": str(gnupg_home)},
    )
    fprs = [line.split(":")[9] for line in cp.stdout.splitlines() if line.startswith("fpr:")]
    return fprs[-1]


def test_setup_happy_path(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    cp = _argit(["setup", "--yes"], cwd=repo, env=env)
    assert cp.returncode == 0, f"stdout={cp.stdout}\nstderr={cp.stderr}"

    # AC 1: artifacts present (latest bundled revision)
    from argit.setup import _bundled_manifest_path
    assert (repo / ".argit" / "manifest" / _bundled_manifest_path().name).is_file()
    gitattributes = (repo / ".gitattributes").read_text()
    assert "openclaw/blob/**" in gitattributes
    assert "openclaw/sqlite/**" in gitattributes
    assert (repo / "secrets").is_dir()
    gpg_id = repo / "secrets" / ".gpg-id"
    assert gpg_id.is_file()
    recipients = gpg_id.read_text().splitlines()
    assert ephemeral_gpg_key in recipients
    assert "1107BD74F292CD3EAB0CF59D49F2D3353A88D34E" in recipients
    # IT key import would be attempted but the bundled .asc may collide with ephemeral key — verify presence regardless
    cp_keys = subprocess.run(["gpg", "--list-keys", "--with-colons"], env=env, capture_output=True, text=True, check=True)
    assert "1107BD74F292CD3EAB0CF59D49F2D3353A88D34E" in cp_keys.stdout
    assert "initialized pass store" in cp.stdout


def test_setup_idempotent(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    cp1 = _argit(["setup", "--yes"], cwd=repo, env=env)
    assert cp1.returncode == 0
    cp2 = _argit(["setup", "--yes"], cwd=repo, env=env)
    assert cp2.returncode == 0
    # Second invocation should report "already" for everything mutating
    assert "manifest already present" in cp2.stdout
    assert "already has the LFS line" in cp2.stdout
    assert "secrets/ already exists" in cp2.stdout
    assert "respecting existing secrets/.gpg-id (2 recipients)" in cp2.stdout


def test_setup_multi_key_requires_agent_key(tmp_path, gnupg_home, ephemeral_gpg_key):
    """AC 4: two personal keys, no --agent-key → exit 1 with candidate list."""
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    second_fpr = _generate_extra_key(gnupg_home, "argit-second")

    cp = _argit(["setup", "--yes"], cwd=repo, env=env)
    assert cp.returncode != 0
    assert "multiple personal GPG keys" in (cp.stdout + cp.stderr)
    assert "--agent-key" in (cp.stdout + cp.stderr)


def test_setup_with_explicit_agent_key(tmp_path, gnupg_home, ephemeral_gpg_key):
    """AC 5: --agent-key picks the requested key."""
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    second_fpr = _generate_extra_key(gnupg_home, "argit-second")

    cp = _argit(["setup", "--yes", "--agent-key", second_fpr], cwd=repo, env=env)
    assert cp.returncode == 0, f"stdout={cp.stdout}\nstderr={cp.stderr}"
    assert second_fpr in cp.stdout


def test_setup_greenfield_byo_it_recipient(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    foreign = _generate_extra_key(gnupg_home, "argit-foreign")

    cp = _argit(
        ["setup", "--yes", "--agent-key", ephemeral_gpg_key, "--it-recipient", foreign],
        cwd=repo,
        env=env,
    )
    assert cp.returncode == 0, f"stdout={cp.stdout}\nstderr={cp.stderr}"

    recipients = (repo / "secrets" / ".gpg-id").read_text().splitlines()
    assert recipients == [ephemeral_gpg_key, foreign]
    cp_keys = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert "1107BD74F292CD3EAB0CF59D49F2D3353A88D34E" not in cp_keys.stdout
    cp_trust = subprocess.run(
        ["gpg", "--export-ownertrust"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert f"{foreign}:4:" in cp_trust.stdout


def test_setup_greenfield_byo_missing_key_fails_without_gpg_id(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    missing = "C" * 40

    cp = _argit(["setup", "--yes", "--it-recipient", missing], cwd=repo, env=env)
    assert cp.returncode != 0
    assert missing in cp.stderr
    assert "gpg --import" in cp.stderr
    assert not (repo / "secrets" / ".gpg-id").exists()


def test_setup_respects_existing_foreign_gpg_id(tmp_path, gnupg_home, ephemeral_gpg_key):
    repo = tmp_path / "repo"; repo.mkdir()
    git_init_repo(repo)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    foreign = _generate_extra_key(gnupg_home, "argit-existing-foreign")
    secrets = repo / "secrets"
    secrets.mkdir()
    original = f"{ephemeral_gpg_key}\n{foreign}\n"
    (secrets / ".gpg-id").write_text(original)

    cp = _argit(["setup", "--yes"], cwd=repo, env=env)
    assert cp.returncode == 0, f"stdout={cp.stdout}\nstderr={cp.stderr}"
    assert (secrets / ".gpg-id").read_text() == original
    assert "respecting existing secrets/.gpg-id (2 recipients)" in cp.stdout
    assert ephemeral_gpg_key in cp.stdout
    assert foreign in cp.stdout
    cp_keys = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert "1107BD74F292CD3EAB0CF59D49F2D3353A88D34E" not in cp_keys.stdout

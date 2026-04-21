"""Integration-test fixtures: ephemeral GPG home, ephemeral pass store, isolated repo.

Skipped automatically when host binaries (`gpg`, `pass`, `sqlite3`, `git`) are
missing — these tests target the nix dev shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

REQUIRED_BINS = ("gpg", "pass", "sqlite3", "git", "git-lfs")


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when REQUIRED_BINS aren't on PATH.

    Git-lfs filter is configured per-test-repo via `git lfs install --local`
    (see `git_init_repo` fixture below), so we don't need a global filter to
    be configured.
    """
    integration_dir = Path(__file__).resolve().parent
    missing = [b for b in REQUIRED_BINS if shutil.which(b) is None]
    if not missing:
        return
    skip = pytest.mark.skip(
        reason=f"missing host binaries: {missing} — install them or run inside the nix dev shell"
    )
    for item in items:
        try:
            item_path = Path(str(item.fspath))
        except Exception:
            continue
        if integration_dir in item_path.parents:
            item.add_marker(skip)


def git_init_repo(repo: Path) -> None:
    """Init a fresh git repo with local user identity and LFS filter installed.

    `git lfs install --local` writes only to `<repo>/.git/config` — the
    operator's global git config is left untouched. This is the per-repo
    setup `argit setup` would do in real life, applied here so tests are
    self-contained.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "argit-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "argit-test"], check=True)
    subprocess.run(["git", "lfs", "install", "--local"], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def gnupg_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Iterator[Path]:
    """Short-path GNUPGHOME — gpg-agent's UNIX socket can't exceed ~104 chars on macOS.

    pytest's default tmp_path under /var/folders/... is already too deep, so we
    create a fresh shallow dir under /tmp for each test and clean up after.
    """
    import tempfile
    import shutil as _shutil
    gh = Path(tempfile.mkdtemp(prefix="ag-", dir="/tmp"))
    gh.chmod(0o700)
    monkeypatch.setenv("GNUPGHOME", str(gh))
    try:
        yield gh
    finally:
        # gpg-agent may still hold the socket open; ignore errors.
        subprocess.run(["gpgconf", "--kill", "gpg-agent"],
                       env={**os.environ, "GNUPGHOME": str(gh)},
                       capture_output=True, timeout=10, check=False)
        _shutil.rmtree(gh, ignore_errors=True)


@pytest.fixture
def ephemeral_gpg_key(gnupg_home: Path) -> str:
    """Generate an ephemeral RSA key with no passphrase. Returns its fingerprint."""
    batch = """
%no-protection
Key-Type: RSA
Key-Length: 2048
Name-Real: argit-test
Name-Email: argit-test@example.invalid
Expire-Date: 0
%commit
"""
    cp = subprocess.run(
        ["gpg", "--batch", "--pinentry-mode", "loopback", "--gen-key"],
        input=batch, capture_output=True, text=True, timeout=120, check=True,
        env={**os.environ, "GNUPGHOME": str(gnupg_home)},
    )
    cp_fpr = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        capture_output=True, text=True, check=True,
        env={**os.environ, "GNUPGHOME": str(gnupg_home)},
    )
    for line in cp_fpr.stdout.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    pytest.fail("could not extract fingerprint from generated key")
    return ""  # for type-checker


@pytest.fixture
def repo_root(tmp_path: Path, isolated_home) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "argit-test@example.invalid"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "argit-test"], cwd=str(repo), check=True)
    return repo

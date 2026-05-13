"""Blob backup handling for nested Git repositories."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from argit.backup import GIT_REMOTE_INFO_FILENAME, _copy_blob_tree, _redact_remote_url


def test_redact_remote_url_removes_url_userinfo():
    assert (
        _redact_remote_url("https://token@example.com/org/repo.git")
        == "https://<redacted>@example.com/org/repo.git"
    )
    assert (
        _redact_remote_url("https://user:token@example.com/org/repo.git")
        == "https://<redacted>@example.com/org/repo.git"
    )
    assert _redact_remote_url("git@github.com:org/repo.git") == "git@github.com:org/repo.git"


def test_copy_blob_tree_strips_nested_git_and_writes_remote_info(tmp_path):
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    nested = src / "skills" / "humanizer"
    (nested / ".git" / "objects" / "pack").mkdir(parents=True)
    (nested / ".git" / "objects" / "pack" / "pack-test.pack").write_text("pack")
    (nested / "README.md").write_text("hello")

    def metadata(repo: Path, args: list[str]) -> str | None:
        assert repo == nested
        if args == ["remote", "-v"]:
            return (
                "origin\thttps://token@github.com/example/humanizer.git (fetch)\n"
                "origin\thttps://token@github.com/example/humanizer.git (push)"
            )
        if args == ["branch", "--show-current"]:
            return "main"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        if args == ["status", "--short"]:
            return " M README.md"
        raise AssertionError(args)

    with patch("argit.backup._run_git_metadata", side_effect=metadata):
        _copy_blob_tree(src, tgt)

    restored_nested = tgt / "skills" / "humanizer"
    assert (restored_nested / "README.md").read_text() == "hello"
    assert not (restored_nested / ".git").exists()
    info = (restored_nested / GIT_REMOTE_INFO_FILENAME).read_text()
    assert "https://<redacted>@github.com/example/humanizer.git" in info
    assert "https://token@" not in info
    assert "- Branch: `main`" in info
    assert "- HEAD: `abc123`" in info
    assert "- ` M README.md`" in info


def test_copy_blob_tree_removes_stale_destination_git_dir(tmp_path):
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    (src / "repo").mkdir(parents=True)
    (src / "repo" / "file.txt").write_text("new")
    stale_pack = tgt / "repo" / ".git" / "objects" / "pack" / "pack-test.pack"
    stale_pack.parent.mkdir(parents=True)
    stale_pack.write_text("old")
    stale_pack.chmod(0o400)

    _copy_blob_tree(src, tgt)

    assert (tgt / "repo" / "file.txt").read_text() == "new"
    assert not (tgt / "repo" / ".git").exists()

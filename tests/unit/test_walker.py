"""Disk walker: prune dirs, dedup symlinks, detect ``.git`` as file or dir."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fleet.walker import looks_like_git_repo, walk_repos
from helpers.git import write_marker_repo


def test_looks_like_git_repo_dir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert looks_like_git_repo(tmp_path) is True


def test_looks_like_git_repo_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert looks_like_git_repo(tmp_path) is True


def test_looks_like_git_repo_negative(tmp_path: Path) -> None:
    assert looks_like_git_repo(tmp_path) is False


def test_walk_yields_top_level_repo(tmp_path: Path) -> None:
    repo = write_marker_repo(tmp_path / "alpha")
    found = list(walk_repos(tmp_path))
    assert repo in found


def test_walk_yields_nested_repo(tmp_path: Path) -> None:
    nested = write_marker_repo(tmp_path / "group" / "alpha")
    found = list(walk_repos(tmp_path))
    assert nested in found


def test_walk_descends_into_repos(tmp_path: Path) -> None:
    """Convention: nested repos under temp/ are still discovered."""
    parent = write_marker_repo(tmp_path / "outer")
    inner = write_marker_repo(parent / "temp" / "inner")
    found = set(walk_repos(tmp_path))
    assert parent in found
    assert inner in found


def test_walk_prunes_node_modules(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "node_modules" / "should-not-find")
    assert list(walk_repos(tmp_path)) == []


def test_walk_prunes_dotted_dirs(tmp_path: Path) -> None:
    for name in (".venv", ".tox", "__pycache__", "dist"):
        write_marker_repo(tmp_path / name / "x")
    assert list(walk_repos(tmp_path)) == []


def test_walk_prunes_tool_home(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "..me" / "self")
    assert list(walk_repos(tmp_path)) == []


def test_walk_skips_fleet_install_dir(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet install dir itself is excluded even when it sits inside the root."""
    fake_install = tmp_path / "self-install"
    write_marker_repo(fake_install)
    monkeypatch.setattr("fleet.walker.fleet_install_dir",
                        lambda: fake_install)
    assert fake_install not in list(walk_repos(tmp_path))


def test_walk_dedups_symlinks(tmp_path: Path) -> None:
    """Two paths that resolve to the same dir must yield only once."""
    target = write_marker_repo(tmp_path / "real")
    link = tmp_path / "shortcut"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform == "win32":
            pytest.skip("symlink creation not permitted on this Windows")
        raise
    found = list(walk_repos(tmp_path))
    # ``target`` itself is yielded; the symlink resolves to the same path
    # and is therefore deduped.
    assert found.count(target) == 1
    assert link not in found


def test_walk_handles_unreadable_root(tmp_path: Path) -> None:
    """A root that doesn't exist returns an empty iterator (no exception)."""
    assert list(walk_repos(tmp_path / "nope")) == []

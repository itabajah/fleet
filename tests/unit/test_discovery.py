"""Discovery layer: combines walker + registry-tree resolution."""

from __future__ import annotations

import json
from pathlib import Path

from fleet.discovery import RepoInfo, discover_repos, repos_to_sync
from fleet.state import set_active_fleet
from helpers.git import write_marker_repo


def _write_registry(root: Path, payload: dict) -> None:
    (root / "fleet.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def test_repoinfo_display_name_top_level() -> None:
    r = RepoInfo("repo", "", Path("x"), True, True)
    assert r.display_name == "repo"
    assert r.parts == ("repo",)


def test_repoinfo_display_name_grouped() -> None:
    r = RepoInfo("repo", "group/sub", Path("x"), True, True)
    assert r.display_name == "group/sub/repo"
    assert r.parts == ("group", "sub", "repo")


def test_discover_returns_empty_when_root_missing(tmp_path: Path) -> None:
    set_active_fleet("demo", tmp_path)
    # Sync root is repos root; both exist but no repos under it.
    assert discover_repos() == []


def test_discover_finds_top_and_nested(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    write_marker_repo(tmp_path / "group" / "beta")
    set_active_fleet("demo", tmp_path)
    repos = discover_repos()
    by_name = {r.display_name for r in repos}
    assert by_name == {"alpha", "group/beta"}


def test_discover_marks_in_registry(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    _write_registry(tmp_path, {".": {"sync": True, "repos": ["alpha"]}})
    set_active_fleet("demo", tmp_path)
    [r] = discover_repos()
    assert r.in_registry is True
    assert r.enabled is True


def test_discover_marks_disabled(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "vendored" / "alpha")
    _write_registry(tmp_path, {"vendored": {"sync": False}})
    set_active_fleet("demo", tmp_path)
    [r] = discover_repos()
    assert r.enabled is False


def test_discover_excluded(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    _write_registry(tmp_path, {
        ".": {"sync": True, "repos": ["alpha"], "exclude": ["alpha"]},
    })
    set_active_fleet("demo", tmp_path)
    [r] = discover_repos()
    assert r.enabled is False
    assert r.in_registry is True


def test_discover_sorted_by_group_then_name(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "z-top")
    write_marker_repo(tmp_path / "a-group" / "b-repo")
    write_marker_repo(tmp_path / "a-group" / "a-repo")
    set_active_fleet("demo", tmp_path)
    names = [r.display_name for r in discover_repos()]
    # Top-level (group "") sorts first; within a group, alphabetical.
    assert names == ["z-top", "a-group/a-repo", "a-group/b-repo"]


def test_repos_to_sync_filters_to_enabled_in_registry(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "in-reg")
    write_marker_repo(tmp_path / "disk-only")
    _write_registry(tmp_path, {".": {"sync": True, "repos": ["in-reg"]}})
    set_active_fleet("demo", tmp_path)
    repos = repos_to_sync()
    assert [r.name for r in repos] == ["in-reg"]


def test_discover_respects_root_field(tmp_path: Path) -> None:
    """Registry's ``root`` field re-bases the sync root."""
    sub = tmp_path / "sub"
    sub.mkdir()
    write_marker_repo(sub / "alpha")
    _write_registry(tmp_path, {"root": "sub"})
    set_active_fleet("demo", tmp_path)
    [r] = discover_repos()
    assert r.name == "alpha"
    # group_path is relative to the configured sync root, not the repos root.
    assert r.group_path == ""

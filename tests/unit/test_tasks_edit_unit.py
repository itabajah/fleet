"""Unit tests for the new Manifest mutation helpers and validation extension."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.errors import FleetError
from fleet.tasks.manifest import Manifest, RepoEntry, now_iso
from fleet.tasks.validation import require_repo_in_task

pytestmark = pytest.mark.unit


def _mk_manifest(workspace: Path, repos: list[tuple[str, str | None]]) -> Manifest:
    return Manifest(
        name="t",
        branch="task/demo/t",
        created_at=now_iso(),
        description="",
        repos=[
            RepoEntry(
                name=n,
                group=g,
                canonical_path=workspace / "_canon" / n,
                worktree_path=workspace / n,
            )
            for n, g in repos
        ],
    )


def test_add_repos_appends_and_round_trips(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", None)])
    m.add_repos([
        RepoEntry(name="beta", group=None,
                  canonical_path=tmp_path / "_canon" / "beta",
                  worktree_path=tmp_path / "beta"),
    ])
    m.save(tmp_path)
    reloaded = Manifest.load(tmp_path)
    assert [r.name for r in reloaded.repos] == ["alpha", "beta"]


def test_remove_repos_drops_and_returns(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", None), ("beta", None), ("gamma", "g")])
    dropped = m.remove_repos({"beta", "gamma"})
    assert {r.name for r in dropped} == {"beta", "gamma"}
    assert [r.name for r in m.repos] == ["alpha"]
    m.save(tmp_path)
    reloaded = Manifest.load(tmp_path)
    assert [r.name for r in reloaded.repos] == ["alpha"]


def test_remove_repos_unknown_name_is_noop(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", None)])
    assert m.remove_repos({"nope"}) == []
    assert [r.name for r in m.repos] == ["alpha"]


def test_rename_repoints_name_branch_and_worktrees(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", None), ("beta", "g")])
    new_ws = tmp_path.parent / "renamed"
    m.rename("renamed", "task/demo/renamed", new_ws)
    assert m.name == "renamed"
    assert m.branch == "task/demo/renamed"
    assert m.repos[0].worktree_path == new_ws / "alpha"
    assert m.repos[1].worktree_path == new_ws / "beta"
    # canonical untouched
    assert m.repos[0].canonical_path == tmp_path / "_canon" / "alpha"


def test_require_repo_in_task_resolves_leaf(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", None), ("beta", "g")])
    assert require_repo_in_task("alpha", m).name == "alpha"
    assert require_repo_in_task("g/beta", m).name == "beta"
    # Leaf-only lookup also works for grouped entries when unique.
    assert require_repo_in_task("beta", m).name == "beta"


def test_require_repo_in_task_miss_lists_members(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", None)])
    with pytest.raises(FleetError, match="not in task 't'") as ei:
        require_repo_in_task("missing", m)
    assert "alpha" in str(ei.value)


def test_require_repo_in_task_ambiguous(tmp_path: Path) -> None:
    m = _mk_manifest(tmp_path, [("alpha", "g1"), ("alpha", "g2")])
    with pytest.raises(FleetError, match="Ambiguous"):
        require_repo_in_task("alpha", m)

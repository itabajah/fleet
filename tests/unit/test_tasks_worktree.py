"""Unit tests for ``fleet.tasks.worktree`` pure helpers.

The git-touching helpers (``prepare_canonical`` / ``add_worktree``) are
exercised by the integration suite; here we cover the pure leaf-collision
check that runs BEFORE any disk mutation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.tasks.worktree import assert_no_leaf_collision


_CASE_INSENSITIVE_FS = os.path.normcase("A") == os.path.normcase("a")


def _ri(name: str, group: str = "") -> RepoInfo:
    return RepoInfo(name=name, group_path=group, path=Path("/x"),
                    enabled=True, in_registry=True)


def test_no_collision_passes(tmp_path: Path) -> None:
    assert_no_leaf_collision([_ri("alpha"), _ri("beta")], tmp_path)


def test_two_new_repos_same_leaf_rejected(tmp_path: Path) -> None:
    """Two selected repos with the same leaf name (different groups) would
    land at the same worktree path and the second ``git worktree add`` would
    fail mid-scaffold. Catch it BEFORE any mutation."""
    repos = [_ri("alpha", "group-a"), _ri("alpha", "group-b")]
    with pytest.raises(FleetError, match="share leaf name 'alpha'"):
        assert_no_leaf_collision(repos, tmp_path)


def test_collision_with_existing_leaf_rejected(tmp_path: Path) -> None:
    with pytest.raises(FleetError, match="already exists"):
        assert_no_leaf_collision([_ri("alpha")], tmp_path,
                                 existing_leaves={"alpha"})


@pytest.mark.skipif(
    not _CASE_INSENSITIVE_FS,
    reason="case-sensitive filesystem; 'Foo' and 'foo' are distinct dirs",
)
def test_case_insensitive_collision_caught(tmp_path: Path) -> None:
    """On Windows / default macOS, ``Foo`` and ``foo`` resolve to the same
    directory. The second ``git worktree add`` would fail with a confusing
    'already exists' message — pre-flight catches it with a clear error."""
    repos = [_ri("Foo"), _ri("foo")]
    with pytest.raises(FleetError, match="share leaf name"):
        assert_no_leaf_collision(repos, tmp_path)


@pytest.mark.skipif(
    not _CASE_INSENSITIVE_FS,
    reason="case-sensitive filesystem; 'Foo' and 'foo' are distinct dirs",
)
def test_case_insensitive_collision_against_existing(tmp_path: Path) -> None:
    with pytest.raises(FleetError, match="already exists"):
        assert_no_leaf_collision([_ri("FOO")], tmp_path,
                                 existing_leaves={"foo"})


@pytest.mark.skipif(
    _CASE_INSENSITIVE_FS,
    reason="case-insensitive filesystem; would (correctly) reject",
)
def test_case_sensitive_filesystems_keep_distinct(tmp_path: Path) -> None:
    """On Linux ``Foo`` and ``foo`` are legitimately different directories
    so the check must NOT false-positive."""
    assert_no_leaf_collision([_ri("Foo"), _ri("foo")], tmp_path)

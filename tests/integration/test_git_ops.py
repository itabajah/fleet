"""git_ops against real local bare remotes — fetch / pull / detect_default_branch / unpushed_count."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fleet import git_ops
from fleet.errors import FleetError
from helpers.git import _git, commit_file, make_dirty, push_branch


def test_is_git_repo(cloned_repo_factory: Callable[..., Path], tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    assert git_ops.is_git_repo(repo) is True
    assert git_ops.is_git_repo(tmp_path / "not-a-repo") is False


def test_origin_url_returns_remote(cloned_repo_factory: Callable[..., Path],
                                   tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    url = git_ops.origin_url(repo)
    assert url is not None
    assert url.endswith(".git")


def test_current_branch_main(cloned_repo_factory: Callable[..., Path],
                             tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    assert git_ops.current_branch(repo) == "main"


def test_is_dirty_clean_then_dirty(cloned_repo_factory: Callable[..., Path],
                                   tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    assert git_ops.is_dirty(repo) is False
    make_dirty(repo)
    assert git_ops.is_dirty(repo) is True


def test_detect_default_branch_online(cloned_repo_factory: Callable[..., Path],
                                      tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    assert git_ops.detect_default_branch(repo) == "main"


def test_detect_default_branch_offline(cloned_repo_factory: Callable[..., Path],
                                       tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    # Already cached origin/HEAD via clone_into; offline path uses local refs.
    assert git_ops.detect_default_branch(repo, offline=True) == "main"


def test_detect_default_branch_offline_fails_when_no_local(
    bare_remote_factory: Callable[..., Path], tmp_path: Path,
) -> None:
    """Without local origin/HEAD or origin/main, offline detection raises."""
    bare = bare_remote_factory("dummy")
    # Hand-clone to skip the clone_into post-setup that runs `set-head`.
    repo = tmp_path / "r"
    _git("clone", str(bare), str(repo), cwd=tmp_path)
    # Strip the symbolic ref so the fast path can't use it.
    head = repo / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    if head.is_file():
        head.unlink()
    # Strip origin/main too so the last-resort probe also fails.
    refs = repo / ".git" / "refs" / "remotes" / "origin"
    for f in refs.iterdir():
        f.unlink()
    # Also strip from packed-refs (if it ended up there).
    packed = repo / ".git" / "packed-refs"
    if packed.is_file():
        packed.write_text("\n", encoding="utf-8")
    with pytest.raises(FleetError):
        git_ops.detect_default_branch(repo, offline=True)


def test_pull_ff_only_succeeds_when_in_sync(
    cloned_repo_factory: Callable[..., Path], tmp_path: Path,
) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    res = git_ops.pull_ff_only(repo, "main")
    assert res.ok


def test_fetch_prune_succeeds(cloned_repo_factory: Callable[..., Path],
                              tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    res = git_ops.fetch_prune(repo)
    assert res.ok


def test_unpushed_count_unknown_for_local_only_branch(
    cloned_repo_factory: Callable[..., Path], tmp_path: Path,
) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    # Make a new local branch that origin doesn't know about.
    _git("checkout", "-b", "feature", cwd=repo)
    commit_file(repo, "x.txt")
    assert git_ops.unpushed_count(repo, "feature") is None


def test_unpushed_count_zero_when_in_sync(
    cloned_repo_factory: Callable[..., Path], tmp_path: Path,
) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    _git("checkout", "-b", "feature", cwd=repo)
    commit_file(repo, "x.txt")
    push_branch(repo, "feature")
    assert git_ops.unpushed_count(repo, "feature") == 0


def test_unpushed_count_positive(cloned_repo_factory: Callable[..., Path],
                                 tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    _git("checkout", "-b", "feature", cwd=repo)
    commit_file(repo, "x.txt")
    push_branch(repo, "feature")
    commit_file(repo, "y.txt")
    commit_file(repo, "z.txt")
    assert git_ops.unpushed_count(repo, "feature") == 2


def test_run_git_raises_for_missing_cwd(tmp_path: Path) -> None:
    with pytest.raises(FleetError, match="does not exist"):
        git_ops.run_git("status", cwd=tmp_path / "nope")


def test_run_git_check_raises_on_failure(cloned_repo_factory: Callable[..., Path],
                                         tmp_path: Path) -> None:
    repo = cloned_repo_factory(tmp_path / "r")
    with pytest.raises(FleetError, match="git checkout"):
        git_ops.run_git("checkout", "no-such-branch-anywhere",
                        cwd=repo, check=True)

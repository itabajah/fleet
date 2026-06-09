"""``fleet task new`` happy paths and rollback."""

from __future__ import annotations

import argparse

import pytest

from fleet.errors import FleetError
from fleet.state import tasks_root
from fleet.tasks.lifecycle import cmd_new
from fleet.tasks.manifest import Manifest


def _new_args(name: str, repos: str, **overrides) -> argparse.Namespace:
    base = {
        "name": name,
        "repos": repos,
        "description": "",
        "no_pull": True,
        "dry_run": False,
        "fleet": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_dry_run_makes_no_changes(populated_fleet, capsys) -> None:
    rc = cmd_new(_new_args("t1", "alpha", dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "would pull each canonical" in out
    assert not (tasks_root() / "t1").exists()


def test_happy_path_creates_workspace(populated_fleet) -> None:
    rc = cmd_new(_new_args("t1", "alpha,beta"))
    assert rc == 0
    workspace = tasks_root() / "t1"
    assert workspace.is_dir()
    assert (workspace / "alpha" / ".git").exists()
    assert (workspace / "beta" / ".git").exists()
    assert (workspace / "context.md").is_file()
    assert (workspace / "scratch").is_dir()
    assert (workspace / "task.json").is_file()
    manifest = Manifest.load(workspace)
    assert manifest.branch == "task/demo/t1"
    assert {r.name for r in manifest.repos} == {"alpha", "beta"}


def test_creates_branch_in_canonical(populated_fleet) -> None:
    cmd_new(_new_args("t1", "alpha"))
    from helpers.git import _git
    out = _git("branch", "--list", "task/demo/t1",
               cwd=populated_fleet.repos["alpha"]).stdout
    assert "task/demo/t1" in out


def test_custom_branch_convention(populated_fleet) -> None:
    """An unscoped custom convention names the branch ``<prefix>/<name>``."""
    from fleet.state import BranchConfig, set_active_fleet
    set_active_fleet("demo", populated_fleet.root,
                     BranchConfig(prefix="wip", scoped=False))
    cmd_new(_new_args("t1", "alpha"))
    manifest = Manifest.load(tasks_root() / "t1")
    assert manifest.branch == "wip/t1"
    from helpers.git import _git
    out = _git("branch", "--list", "wip/t1",
               cwd=populated_fleet.repos["alpha"]).stdout
    assert "wip/t1" in out


def test_rejects_existing_task_name(populated_fleet) -> None:
    cmd_new(_new_args("t1", "alpha"))
    with pytest.raises(FleetError, match="already exists"):
        cmd_new(_new_args("t1", "beta"))


def test_rejects_invalid_task_name(populated_fleet) -> None:
    with pytest.raises(FleetError, match="Invalid task name"):
        cmd_new(_new_args("bad name", "alpha"))


def test_rejects_unknown_repo(populated_fleet) -> None:
    with pytest.raises(FleetError, match="Unknown repo"):
        cmd_new(_new_args("t1", "ghost"))


def test_rejects_empty_repos_list(populated_fleet) -> None:
    with pytest.raises(FleetError, match="--repos requires"):
        cmd_new(_new_args("t1", ""))


def test_rejects_branch_already_in_canonical(populated_fleet) -> None:
    """If `task/demo/t1` already exists locally, refuse before mutating disk."""
    from helpers.git import _git
    _git("branch", "task/demo/t1", "main",
         cwd=populated_fleet.repos["alpha"])
    with pytest.raises(FleetError, match="already exists"):
        cmd_new(_new_args("t1", "alpha"))
    # Workspace cleaned up after rollback.
    assert not (tasks_root() / "t1").exists()


def test_rejects_branch_already_on_origin(populated_fleet) -> None:
    """If `task/demo/t1` exists on origin, refuse and roll back."""
    from helpers.git import _git, push_branch
    repo = populated_fleet.repos["alpha"]
    _git("checkout", "-b", "task/demo/t1", cwd=repo)
    push_branch(repo, "task/demo/t1")
    _git("checkout", "main", cwd=repo)
    _git("branch", "-D", "task/demo/t1", cwd=repo)
    with pytest.raises(FleetError, match="already exists on origin"):
        cmd_new(_new_args("t1", "alpha"))


def test_no_pull_offline(populated_fleet, capsys) -> None:
    rc = cmd_new(_new_args("t1", "alpha", no_pull=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipping fetch + pull" in out


def test_dedup_repeated_repo_token(populated_fleet, capsys) -> None:
    rc = cmd_new(_new_args("t1", "alpha,alpha"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ignoring duplicate" in out
    manifest = Manifest.load(tasks_root() / "t1")
    assert len(manifest.repos) == 1


def test_branch_uses_no_track(populated_fleet) -> None:
    """The new branch must NOT have an upstream — that's deliberate."""
    cmd_new(_new_args("t1", "alpha"))
    from helpers.git import _git
    upstream = _git("for-each-ref", "--format=%(upstream)",
                    "refs/heads/task/demo/t1",
                    cwd=populated_fleet.repos["alpha"]).stdout.strip()
    assert upstream == ""

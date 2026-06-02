"""Integration tests for ``task add-repo`` / ``remove-repo`` / ``rename`` / ``edit``."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pytest

from fleet import git_ops
from fleet.errors import FleetError
from fleet.state import tasks_root
from fleet.tasks.edit import (
    cmd_add_repo,
    cmd_edit,
    cmd_remove_repo,
    cmd_rename,
)
from fleet.tasks.lifecycle import cmd_end, cmd_new
from fleet.tasks.manifest import Manifest
from helpers.git import make_dirty


# ---------------------------------------------------------------------------
# argparse.Namespace builders
# ---------------------------------------------------------------------------

def _new_args(name: str, repos: str) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, repos=repos, description="", no_pull=True,
        dry_run=False, fleet=None,
    )


def _add_args(name: str, repos: str, *, no_pull: bool = True,
              dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, repos=repos, no_pull=no_pull, dry_run=dry_run, fleet=None,
    )


def _rm_args(name: str, repos: str, *, force: bool = False,
             dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, repos=repos, force=force, dry_run=dry_run, fleet=None,
    )


def _rename_args(old: str, new: str) -> argparse.Namespace:
    return argparse.Namespace(old=old, new=new, fleet=None)


def _edit_args(name: str, *, description: str | None = None,
               description_file: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, description=description,
        description_file=description_file, fleet=None,
    )


# ---------------------------------------------------------------------------
# add-repo
# ---------------------------------------------------------------------------

class TestAddRepo:
    def test_happy_path(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        rc = cmd_add_repo(_add_args("t", "beta"))
        assert rc == 0
        ws = tasks_root() / "t"
        assert (ws / "beta" / ".git").exists()
        m = Manifest.load(ws)
        assert [r.name for r in m.repos] == ["alpha", "beta"]

    def test_duplicate_repo_rejected(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="already in task"):
            cmd_add_repo(_add_args("t", "alpha"))
        m = Manifest.load(tasks_root() / "t")
        assert [r.name for r in m.repos] == ["alpha"]

    def test_unknown_repo(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="Unknown repo"):
            cmd_add_repo(_add_args("t", "nope"))

    def test_dry_run_no_mutation(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        rc = cmd_add_repo(_add_args("t", "beta", dry_run=True))
        assert rc == 0
        ws = tasks_root() / "t"
        assert not (ws / "beta").exists()
        assert [r.name for r in Manifest.load(ws).repos] == ["alpha"]

    def test_rollback_on_mid_failure(self, populated_fleet, capsys,
                                     monkeypatch) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()

        real_run_git = git_ops.run_git
        state = {"adds_seen": 0}

        def fake_run_git(*args, **kwargs):
            if args[:2] == ("worktree", "add"):
                state["adds_seen"] += 1
                if state["adds_seen"] == 2:
                    raise FleetError("injected failure on 2nd worktree add")
            return real_run_git(*args, **kwargs)

        monkeypatch.setattr(git_ops, "run_git", fake_run_git)

        with pytest.raises(FleetError, match="injected failure"):
            cmd_add_repo(_add_args("t", "beta,group/gamma"))

        ws = tasks_root() / "t"
        assert [r.name for r in Manifest.load(ws).repos] == ["alpha"]
        assert not (ws / "beta").exists()
        assert not (ws / "gamma").exists()

    def test_remove_then_add_reattaches_existing_branch(
        self, populated_fleet, capsys,
    ) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()
        cmd_remove_repo(_rm_args("t", "beta"))
        capsys.readouterr()
        # Branch still exists in beta's canonical; re-adding must reattach
        # rather than error out with "branch already exists".
        rc = cmd_add_repo(_add_args("t", "beta"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "reattached existing branch" in out
        assert (tasks_root() / "t" / "beta" / ".git").exists()
        assert [r.name for r in Manifest.load(tasks_root() / "t").repos] \
            == ["alpha", "beta"]


class TestRemoveRepo:
    def test_happy_path(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()
        rc = cmd_remove_repo(_rm_args("t", "beta"))
        assert rc == 0
        ws = tasks_root() / "t"
        assert not (ws / "beta").exists()
        assert [r.name for r in Manifest.load(ws).repos] == ["alpha"]

    def test_dirty_refusal_without_force(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()
        make_dirty(tasks_root() / "t" / "beta")
        with pytest.raises(FleetError, match="uncommitted changes"):
            cmd_remove_repo(_rm_args("t", "beta"))
        assert (tasks_root() / "t" / "beta").exists()

    def test_force_removes_dirty(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()
        make_dirty(tasks_root() / "t" / "beta")
        rc = cmd_remove_repo(_rm_args("t", "beta", force=True))
        assert rc == 0
        assert not (tasks_root() / "t" / "beta").exists()

    def test_repo_not_in_task(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="not in task"):
            cmd_remove_repo(_rm_args("t", "beta"))

    def test_last_repo_warns(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        rc = cmd_remove_repo(_rm_args("t", "alpha"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "no repos" in out
        assert Manifest.load(tasks_root() / "t").repos == []

    def test_missing_worktree_dir_cleanly_dropped(self, populated_fleet,
                                                  capsys) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()
        # Force-remove worktree externally to simulate a missing dir.
        ws = tasks_root() / "t"
        beta = ws / "beta"
        canonical = populated_fleet.repos["beta"]
        git_ops.run_git("worktree", "remove", "--force", str(beta),
                        cwd=canonical, check=False)
        assert not beta.exists()
        rc = cmd_remove_repo(_rm_args("t", "beta"))
        assert rc == 0
        assert [r.name for r in Manifest.load(ws).repos] == ["alpha"]


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

class TestRename:
    def test_happy_path(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("old", "alpha,beta"))
        capsys.readouterr()
        rc = cmd_rename(_rename_args("old", "new"))
        assert rc == 0
        root = tasks_root()
        assert not (root / "old").exists()
        assert (root / "new" / "task.json").is_file()
        m = Manifest.load(root / "new")
        assert m.name == "new"
        assert m.branch == "task/demo/new"
        assert m.repos[0].worktree_path == root / "new" / "alpha"
        # Branch actually renamed in each canonical
        for repo_name in ("alpha", "beta"):
            canonical = populated_fleet.repos[repo_name]
            r = git_ops.run_git("show-ref", "--verify", "--quiet",
                                "refs/heads/task/demo/new",
                                cwd=canonical, check=False)
            assert r.ok
            r2 = git_ops.run_git("show-ref", "--verify", "--quiet",
                                 "refs/heads/task/demo/old",
                                 cwd=canonical, check=False)
            assert not r2.ok
        # context.md header rewritten
        ctx = (root / "new" / "context.md").read_text(encoding="utf-8")
        assert ctx.startswith("# new\n")
        assert "task/demo/new" in ctx

    def test_post_rename_worktree_repaired(self, populated_fleet, capsys) -> None:
        """`git worktree remove` from the canonical must work after rename.

        Regression: without `git worktree repair` the canonical's
        `.git/worktrees/<leaf>/gitdir` still points at the pre-rename
        path, so `task end` (and any other worktree management op)
        fails with "is not a working tree".
        """
        cmd_new(_new_args("old", "alpha,beta"))
        capsys.readouterr()
        cmd_rename(_rename_args("old", "new"))
        capsys.readouterr()
        # cmd_end exercises `git worktree remove` from each canonical.
        rc = cmd_end(argparse.Namespace(name="new", force=True, fleet=None))
        assert rc == 0
        assert not (tasks_root() / "new").exists()

    def test_same_name_is_noop(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        rc = cmd_rename(_rename_args("t", "t"))
        assert rc == 0
        assert (tasks_root() / "t").is_dir()

    def test_new_exists_rejected(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("a", "alpha"))
        cmd_new(_new_args("b", "beta"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="already exists"):
            cmd_rename(_rename_args("a", "b"))
        # both workspaces still exist
        assert (tasks_root() / "a").is_dir()
        assert (tasks_root() / "b").is_dir()

    def test_invalid_new_name(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="Invalid task name"):
            cmd_rename(_rename_args("t", "bad..name"))

    def test_missing_canonical_branch_warns_and_continues(
        self, populated_fleet, capsys,
    ) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()
        # Manually delete the task branch from one canonical.
        canonical = populated_fleet.repos["beta"]
        # Need to remove the worktree first so we can delete the branch.
        ws_beta = tasks_root() / "t" / "beta"
        git_ops.run_git("worktree", "remove", "--force", str(ws_beta),
                        cwd=canonical, check=False)
        git_ops.run_git("branch", "-D", "task/demo/t",
                        cwd=canonical, check=False)

        rc = cmd_rename(_rename_args("t", "u"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "WARN" in out
        # alpha got renamed
        canonical_a = populated_fleet.repos["alpha"]
        r = git_ops.run_git("show-ref", "--verify", "--quiet",
                            "refs/heads/task/demo/u",
                            cwd=canonical_a, check=False)
        assert r.ok

    def test_branch_failure_rolls_back(self, populated_fleet, capsys,
                                       monkeypatch) -> None:
        cmd_new(_new_args("t", "alpha,beta"))
        capsys.readouterr()

        real = git_ops.rename_branch
        state = {"n": 0}

        def fake(repo_path, old, new):  # noqa: ANN001
            state["n"] += 1
            if state["n"] == 2:
                from fleet.git_ops import GitResult
                return GitResult(returncode=1, stdout="",
                                 stderr="fatal: simulated failure")
            return real(repo_path, old, new)

        monkeypatch.setattr(git_ops, "rename_branch", fake)
        # tasks.edit imported the symbol via `from fleet import git_ops`,
        # so this monkeypatch takes effect.

        with pytest.raises(FleetError, match="aborted"):
            cmd_rename(_rename_args("t", "u"))

        # Workspace not moved
        assert (tasks_root() / "t").is_dir()
        assert not (tasks_root() / "u").exists()
        # First canonical's branch reverted to old
        canonical_a = populated_fleet.repos["alpha"]
        r_old = git_ops.run_git("show-ref", "--verify", "--quiet",
                                "refs/heads/task/demo/t",
                                cwd=canonical_a, check=False)
        assert r_old.ok
        r_new = git_ops.run_git("show-ref", "--verify", "--quiet",
                                "refs/heads/task/demo/u",
                                cwd=canonical_a, check=False)
        assert not r_new.ok


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

class TestEdit:
    def test_description_text(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        rc = cmd_edit(_edit_args("t", description="hello world"))
        assert rc == 0
        m = Manifest.load(tasks_root() / "t")
        assert m.description == "hello world"
        ctx = (tasks_root() / "t" / "context.md").read_text(encoding="utf-8")
        assert "hello world" in ctx

    def test_description_file_stdin(self, populated_fleet, capsys,
                                    monkeypatch) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin"))
        rc = cmd_edit(_edit_args("t", description_file="-"))
        assert rc == 0
        assert Manifest.load(tasks_root() / "t").description == "from stdin"

    def test_description_file_path(self, populated_fleet, capsys,
                                   tmp_path: Path) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        f = tmp_path / "desc.txt"
        f.write_text("from file", encoding="utf-8")
        rc = cmd_edit(_edit_args("t", description_file=str(f)))
        assert rc == 0
        assert Manifest.load(tasks_root() / "t").description == "from file"

    def test_no_args_errors(self, populated_fleet, capsys) -> None:
        cmd_new(_new_args("t", "alpha"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="needs --description"):
            cmd_edit(_edit_args("t"))

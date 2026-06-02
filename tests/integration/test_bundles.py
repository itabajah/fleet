"""Integration tests for ``fleet bundles ...`` and ``@bundle`` consumption."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from fleet.bundles_config import BundlesConfig
from fleet.bundles_commands import (
    cmd_bundles_add,
    cmd_bundles_edit,
    cmd_bundles_list,
    cmd_bundles_remove,
    cmd_bundles_show,
)
from fleet.errors import FleetError
from fleet.state import bundles_path, set_active_fleet, tasks_root
from fleet.tasks.edit import cmd_add_repo, cmd_remove_repo
from fleet.tasks.lifecycle import cmd_new
from fleet.tasks.manifest import Manifest


# ---------------------------------------------------------------------------
# argparse.Namespace builders
# ---------------------------------------------------------------------------

def _add_args(name: str, repos: str, *, force: bool = False
              ) -> argparse.Namespace:
    return argparse.Namespace(name=name, repos=repos, force=force, fleet=None)


def _edit_args(name: str, *, add: str | None = None,
               remove: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(name=name, add=add, remove=remove, fleet=None)


def _show_args(name: str) -> argparse.Namespace:
    return argparse.Namespace(name=name, fleet=None)


def _new_task(name: str, repos: str) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, repos=repos, description="", no_pull=True,
        dry_run=False, fleet=None,
    )


def _add_repo_args(name: str, repos: str) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, repos=repos, no_pull=True, dry_run=False, fleet=None,
    )


def _rm_repo_args(name: str, repos: str) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, repos=repos, force=False, dry_run=False, fleet=None,
    )


# ---------------------------------------------------------------------------
# bundles CRUD
# ---------------------------------------------------------------------------

class TestBundlesCrud:
    def test_add_writes_ordered_tokens(self, populated_fleet, capsys) -> None:
        rc = cmd_bundles_add(_add_args("core", "alpha,group/gamma"))
        assert rc == 0
        data = json.loads(bundles_path().read_text(encoding="utf-8"))
        assert data == {"bundles": {"core": ["alpha", "group/gamma"]}}
        assert "✓ Bundle 'core' created" in capsys.readouterr().out

    def test_add_duplicate_without_force_rejected(
        self, populated_fleet, capsys
    ) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="already exists"):
            cmd_bundles_add(_add_args("core", "beta"))

    def test_add_force_overwrites(self, populated_fleet, capsys) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        cmd_bundles_add(_add_args("core", "beta", force=True))
        assert BundlesConfig.load().get("core") == ["beta"]

    def test_add_at_token_rejected(self, populated_fleet, capsys) -> None:
        with pytest.raises(FleetError, match="cannot start with '@'"):
            cmd_bundles_add(_add_args("core", "@nested"))

    def test_add_warns_on_missing_member(
        self, populated_fleet, capsys
    ) -> None:
        rc = cmd_bundles_add(_add_args("core", "alpha,not-a-repo"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "WARN" in out and "not-a-repo" in out

    def test_list_empty(self, populated_fleet, capsys) -> None:
        rc = cmd_bundles_list(argparse.Namespace(fleet=None))
        assert rc == 0
        assert "No bundles configured" in capsys.readouterr().out

    def test_list_populated(self, populated_fleet, capsys) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        cmd_bundles_add(_add_args("frontend", "beta,group/gamma"))
        capsys.readouterr()
        cmd_bundles_list(argparse.Namespace(fleet=None))
        out = capsys.readouterr().out
        assert "core" in out and "1 repo(s)" in out
        assert "frontend" in out and "2 repo(s)" in out

    def test_show_marks_missing(self, populated_fleet, capsys) -> None:
        cmd_bundles_add(_add_args("core", "alpha,gone"))
        capsys.readouterr()
        cmd_bundles_show(_show_args("core"))
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "gone" in out and "[missing]" in out

    def test_show_unknown(self, populated_fleet) -> None:
        with pytest.raises(FleetError, match="No such bundle"):
            cmd_bundles_show(_show_args("nope"))

    def test_remove(self, populated_fleet, capsys) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        capsys.readouterr()
        cmd_bundles_remove(_show_args("core"))
        assert BundlesConfig.load().names() == []

    def test_edit_add_and_remove(self, populated_fleet, capsys) -> None:
        cmd_bundles_add(_add_args("core", "alpha,beta"))
        capsys.readouterr()
        cmd_bundles_edit(_edit_args("core", add="group/gamma", remove="beta"))
        assert BundlesConfig.load().get("core") == ["alpha", "group/gamma"]

    def test_edit_requires_one_flag(self, populated_fleet) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        with pytest.raises(FleetError, match="needs --add"):
            cmd_bundles_edit(_edit_args("core"))

    def test_edit_remove_non_member_warns(
        self, populated_fleet, capsys
    ) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        capsys.readouterr()
        rc = cmd_bundles_edit(_edit_args("core", remove="not-there"))
        assert rc == 0
        assert "not in bundle" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Per-fleet isolation
# ---------------------------------------------------------------------------

class TestFleetIsolation:
    def test_two_fleets_independent(
        self, populated_fleet, tmp_path: Path, cloned_repo_factory
    ) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))

        # Build a second fleet with its own repos.
        other_root = tmp_path / "other_repos"
        other_root.mkdir()
        cloned_repo_factory(other_root / "zeta")
        from fleet.fleets_config import FleetsConfig
        from fleet.scan import cmd_scan
        cfg = FleetsConfig.load()
        cfg.add("other", other_root, force=True)
        cfg.save()
        set_active_fleet("other", other_root)
        cmd_scan(argparse.Namespace())

        # Fleet "other" sees no bundles even though "demo" has one.
        assert BundlesConfig.load().names() == []
        cmd_bundles_add(_add_args("misc", "zeta"))
        assert BundlesConfig.load().names() == ["misc"]

        # Switch back: "demo" still has only "core".
        set_active_fleet("demo", populated_fleet.root)
        assert BundlesConfig.load().names() == ["core"]


# ---------------------------------------------------------------------------
# Consumption: task new / add-repo / remove-repo with @bundle
# ---------------------------------------------------------------------------

class TestBundleConsumption:
    def test_task_new_with_bundle_expands_in_order(
        self, populated_fleet, capsys
    ) -> None:
        cmd_bundles_add(_add_args("core", "alpha,group/gamma"))
        capsys.readouterr()
        rc = cmd_new(_new_task("t", "@core"))
        assert rc == 0
        ws = tasks_root() / "t"
        m = Manifest.load(ws)
        assert [r.name for r in m.repos] == ["alpha", "gamma"]
        assert (ws / "alpha" / ".git").exists()
        assert (ws / "gamma" / ".git").exists()

    def test_task_new_mixed_dedups(
        self, populated_fleet, capsys
    ) -> None:
        cmd_bundles_add(_add_args("core", "alpha"))
        capsys.readouterr()
        rc = cmd_new(_new_task("t", "@core,alpha"))
        assert rc == 0
        m = Manifest.load(tasks_root() / "t")
        assert [r.name for r in m.repos] == ["alpha"]

    def test_task_new_unknown_bundle_no_workspace(
        self, populated_fleet, capsys
    ) -> None:
        with pytest.raises(FleetError, match="Unknown bundle '@nope'"):
            cmd_new(_new_task("t", "@nope"))
        assert not (tasks_root() / "t").exists()

    def test_task_new_bundle_with_missing_member_no_workspace(
        self, populated_fleet, capsys
    ) -> None:
        cmd_bundles_add(_add_args("core", "alpha,gone"))
        capsys.readouterr()
        with pytest.raises(FleetError, match="Unknown repo 'gone'"):
            cmd_new(_new_task("t", "@core"))
        assert not (tasks_root() / "t").exists()

    def test_add_repo_with_bundle(
        self, populated_fleet, capsys
    ) -> None:
        cmd_new(_new_task("t", "alpha"))
        cmd_bundles_add(_add_args("more", "beta,group/gamma"))
        capsys.readouterr()
        rc = cmd_add_repo(_add_repo_args("t", "@more"))
        assert rc == 0
        m = Manifest.load(tasks_root() / "t")
        assert [r.name for r in m.repos] == ["alpha", "beta", "gamma"]

    def test_remove_repo_with_bundle(
        self, populated_fleet, capsys
    ) -> None:
        cmd_new(_new_task("t", "alpha,beta,group/gamma"))
        cmd_bundles_add(_add_args("drop", "alpha,beta"))
        capsys.readouterr()
        rc = cmd_remove_repo(_rm_repo_args("t", "@drop"))
        assert rc == 0
        m = Manifest.load(tasks_root() / "t")
        assert [r.name for r in m.repos] == ["gamma"]

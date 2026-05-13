"""Two fleets sharing physical clones — task isolation, branch namespacing."""

from __future__ import annotations

import argparse
from pathlib import Path

from fleet.fleets_config import FleetsConfig
from fleet.scan import cmd_scan
from fleet.state import set_active_fleet, tasks_root
from fleet.tasks.lifecycle import cmd_new
from fleet.tasks.manifest import Manifest
from helpers.git import _git


def _new_args(name: str, repos: str, **kw) -> argparse.Namespace:
    base = {"name": name, "repos": repos, "description": "",
            "no_pull": True, "dry_run": False, "fleet": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_two_fleets_isolate_tasks_on_disk(populated_fleet,
                                          tmp_path: Path, capsys) -> None:
    """Same-named tasks in two fleets land in different on-disk dirs."""
    # populated_fleet is "demo" already. Add a second fleet pointing at a
    # different repos root with its own clone of alpha.
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    from helpers.git import clone_into, init_bare_remote
    bare = init_bare_remote(tmp_path / "_bare-other.git")
    clone_into(bare, other_root / "alpha")

    cfg = FleetsConfig.load()
    cfg.add("work", other_root, force=True)
    cfg.save()

    # Create task `bug-1` in fleet `demo`.
    cmd_new(_new_args("bug-1", "alpha"))
    demo_workspace = tasks_root() / "bug-1"
    assert demo_workspace.is_dir()

    # Switch to `work` and create a same-named task.
    set_active_fleet("work", other_root)
    cmd_scan(argparse.Namespace())
    cmd_new(_new_args("bug-1", "alpha"))
    work_workspace = tasks_root() / "bug-1"

    assert demo_workspace != work_workspace
    assert demo_workspace.is_dir()
    assert work_workspace.is_dir()


def test_branch_names_namespaced_by_fleet(populated_fleet, tmp_path,
                                          capsys) -> None:
    """Task branches include the fleet name to prevent ref collisions."""
    cmd_new(_new_args("bug-1", "alpha"))
    branches = _git("branch", "--list",
                    cwd=populated_fleet.repos["alpha"]).stdout
    assert "task/demo/bug-1" in branches


def test_manifest_records_fleet_branch(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    manifest = Manifest.load(tasks_root() / "t1")
    assert manifest.branch == "task/demo/t1"


def test_resolve_override_via_args(populated_fleet, tmp_path) -> None:
    """FleetsConfig.resolve(override) returns the override when valid."""
    other = tmp_path / "other"
    other.mkdir()
    cfg = FleetsConfig.load()
    cfg.add("work", other, force=True)
    cfg.save()
    entry = cfg.resolve("work")
    assert entry.name == "work"
    assert entry.root == other.resolve()


def test_resolve_default_when_no_override(populated_fleet) -> None:
    cfg = FleetsConfig.load()
    entry = cfg.resolve(None)
    assert entry.name == "demo"

"""``fleet task list`` / ``info`` / ``sync`` / ``end`` end-to-end."""

from __future__ import annotations

import argparse
import json
import zipfile

import pytest

from fleet.errors import FleetError
from fleet.state import archive_root, tasks_root
from fleet.tasks.inspect import cmd_info, cmd_list, cmd_path, cmd_sync
from fleet.tasks.lifecycle import cmd_end, cmd_new


def _new_args(name: str, repos: str, **kw) -> argparse.Namespace:
    base = {"name": name, "repos": repos, "description": "test desc",
            "no_pull": True, "dry_run": False, "fleet": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _list_args(**kw) -> argparse.Namespace:
    base = {"quick": True, "as_json": False, "fleet": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _name_args(name: str, **kw) -> argparse.Namespace:
    base = {"name": name, "fleet": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _end_args(name: str, force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(name=name, force=force, fleet=None)


def test_list_empty(populated_fleet, capsys) -> None:
    rc = cmd_list(_list_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "No active tasks" in out or "doesn't exist" in out


def test_list_after_new(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    rc = cmd_list(_list_args(quick=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "t1" in out
    assert "task/demo/t1" in out


def test_list_json_mode(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    cmd_new(_new_args("t2", "beta"))
    capsys.readouterr()
    rc = cmd_list(_list_args(as_json=True))
    assert rc == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    by_name = {p["name"]: p for p in lines}
    assert set(by_name) == {"t1", "t2"}
    assert by_name["t1"]["branch"] == "task/demo/t1"
    assert by_name["t2"]["repos"][0]["name"] == "beta"


def test_list_marks_dirty(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    from helpers.git import make_dirty
    make_dirty(tasks_root() / "t1" / "alpha")
    cmd_list(_list_args(quick=False))
    out = capsys.readouterr().out
    assert "dirty:1" in out


def test_info_unknown_task(populated_fleet) -> None:
    with pytest.raises(FleetError, match="No such task"):
        cmd_info(_name_args("ghost"))


def test_info_renders(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha", description="line1\nline2"))
    capsys.readouterr()
    rc = cmd_info(_name_args("t1"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Task: t1" in out
    assert "task/demo/t1" in out
    # Fresh task branch hasn't been pushed yet -> "never pushed".
    assert "never pushed" in out


def test_info_marks_missing_worktree(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    import shutil
    shutil.rmtree(tasks_root() / "t1" / "alpha")
    cmd_info(_name_args("t1"))
    out = capsys.readouterr().out
    assert "worktree directory missing" in out


def test_path_prints_workspace(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    rc = cmd_path(_name_args("t1"))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tasks_root() / "t1")


def test_path_unknown_to_stderr(populated_fleet) -> None:
    with pytest.raises(FleetError):
        cmd_path(_name_args("ghost"))


def test_sync_happy_when_in_sync(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    rc = cmd_sync(_name_args("t1"))
    # Branch isn't on origin yet -> exit 0, "not on origin yet" message.
    assert rc == 0
    out = capsys.readouterr().out
    assert "not on origin yet" in out


def test_sync_skips_dirty(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    from helpers.git import make_dirty
    make_dirty(tasks_root() / "t1" / "alpha")
    rc = cmd_sync(_name_args("t1"))
    assert rc == 2
    out = capsys.readouterr().out
    assert "uncommitted changes" in out


def test_end_happy_path(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha,beta"))
    capsys.readouterr()
    rc = cmd_end(_end_args("t1"))
    assert rc == 0
    assert not (tasks_root() / "t1").exists()
    archives = list(archive_root().glob("t1-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as zf:
        names = set(zf.namelist())
    assert "task.json" in names
    assert "context.md" in names


def test_end_refuses_dirty(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    from helpers.git import make_dirty
    make_dirty(tasks_root() / "t1" / "alpha")
    capsys.readouterr()
    with pytest.raises(FleetError, match="uncommitted changes"):
        cmd_end(_end_args("t1"))


def test_end_force_removes_dirty(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    from helpers.git import make_dirty
    make_dirty(tasks_root() / "t1" / "alpha")
    capsys.readouterr()
    rc = cmd_end(_end_args("t1", force=True))
    assert rc == 0
    assert not (tasks_root() / "t1").exists()


def test_end_after_partial_manual_cleanup(populated_fleet, capsys) -> None:
    """If a worktree was already removed manually, `end` still archives + cleans up."""
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    import shutil
    # Pretend the user removed the worktree dir by hand (NOT via git worktree
    # remove). The `end` command notes "already gone" and proceeds.
    shutil.rmtree(tasks_root() / "t1" / "alpha")
    rc = cmd_end(_end_args("t1"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "already gone" in out


def test_end_unknown_task(populated_fleet) -> None:
    with pytest.raises(FleetError, match="No such task"):
        cmd_end(_end_args("ghost"))

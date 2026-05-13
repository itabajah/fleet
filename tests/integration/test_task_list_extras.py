"""``fleet task list`` non-quick status flags + edge cases."""

from __future__ import annotations

import argparse
import shutil

from fleet.state import tasks_root
from fleet.tasks.inspect import cmd_list
from fleet.tasks.lifecycle import cmd_new


def _new_args(name: str, repos: str) -> argparse.Namespace:
    return argparse.Namespace(name=name, repos=repos, description="",
                              no_pull=True, dry_run=False, fleet=None)


def _list_args(quick: bool = False, as_json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(quick=quick, as_json=as_json, fleet=None)


def test_list_clean_status(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    cmd_list(_list_args())
    out = capsys.readouterr().out
    # Branch never pushed -> not-pushed flag, not "clean".
    assert "not-pushed:1" in out


def test_list_marks_missing(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    shutil.rmtree(tasks_root() / "t1" / "alpha")
    cmd_list(_list_args())
    out = capsys.readouterr().out
    assert "missing:1" in out


def test_list_renders_unparseable_manifest(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    # Corrupt the manifest so try_load returns None.
    (tasks_root() / "t1" / "task.json").write_text("not json {",
                                                   encoding="utf-8")
    cmd_list(_list_args())
    out = capsys.readouterr().out
    assert "(no manifest)" in out


def test_list_json_with_unparseable(populated_fleet, capsys) -> None:
    cmd_new(_new_args("t1", "alpha"))
    capsys.readouterr()
    (tasks_root() / "t1" / "task.json").write_text("not json {",
                                                   encoding="utf-8")
    cmd_list(_list_args(as_json=True))
    out = capsys.readouterr().out
    assert '"error":"unparseable_manifest"' in out

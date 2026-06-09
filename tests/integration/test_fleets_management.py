"""``fleet fleets ...`` management commands."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from fleet.errors import FleetError
from fleet.fleets_commands import (
    cmd_fleets_add,
    cmd_fleets_default,
    cmd_fleets_list,
    cmd_fleets_remove,
    cmd_fleets_rename,
)
from fleet.fleets_config import FleetsConfig


def _add_args(name: str, root: Path | None = None,
              force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(name=name,
                              root=str(root) if root else None,
                              force=force)


def test_list_empty(capsys) -> None:
    rc = cmd_fleets_list(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "No fleets configured" in out


def test_add_first_becomes_default(tmp_path, capsys) -> None:
    rc = cmd_fleets_add(_add_args("alpha", tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Registered fleet 'alpha'" in out
    assert "(set as default)" in out
    cfg = FleetsConfig.load()
    assert cfg.default == "alpha"


def test_add_second_does_not_override_default(tmp_path, capsys) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cmd_fleets_add(_add_args("alpha", tmp_path))
    capsys.readouterr()
    cmd_fleets_add(_add_args("beta", other))
    out = capsys.readouterr().out
    assert "Registered fleet 'beta'" in out
    assert "(set as default)" not in out
    cfg = FleetsConfig.load()
    assert cfg.default == "alpha"


def test_add_rejects_missing_root(tmp_path) -> None:
    with pytest.raises(FleetError, match="does not exist"):
        cmd_fleets_add(_add_args("alpha", tmp_path / "nope"))


def test_add_force_overwrites(tmp_path, capsys) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cmd_fleets_add(_add_args("alpha", tmp_path))
    capsys.readouterr()
    cmd_fleets_add(_add_args("alpha", other, force=True))
    capsys.readouterr()
    cfg = FleetsConfig.load()
    assert cfg.fleets["alpha"].root == other.resolve()


def test_default_unknown_raises(tmp_path) -> None:
    with pytest.raises(FleetError):
        cmd_fleets_default(argparse.Namespace(name="ghost"))


def test_default_switches(tmp_path, capsys) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cmd_fleets_add(_add_args("alpha", tmp_path))
    cmd_fleets_add(_add_args("beta", other))
    capsys.readouterr()
    rc = cmd_fleets_default(argparse.Namespace(name="beta"))
    assert rc == 0
    cfg = FleetsConfig.load()
    assert cfg.default == "beta"


def test_remove_default_clears_and_warns(tmp_path, capsys) -> None:
    # Removing the active default clears it (no silent alphabetical promote)
    # and the handler prints a prominent warning naming the remaining fleets.
    other = tmp_path / "other"
    other.mkdir()
    cmd_fleets_add(_add_args("zeta", tmp_path))
    cmd_fleets_add(_add_args("alpha", other))
    capsys.readouterr()
    cmd_fleets_remove(argparse.Namespace(name="zeta"))
    out = capsys.readouterr().out
    assert "was the default" in out
    assert "alpha" in out
    cfg = FleetsConfig.load()
    assert cfg.default is None
    assert set(cfg.fleets) == {"alpha"}


def test_fleets_rename_cli(tmp_path, capsys) -> None:
    cmd_fleets_add(_add_args("old", tmp_path))
    capsys.readouterr()
    rc = cmd_fleets_rename(argparse.Namespace(old="old", new="fresh"))
    assert rc == 0
    cfg = FleetsConfig.load()
    assert "old" not in cfg.fleets
    assert cfg.fleets["fresh"].root == tmp_path.resolve()
    assert cfg.default == "fresh"


def test_fleets_rename_note_unscoped(tmp_path, capsys) -> None:
    """Under an unscoped convention, branch names don't carry the fleet, so
    the rename note says branches are unaffected instead of showing a remap."""
    from fleet.state import BranchConfig
    cfg = FleetsConfig()
    cfg.add("old", tmp_path)
    cfg.branch = BranchConfig(prefix="wip", scoped=False)
    cfg.save()
    capsys.readouterr()
    rc = cmd_fleets_rename(argparse.Namespace(old="old", new="fresh"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "aren't fleet-scoped" in out
    assert "wip" in out


def test_remove_last_clears_default(tmp_path, capsys) -> None:
    cmd_fleets_add(_add_args("alpha", tmp_path))
    capsys.readouterr()
    cmd_fleets_remove(argparse.Namespace(name="alpha"))
    out = capsys.readouterr().out
    assert "No fleets remain" in out
    cfg = FleetsConfig.load()
    assert cfg.default is None


def test_remove_unknown(tmp_path) -> None:
    with pytest.raises(FleetError, match="No such fleet"):
        cmd_fleets_remove(argparse.Namespace(name="ghost"))


def test_list_marks_default(tmp_path, capsys) -> None:
    cmd_fleets_add(_add_args("alpha", tmp_path))
    capsys.readouterr()
    cmd_fleets_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "(default)" in out

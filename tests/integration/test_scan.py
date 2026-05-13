"""End-to-end ``fleet scan`` against a real on-disk fleet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from fleet.errors import FleetError
from fleet.scan import cmd_scan
from fleet.state import set_active_fleet
from helpers.git import write_marker_repo


def _registry(root: Path) -> dict:
    return json.loads((root / "fleet.json").read_text(encoding="utf-8"))


def test_scan_empty_root_writes_minimal(tmp_path: Path) -> None:
    set_active_fleet("demo", tmp_path)
    rc = cmd_scan(argparse.Namespace())
    assert rc == 0
    reg = _registry(tmp_path)
    assert reg == {"root": "."}


def test_scan_finds_top_level_repos(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    write_marker_repo(tmp_path / "beta")
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert reg["."]["repos"] == ["alpha", "beta"]


def test_scan_collapses_single_child_chain(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "group" / "sub" / "alpha")
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    # Single-child chain becomes "group/sub" key.
    assert "group/sub" in reg
    assert reg["group/sub"]["repos"] == ["alpha"]


def test_scan_preserves_disabled_node(tmp_path: Path) -> None:
    """Hand-edited `sync: false` survives a re-scan even when the dir is pruned."""
    write_marker_repo(tmp_path / "..me" / "self-clone")
    write_marker_repo(tmp_path / "alpha")
    (tmp_path / "fleet.json").write_text(
        json.dumps({"root": ".", "..me": {"sync": False}}) + "\n",
        encoding="utf-8",
    )
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert reg["..me"] == {"sync": False}
    assert reg["."]["repos"] == ["alpha"]


def test_scan_preserves_exclude_under_disabled(tmp_path: Path) -> None:
    """User-set `exclude` on a disabled folder must survive a re-scan
    (whether or not the folder is on disk)."""
    write_marker_repo(tmp_path / "alpha")
    (tmp_path / "fleet.json").write_text(
        json.dumps({
            "root": ".",
            "vendored": {"sync": False, "exclude": ["secret-repo"]},
        }) + "\n",
        encoding="utf-8",
    )
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert reg["vendored"]["sync"] is False
    assert reg["vendored"]["exclude"] == ["secret-repo"]


def test_scan_preserves_subfolders_under_disabled(tmp_path: Path) -> None:
    """User-set nested `subfolders` under a disabled folder must survive."""
    # `vendored/` exists on disk so the walker visits it and creates a stub;
    # the merge path must still preserve the user's nested subfolders entry.
    (tmp_path / "vendored").mkdir()
    write_marker_repo(tmp_path / "alpha")
    nested = {"deep": {"sync": False}}
    (tmp_path / "fleet.json").write_text(
        json.dumps({
            "root": ".",
            "vendored": {"sync": False, "subfolders": nested},
        }) + "\n",
        encoding="utf-8",
    )
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert reg["vendored"]["sync"] is False
    assert reg["vendored"].get("subfolders", {}).get("deep") == {"sync": False}


def test_scan_preserves_manual_exclude(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    write_marker_repo(tmp_path / "beta")
    (tmp_path / "fleet.json").write_text(
        json.dumps({"root": ".", ".": {"sync": True, "exclude": ["beta"]}}) + "\n",
        encoding="utf-8",
    )
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert "beta" in reg["."]["exclude"]
    assert "beta" not in reg["."]["repos"]
    assert reg["."]["repos"] == ["alpha"]


def test_scan_sorts_alphabetically_case_insensitive(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "Zebra")
    write_marker_repo(tmp_path / "alpha")
    write_marker_repo(tmp_path / "Beta")
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert reg["."]["repos"] == ["alpha", "Beta", "Zebra"]


def test_scan_idempotent_no_new_finds(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    first = _registry(tmp_path)
    cmd_scan(argparse.Namespace())
    second = _registry(tmp_path)
    assert first == second


def test_scan_writes_atomically_no_tmp_left(tmp_path: Path) -> None:
    write_marker_repo(tmp_path / "alpha")
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    assert list(tmp_path.glob("fleet.json.tmp")) == []


def test_scan_refuses_invalid_root_field(tmp_path: Path) -> None:
    """A misconfigured `root` field is a hard error \u2014 no silent rewrite."""
    (tmp_path / "fleet.json").write_text(
        json.dumps({"root": "does-not-exist"}) + "\n", encoding="utf-8",
    )
    set_active_fleet("demo", tmp_path)
    with pytest.raises(FleetError, match="does not exist"):
        cmd_scan(argparse.Namespace())
    # fleet.json untouched.
    reg = _registry(tmp_path)
    assert reg["root"] == "does-not-exist"


def test_scan_with_root_subdir(tmp_path: Path) -> None:
    """When `root` points at a subdir, only repos under that subdir are found."""
    sub = tmp_path / "sub"
    sub.mkdir()
    write_marker_repo(sub / "alpha")
    write_marker_repo(tmp_path / "outside")  # outside the configured root
    (tmp_path / "fleet.json").write_text(
        json.dumps({"root": "sub"}) + "\n", encoding="utf-8",
    )
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    reg = _registry(tmp_path)
    assert reg["root"] == "sub"
    assert reg["."]["repos"] == ["alpha"]


def test_scan_counts_new_vs_existing(tmp_path: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    write_marker_repo(tmp_path / "alpha")
    set_active_fleet("demo", tmp_path)
    cmd_scan(argparse.Namespace())
    capsys.readouterr()  # discard
    write_marker_repo(tmp_path / "beta")
    cmd_scan(argparse.Namespace())
    out = capsys.readouterr().out
    assert "Total repositories found: 2" in out
    assert "New repositories found:   1" in out

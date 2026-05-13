"""``fleet repos`` output rendering against a populated fleet."""

from __future__ import annotations

import argparse

from fleet.repos_command import cmd_repos


def test_repos_lists_groups(populated_fleet, capsys) -> None:
    rc = cmd_repos(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "(top-level):" in out
    assert "alpha" in out
    assert "beta" in out
    assert "group:" in out
    assert "gamma" in out
    assert "Total: 3 repos" in out


def test_repos_marks_disabled(populated_fleet, capsys) -> None:
    """Re-write the registry so one repo is excluded; it should render with marker."""
    import json
    (populated_fleet.root / "fleet.json").write_text(
        json.dumps({
            "root": ".",
            ".": {"sync": True, "repos": ["alpha", "beta"], "exclude": ["beta"]},
            "group": {"sync": True, "repos": ["gamma"]},
        }) + "\n",
        encoding="utf-8",
    )
    cmd_repos(argparse.Namespace())
    out = capsys.readouterr().out
    assert "beta" in out
    assert "(disabled)" in out


def test_repos_marks_not_in_registry(populated_fleet, capsys, tmp_path) -> None:
    from helpers.git import write_marker_repo
    # Drop a fake repo on disk; it isn't in the registry.
    write_marker_repo(populated_fleet.root / "extra")
    cmd_repos(argparse.Namespace())
    out = capsys.readouterr().out
    assert "extra" in out
    assert "(not in registry)" in out


def test_repos_handles_empty(tmp_path, capsys) -> None:
    from fleet.state import set_active_fleet
    set_active_fleet("demo", tmp_path)
    rc = cmd_repos(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "No git repos found" in out

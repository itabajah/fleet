"""CLI parser sanity: subcommand registration, --version, --fleet plumbing."""

from __future__ import annotations

import argparse

import pytest

from fleet import __version__
from fleet.cli import build_parser


def test_top_level_subcommands() -> None:
    parser = build_parser()
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    assert set(sub_action.choices) == {
        "sync", "scan", "repos", "task", "fleets", "open",
    }


def test_task_subcommands() -> None:
    parser = build_parser()
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    task_parser = sub_action.choices["task"]
    task_sub = next(a for a in task_parser._actions
                    if isinstance(a, argparse._SubParsersAction))
    assert set(task_sub.choices) == {
        "new", "list", "info", "sync", "end", "path", "open",
    }


def test_fleets_subcommands() -> None:
    parser = build_parser()
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    fleets_parser = sub_action.choices["fleets"]
    fleets_sub = next(a for a in fleets_parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    assert set(fleets_sub.choices) == {"list", "add", "default", "remove"}


def test_version_action(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


@pytest.mark.parametrize("argv", [
    ["sync", "-F", "work"],
    ["scan", "-F", "work"],
    ["repos", "-F", "work"],
    ["task", "list", "-F", "work"],
    ["task", "info", "x", "-F", "work"],
    ["task", "sync", "x", "-F", "work"],
    ["task", "end", "x", "-F", "work"],
    ["task", "path", "x", "-F", "work"],
    ["task", "new", "x", "--repos", "a,b", "-F", "work"],
])
def test_fleet_arg_accepted_on_every_aware_subcommand(argv) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    assert args.fleet == "work"


def test_fleet_arg_rejected_on_fleets_command() -> None:
    """`fleets` commands manage the registry; -F doesn't apply."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fleets", "list", "-F", "work"])


def test_unknown_command_exits_non_zero() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["totally-not-a-command"])
    assert excinfo.value.code != 0


def test_help_renders_for_every_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    for name, sub in sub_action.choices.items():
        sub.format_help()  # must not raise
        # Render full help text without crashing on missing required args.
        assert isinstance(name, str)

"""Top-level argparse for the ``fleet`` CLI.

Subcommand registration is delegated to each command module via a
``register(subparsers, fleet_arg)`` hook so this file stays small and
imports stay shallow. Resolution order:

  1. Force UTF-8 on stdout/stderr (Windows consoles default to cp1252
     when output is piped/captured, which blows up on the ⚠/✓/✗ glyphs).
  2. Build the parser by calling each module's ``register()``.
  3. Resolve the active fleet (``-F``/``--fleet`` override beats the
     configured default) and pin it via :func:`fleet.state.set_active_fleet`.
  4. Dispatch to the chosen handler.

``fleet open`` is handled in ``Fleet.psm1`` because Python can't change
the parent shell's CWD. If invoked here, a helpful error tells the user
how to wire the PowerShell module.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from fleet import __version__, fleets_commands, repos_command, scan, sync, tasks
from fleet.console import dim, red
from fleet.errors import FleetError
from fleet.fleets_config import FleetsConfig
from fleet.state import active_fleet_name, set_active_fleet


def _open_unsupported(_args: argparse.Namespace) -> int:
    """Stub for ``fleet open`` / ``fleet task open``: Python can't cd the parent."""
    print(red(
        "ERROR: `fleet open` mutates the parent shell's working directory, "
        "so it can't run from the Python CLI."
    ), file=sys.stderr)
    if sys.platform == "win32":
        # Best-effort hint when running from a source checkout. When installed
        # via pip into site-packages, the path is misleading, so we omit it.
        try:
            psm1 = Path(__file__).resolve().parents[2] / "Fleet.psm1"
            if psm1.is_file():
                print(
                    "Wire the PowerShell module into your $PROFILE and "
                    f"restart pwsh:\n  Import-Module \"{psm1}\"",
                    file=sys.stderr,
                )
            else:
                print(
                    "Wire the Fleet.psm1 module from your fleet checkout into "
                    "your $PROFILE.",
                    file=sys.stderr,
                )
        except OSError:
            print(
                "Wire the Fleet.psm1 module from your fleet checkout into "
                "your $PROFILE.",
                file=sys.stderr,
            )
    else:
        print(
            "From bash/zsh, run:\n"
            "  cd \"$(fleet task path <name>)\" && code .",
            file=sys.stderr,
        )
    return 2


def build_parser() -> argparse.ArgumentParser:
    # Common parent: the `--fleet`/`-F` override is attached to every
    # fleet-aware subparser. Putting it here (rather than on the top-level
    # parser) lets users type it AFTER the subcommand: `fleet sync -F work`.
    fleet_arg = argparse.ArgumentParser(add_help=False)
    fleet_arg.add_argument(
        "--fleet", "-F", default=None, metavar="NAME",
        help="use the named fleet (default: configured default)",
    )

    p = argparse.ArgumentParser(
        prog="fleet",
        description="Multi-repo workspace tool: parallel git sync + task scaffolder.",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    subparsers = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # Each module owns its own subparser registration.
    sync.register(subparsers, fleet_arg)
    scan.register(subparsers, fleet_arg)
    repos_command.register(subparsers, fleet_arg)
    tasks.register(subparsers, fleet_arg)
    fleets_commands.register(subparsers, fleet_arg)

    # `fleet open` and `fleet task open` are PS-only. Stub them here so the
    # error message is a single concise line rather than argparse's
    # "invalid choice" complaint.
    s_open = subparsers.add_parser(
        "open",
        help="(PowerShell-only) cd into a task workspace and launch VS Code",
    )
    s_open.add_argument("name", nargs="?", help="task name")
    s_open.set_defaults(func=_open_unsupported)

    # Patch the `task` group in-place to add an `open` stub at the same
    # level as `task new`/`list`/etc. This is purely so users get a helpful
    # error rather than "invalid choice: 'open'".
    task_action = next(
        a for a in subparsers.choices["task"]._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    s_topen = task_action.add_parser(
        "open",
        help="(PowerShell-only) cd into a task workspace and launch VS Code",
    )
    s_topen.add_argument("name", nargs="?", help="task name")
    s_topen.set_defaults(func=_open_unsupported)

    return p


def _needs_active_fleet(args: argparse.Namespace) -> bool:
    """True when the command requires the active fleet to be resolved.

    ``fleet fleets ...`` and ``fleet open`` (and ``fleet task open``) are
    excluded: they manage config or shell state and don't read the registry.
    """
    if args.cmd == "fleets":
        return False
    if args.cmd == "open":
        return False
    return not (args.cmd == "task" and getattr(args, "task_cmd", None) == "open")


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252 when output is piped/captured,
    # which blows up on the ⚠/✓/✗ glyphs we use for status. Force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError):
            reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    if _needs_active_fleet(args):
        try:
            cfg = FleetsConfig.load()
            entry = cfg.resolve(getattr(args, "fleet", None))
            set_active_fleet(entry.name, entry.root)
        except FleetError as e:
            print(f"{red('ERROR:')} {e}", file=sys.stderr)
            return e.exit_code

        # Show a quiet banner only when overriding via -F so default usage
        # stays uncluttered. Goes to stderr so callers that capture stdout
        # (Fleet.psm1's `task path` reader, shell `$(…)` substitution) get
        # clean output.
        if getattr(args, "fleet", None):
            print(dim(f"[fleet: {active_fleet_name()}  →  {entry.root}]"),
                  file=sys.stderr)

    try:
        return args.func(args)
    except FleetError as e:
        print(f"{red('ERROR:')} {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print(red("\nInterrupted."), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

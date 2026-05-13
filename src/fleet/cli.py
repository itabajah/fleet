"""argparse top-level for the `fleet` CLI.

Subcommands:
  fleet sync   [--dry-run] [--workers N] [--no-auth-check] [-F NAME]
  fleet scan   [-F NAME]
  fleet repos  [-F NAME]
  fleet task new   <name> --repos a,b[,group/c] [--description ...] [--no-pull] [--dry-run] [-F NAME]
  fleet task list  [--quick] [-F NAME]
  fleet task info  <name>           [-F NAME]
  fleet task sync  <name>           [-F NAME]
  fleet task end   <name> [--force] [-F NAME]
  fleet task path  <name>           [-F NAME]   (used by `fleet open` in PS)
  fleet fleets list
  fleet fleets add     <name> [--root PATH] [--force]
  fleet fleets default <name>
  fleet fleets remove  <name>

`fleet open <name>` is handled in Fleet.psm1 because Python can't change the
parent shell's CWD. If invoked here, a helpful error tells the user how to
wire the PowerShell module.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fleet import registry, sync, tasks
from fleet.config import (
    FleetError,
    active_fleet_name,
    cyan,
    dim,
    gray,
    green,
    red,
    set_active_fleet,
    yellow,
)
from fleet.fleets_config import FleetsConfig, config_path


def _open_unsupported(_args: argparse.Namespace) -> int:
    print(red(
        "ERROR: `fleet open` mutates the parent shell's working directory, "
        "so it can't run from the Python CLI."
    ), file=sys.stderr)
    if sys.platform == "win32":
        psm1 = Path(__file__).resolve().parents[2] / "Fleet.psm1"
        print(
            "Wire the PowerShell module into your $PROFILE and restart pwsh:\n"
            f"  Import-Module \"{psm1}\"",
            file=sys.stderr,
        )
    else:
        print(
            "From bash/zsh, run:\n"
            "  cd \"$(fleet task path <name>)\" && code .",
            file=sys.stderr,
        )
    return 2


# ---------------------------------------------------------------------------
# `fleet fleets ...` handlers
# ---------------------------------------------------------------------------

def cmd_fleets_list(_args: argparse.Namespace) -> int:
    cfg = FleetsConfig.load()
    if not cfg.fleets:
        print(yellow("No fleets configured."))
        print(gray("  Add one with: fleet fleets add <name> [--root PATH]"))
        return 0
    print(cyan("Configured fleets:"))
    name_w = max(len(n) for n in cfg.fleets)
    for name in sorted(cfg.fleets):
        entry = cfg.fleets[name]
        marker = green("  (default)") if name == cfg.default else ""
        scanned = (entry.root / "fleet.json").is_file()
        status = "" if scanned else gray("  [not scanned]")
        print(f"  {name.ljust(name_w)}  {entry.root}{status}{marker}")
    print()
    print(gray(f"Config file: {config_path()}"))
    return 0


def cmd_fleets_add(args: argparse.Namespace) -> int:
    cfg = FleetsConfig.load()
    root = Path(args.root) if args.root else Path.cwd()
    cfg.add(args.name, root, force=args.force)
    cfg.save()
    print(green(f"✓ Registered fleet '{args.name}' at {cfg.fleets[args.name].root}"))
    if cfg.default == args.name:
        print(gray("  (set as default)"))
    if not (cfg.fleets[args.name].root / "fleet.json").is_file():
        print(gray(f"  Tip: run `fleet scan -F {args.name}` to populate fleet.json"))
    return 0


def cmd_fleets_default(args: argparse.Namespace) -> int:
    cfg = FleetsConfig.load()
    cfg.set_default(args.name)
    cfg.save()
    print(green(f"✓ Default fleet is now '{args.name}'"))
    return 0


def cmd_fleets_remove(args: argparse.Namespace) -> int:
    cfg = FleetsConfig.load()
    cfg.remove(args.name)
    cfg.save()
    print(green(f"✓ Unregistered fleet '{args.name}'"))
    if cfg.default:
        print(gray(f"  Default is now '{cfg.default}'."))
    elif cfg.fleets:
        print(gray("  No default set. Run `fleet fleets default <name>`."))
    else:
        print(gray("  No fleets remain. Add one with `fleet fleets add`."))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Common parent: the `--fleet`/`-F` override is attached to every
    # fleet-aware subparser via `parents=[fleet_arg]`. Putting it here
    # (rather than on the top-level parser) lets users type it AFTER the
    # subcommand: `fleet sync -F work` works naturally.
    fleet_arg = argparse.ArgumentParser(add_help=False)
    fleet_arg.add_argument(
        "--fleet", "-F", default=None, metavar="NAME",
        help="use the named fleet (default: configured default)",
    )

    p = argparse.ArgumentParser(
        prog="fleet",
        description="Multi-repo workspace tool: parallel git sync + task scaffolder.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # --- fleet sync ---
    s_sync = sub.add_parser(
        "sync", parents=[fleet_arg],
        help="parallel git pull --ff-only across every enabled repo",
    )
    s_sync.add_argument("--dry-run", action="store_true",
                        help="preview changes without modifying repos")
    s_sync.add_argument("--workers", type=int, default=sync.DEFAULT_WORKERS,
                        help=f"number of parallel workers "
                             f"(default {sync.DEFAULT_WORKERS}, 0 = auto, capped at 32)")
    s_sync.add_argument("--no-auth-check", action="store_true",
                        help="skip the per-host credential probe")
    s_sync.set_defaults(func=sync.cmd_sync)

    # --- fleet scan ---
    s_scan = sub.add_parser(
        "scan", parents=[fleet_arg],
        help="rescan disk and rewrite fleet.json (preserves manual sync/exclude)",
    )
    s_scan.set_defaults(func=registry.cmd_scan)

    # --- fleet repos ---
    s_repos = sub.add_parser(
        "repos", parents=[fleet_arg],
        help="list every git repo under the active fleet's root",
    )
    s_repos.set_defaults(func=tasks.cmd_repos)

    # --- fleet open (PS-only; stub here just to give a clear error) ---
    s_open = sub.add_parser(
        "open",
        help="(PowerShell-only) cd into a task workspace and launch VS Code",
    )
    s_open.add_argument("name", nargs="?", help="task name")
    s_open.set_defaults(func=_open_unsupported)

    # --- fleet task ---
    s_task = sub.add_parser("task", help="manage task workspaces")
    task_sub = s_task.add_subparsers(dest="task_cmd", required=True,
                                     metavar="<command>")

    # task new
    s_new = task_sub.add_parser(
        "new", parents=[fleet_arg],
        help="create a new task workspace with worktrees per repo",
    )
    s_new.add_argument("name", help="task name (folder + branch suffix)")
    s_new.add_argument("--repos", required=True,
                       help="comma-separated repo names "
                            "(use group/path/name to disambiguate)")
    s_new.add_argument("--description", "-d", default="",
                       help="seed text for context.md")
    s_new.add_argument("--no-pull", action="store_true",
                       help="skip fetch + pull on each canonical repo "
                            "(uses local refs only; offline-safe)")
    s_new.add_argument("--dry-run", action="store_true",
                       help="validate inputs and print the plan without "
                            "creating anything")
    s_new.set_defaults(func=tasks.cmd_new)

    # task list
    s_list = task_sub.add_parser(
        "list", parents=[fleet_arg],
        help="list active task workspaces in the active fleet",
    )
    s_list.add_argument("--quick", action="store_true",
                        help="skip dirty/unpushed status checks")
    s_list.set_defaults(func=tasks.cmd_list)

    # task info
    s_info = task_sub.add_parser(
        "info", parents=[fleet_arg],
        help="show detailed status of one task",
    )
    s_info.add_argument("name", help="task name")
    s_info.set_defaults(func=tasks.cmd_info)

    # task sync
    s_tsync = task_sub.add_parser(
        "sync", parents=[fleet_arg],
        help="fetch + ff-pull each worktree on its task branch",
    )
    s_tsync.add_argument("name", help="task name")
    s_tsync.set_defaults(func=tasks.cmd_sync)

    # task end
    s_end = task_sub.add_parser(
        "end", parents=[fleet_arg],
        help="archive and tear down a task workspace",
    )
    s_end.add_argument("name", help="task name")
    s_end.add_argument("--force", action="store_true",
                       help="proceed even if a worktree is dirty")
    s_end.set_defaults(func=tasks.cmd_end)

    # task path  (prints absolute workspace path; used by `fleet open`)
    s_path = task_sub.add_parser(
        "path", parents=[fleet_arg],
        help="print the absolute path to a task workspace",
    )
    s_path.add_argument("name", help="task name")
    s_path.set_defaults(func=tasks.cmd_path)

    # task open (PS-only; stub for parity)
    s_topen = task_sub.add_parser(
        "open",
        help="(PowerShell-only) cd into a task workspace and launch VS Code",
    )
    s_topen.add_argument("name", nargs="?", help="task name")
    s_topen.set_defaults(func=_open_unsupported)

    # --- fleet fleets ---
    s_fleets = sub.add_parser("fleets", help="manage configured fleets")
    fleets_sub = s_fleets.add_subparsers(dest="fleets_cmd", required=True,
                                         metavar="<command>")

    f_list = fleets_sub.add_parser("list", help="list all configured fleets")
    f_list.set_defaults(func=cmd_fleets_list)

    f_add = fleets_sub.add_parser("add", help="register a new fleet")
    f_add.add_argument("name", help="fleet name")
    f_add.add_argument("--root", default=None,
                       help="repos root path (default: current directory)")
    f_add.add_argument("--force", action="store_true",
                       help="overwrite if the name is already registered")
    f_add.set_defaults(func=cmd_fleets_add)

    f_def = fleets_sub.add_parser("default",
                                  help="set the default fleet")
    f_def.add_argument("name", help="fleet name")
    f_def.set_defaults(func=cmd_fleets_default)

    f_rm = fleets_sub.add_parser("remove",
                                 help="unregister a fleet (no file deletion)")
    f_rm.add_argument("name", help="fleet name")
    f_rm.set_defaults(func=cmd_fleets_remove)

    return p


def _needs_active_fleet(args: argparse.Namespace) -> bool:
    """True when the command requires the active fleet to be resolved.

    `fleet fleets ...` and `fleet open` (and `fleet task open`) are excluded:
    they manage config or shell state and don't read the registry directly.
    """
    if args.cmd == "fleets":
        return False
    if args.cmd == "open":
        return False
    if args.cmd == "task" and getattr(args, "task_cmd", None) == "open":
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252 when output is piped/captured, which
    # blows up on the ⚠/✓/✗ glyphs we use for status. Force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

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

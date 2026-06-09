"""Handlers for the ``fleet fleets ...`` subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

from fleet.console import cyan, gray, green, yellow
from fleet.fleets_config import FleetsConfig, config_path
from fleet.jsonstore import config_lock


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
    with config_lock(config_path()):
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
    with config_lock(config_path()):
        cfg = FleetsConfig.load()
        cfg.set_default(args.name)
        cfg.save()
    print(green(f"✓ Default fleet is now '{args.name}'"))
    return 0


def cmd_fleets_remove(args: argparse.Namespace) -> int:
    with config_lock(config_path()):
        cfg = FleetsConfig.load()
        was_default = cfg.remove(args.name)
        cfg.save()
    print(green(f"✓ Unregistered fleet '{args.name}'"))
    if was_default:
        if cfg.fleets:
            print(yellow(
                f"  '{args.name}' was the default; no default is set now. "
                f"Run `fleet fleets default <name>` to choose one "
                f"({', '.join(sorted(cfg.fleets))})."
            ))
        else:
            print(gray("  No fleets remain. Add one with `fleet fleets add`."))
    return 0


def cmd_fleets_rename(args: argparse.Namespace) -> int:
    with config_lock(config_path()):
        cfg = FleetsConfig.load()
        cfg.rename(args.old, args.new)
        cfg.save()
    print(green(f"✓ Renamed fleet '{args.old}' → '{args.new}'"))
    if cfg.default == args.new:
        print(gray("  (still the default)"))
    bc = cfg.branch
    if bc.scoped:
        print(yellow(
            f"  Note: existing task branches keep the old name "
            f"(`{bc.prefix}/{args.old}/...`). "
            f"New tasks use `{bc.prefix}/{args.new}/...`."
        ))
    else:
        print(gray(
            f"  Task branches aren't fleet-scoped (prefix `{bc.prefix}`), "
            f"so branch names are unaffected."
        ))
    return 0


def register(subparsers: argparse._SubParsersAction,
             _fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet fleets`` command group.

    The ``_fleet_arg`` parent isn't used here — these commands operate on the
    fleets config itself, not within an active fleet.
    """
    p = subparsers.add_parser("fleets", help="manage configured fleets")
    sub = p.add_subparsers(dest="fleets_cmd", required=True, metavar="<command>")

    f_list = sub.add_parser("list", help="list all configured fleets")
    f_list.set_defaults(func=cmd_fleets_list)

    f_add = sub.add_parser("add", help="register a new fleet")
    f_add.add_argument("name", help="fleet name")
    f_add.add_argument("--root", default=None,
                       help="repos root path (default: current directory)")
    f_add.add_argument("--force", action="store_true",
                       help="overwrite if the name is already registered")
    f_add.set_defaults(func=cmd_fleets_add)

    f_def = sub.add_parser("default", help="set the default fleet")
    f_def.add_argument("name", help="fleet name")
    f_def.set_defaults(func=cmd_fleets_default)

    f_rm = sub.add_parser("remove", help="unregister a fleet (no file deletion)")
    f_rm.add_argument("name", help="fleet name")
    f_rm.set_defaults(func=cmd_fleets_remove)

    f_ren = sub.add_parser("rename", help="rename a fleet (config only)")
    f_ren.add_argument("old", help="current fleet name")
    f_ren.add_argument("new", help="new fleet name")
    f_ren.set_defaults(func=cmd_fleets_rename)

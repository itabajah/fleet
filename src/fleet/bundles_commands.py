"""Handlers for the ``fleet bundles ...`` subcommands."""

from __future__ import annotations

import argparse

from fleet.bundles_config import (
    BundlesConfig,
    expand_bundle_tokens,  # noqa: F401  (re-exported for convenience)
)
from fleet.console import cyan, gray, green, yellow
from fleet.discovery import discover_repos
from fleet.errors import FleetError
from fleet.fleets_config import _config_lock
from fleet.state import active_fleet_name, bundles_path
from fleet.tasks.validation import resolve_repo


def _split_csv(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def _warn_unresolved(tokens: list[str]) -> None:
    """Best-effort: warn about tokens that don't resolve right now."""
    try:
        all_repos = discover_repos()
    except FleetError:
        return
    if not all_repos:
        return
    missing: list[str] = []
    for tok in tokens:
        try:
            resolve_repo(tok, all_repos)
        except FleetError:
            missing.append(tok)
    if missing:
        print(yellow(
            f"WARN: {len(missing)} member(s) don't currently resolve: "
            f"{', '.join(missing)}"
        ))
        print(gray(
            "  (saved anyway; resolution happens when the bundle is "
            "consumed by `task new` / `add-repo` / `remove-repo`.)"
        ))


# ---------------------------------------------------------------------------
# bundles add
# ---------------------------------------------------------------------------

def cmd_bundles_add(args: argparse.Namespace) -> int:
    tokens = _split_csv(args.repos)
    if not tokens:
        raise FleetError("--repos requires at least one repo token.")

    with _config_lock(bundles_path()):
        cfg = BundlesConfig.load()
        existed = args.name in cfg.bundles
        stored = cfg.add(args.name, tokens, force=args.force)
        cfg.save()

    _warn_unresolved(stored)
    verb = "updated" if existed else "created"
    print(green(f"✓ Bundle '{args.name}' {verb} ({len(stored)} repo(s))."))
    return 0


# ---------------------------------------------------------------------------
# bundles list
# ---------------------------------------------------------------------------

def cmd_bundles_list(_args: argparse.Namespace) -> int:
    cfg = BundlesConfig.load()
    if not cfg.bundles:
        print(yellow("No bundles configured."))
        print(gray("  Add one with: fleet bundles add <name> --repos a,b,c"))
        return 0
    fleet = active_fleet_name() or "?"
    print(cyan(f"Configured bundles (fleet '{fleet}'):"))
    name_w = max(len(n) for n in cfg.bundles)
    for name in cfg.names():
        n = len(cfg.bundles[name])
        print(f"  {name.ljust(name_w)}  {n} repo(s)")
    print()
    print(gray(f"Bundles file: {bundles_path()}"))
    return 0


# ---------------------------------------------------------------------------
# bundles show
# ---------------------------------------------------------------------------

def cmd_bundles_show(args: argparse.Namespace) -> int:
    cfg = BundlesConfig.load()
    members = cfg.get(args.name)
    fleet = active_fleet_name() or "?"
    print(cyan(f"Bundle '{args.name}' (fleet '{fleet}'):"))
    if not members:
        print(gray("  (empty)"))
        return 0
    try:
        all_repos = discover_repos()
    except FleetError:
        all_repos = []
    tok_w = max(len(t) for t in members)
    for tok in members:
        marker = ""
        if all_repos:
            try:
                resolve_repo(tok, all_repos)
            except FleetError:
                marker = yellow("  [missing]")
        print(f"  {tok.ljust(tok_w)}{marker}")
    return 0


# ---------------------------------------------------------------------------
# bundles remove
# ---------------------------------------------------------------------------

def cmd_bundles_remove(args: argparse.Namespace) -> int:
    with _config_lock(bundles_path()):
        cfg = BundlesConfig.load()
        cfg.remove(args.name)
        cfg.save()
    print(green(f"✓ Bundle '{args.name}' removed."))
    return 0


# ---------------------------------------------------------------------------
# bundles edit
# ---------------------------------------------------------------------------

def cmd_bundles_edit(args: argparse.Namespace) -> int:
    add_tokens = _split_csv(args.add) if args.add else []
    remove_tokens = _split_csv(args.remove) if args.remove else []
    if not add_tokens and not remove_tokens:
        raise FleetError(
            "bundles edit needs --add TOKENS and/or --remove TOKENS."
        )

    with _config_lock(bundles_path()):
        cfg = BundlesConfig.load()
        current = cfg.get(args.name)
        not_member = [t for t in remove_tokens if t not in current]
        if not_member:
            print(yellow(
                f"WARN: not in bundle '{args.name}': "
                f"{', '.join(not_member)} (ignoring)"
            ))
        stored = cfg.edit(args.name, add_tokens, remove_tokens)
        cfg.save()

    if add_tokens:
        _warn_unresolved(add_tokens)
    print(green(f"✓ Bundle '{args.name}' updated ({len(stored)} repo(s))."))
    return 0


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet bundles`` command group."""
    p = subparsers.add_parser("bundles", help="manage per-fleet repo bundles")
    sub = p.add_subparsers(dest="bundles_cmd", required=True,
                           metavar="<command>")

    b_add = sub.add_parser("add", parents=[fleet_arg],
                           help="create a new bundle")
    b_add.add_argument("name", help="bundle name")
    b_add.add_argument("--repos", required=True,
                       help="comma-separated repo tokens "
                            "(use group/path/name to disambiguate)")
    b_add.add_argument("--force", action="store_true",
                       help="overwrite an existing bundle of the same name")
    b_add.set_defaults(func=cmd_bundles_add)

    b_list = sub.add_parser("list", parents=[fleet_arg],
                            help="list every bundle in the active fleet")
    b_list.set_defaults(func=cmd_bundles_list)

    b_show = sub.add_parser("show", parents=[fleet_arg],
                            help="show the contents of a bundle")
    b_show.add_argument("name", help="bundle name")
    b_show.set_defaults(func=cmd_bundles_show)

    b_rm = sub.add_parser("remove", parents=[fleet_arg],
                          help="delete a bundle (config only; no git changes)")
    b_rm.add_argument("name", help="bundle name")
    b_rm.set_defaults(func=cmd_bundles_remove)

    b_edit = sub.add_parser("edit", parents=[fleet_arg],
                            help="add and/or remove members of a bundle")
    b_edit.add_argument("name", help="bundle name")
    b_edit.add_argument("--add", default=None,
                        help="comma-separated tokens to append")
    b_edit.add_argument("--remove", default=None,
                        help="comma-separated tokens to drop")
    b_edit.set_defaults(func=cmd_bundles_edit)

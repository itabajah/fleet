"""``fleet repos`` — list every repo on disk, grouped, with disabled markers."""

from __future__ import annotations

import argparse

from fleet.console import dim
from fleet.discovery import RepoInfo, discover_repos


def cmd_repos(_args: argparse.Namespace) -> int:
    repos = discover_repos()
    if not repos:
        print("No git repos found under the configured Repos root.")
        return 0

    by_group: dict[str, list[RepoInfo]] = {}
    for r in repos:
        by_group.setdefault(r.group_path, []).append(r)

    # Top-level (group_path == "") first, then groups alphabetically.
    ordered = sorted(by_group.keys(), key=lambda g: (g != "", g))
    for group in ordered:
        label = group if group else "(top-level)"
        print(f"\n{label}:")
        for r in by_group[group]:
            marker = ""
            if not r.enabled:
                marker = dim("  (disabled)")
            elif not r.in_registry:
                marker = dim("  (not in registry)")
            print(f"  {r.name}{marker}")

    name_counts: dict[str, int] = {}
    for r in repos:
        name_counts[r.name] = name_counts.get(r.name, 0) + 1
    dupes = sorted(n for n, c in name_counts.items() if c > 1)
    if dupes:
        print(f"\nNote: {len(dupes)} repo name(s) appear in more than one "
              f"group: {', '.join(dupes)}")
        print("      Use 'group/path/name' syntax with --repos to disambiguate.")

    enabled = sum(1 for r in repos if r.enabled)
    print(f"\nTotal: {len(repos)} repos ({enabled} enabled) across "
          f"{len(by_group)} group(s).")
    return 0


def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet repos`` subcommand."""
    p = subparsers.add_parser(
        "repos", parents=[fleet_arg],
        help="list every git repo under the active fleet's root",
    )
    p.set_defaults(func=cmd_repos)

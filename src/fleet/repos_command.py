"""``fleet repos`` — list every repo on disk, grouped, with disabled markers."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from typing import TYPE_CHECKING

from fleet.console import dim, gray

if TYPE_CHECKING:
    from fleet.discovery import RepoInfo


def _matches_filter(r: RepoInfo, pattern: str) -> bool:
    """True if the repo's leaf name or full display path matches ``pattern``.

    Glob semantics (``fnmatch``), case-insensitive, tested against both the
    bare name and the ``group/path/name`` form so ``--filter "infra/*"`` and
    ``--filter "*-cli"`` both work.
    """
    p = pattern.lower()
    return (fnmatch.fnmatch(r.name.lower(), p)
            or fnmatch.fnmatch(r.display_name.lower(), p))


def cmd_repos(args: argparse.Namespace) -> int:
    from fleet.discovery import discover_repos

    repos = discover_repos()

    pattern = getattr(args, "filter", None)
    if pattern:
        repos = [r for r in repos if _matches_filter(r, pattern)]

    if getattr(args, "as_json", False):
        for r in repos:
            sys.stdout.write(json.dumps({
                "name": r.name,
                "group": r.group_path or None,
                "display_name": r.display_name,
                "path": str(r.path),
                "enabled": r.enabled,
                "in_registry": r.in_registry,
            }) + "\n")
        return 0

    if not repos:
        if pattern:
            print(f"No git repos match --filter {pattern!r}.")
        else:
            print("No git repos found under the configured Repos root.")
        return 0

    by_group: dict[str, list[RepoInfo]] = {}
    for r in repos:
        by_group.setdefault(r.group_path, []).append(r)

    # Top-level (group_path == "") first, then groups alphabetically.
    ordered = sorted(by_group.keys(), key=lambda g: (g != "", g))
    any_disabled = False
    any_unregistered = False
    for group in ordered:
        label = group if group else "(top-level)"
        print(f"\n{label}:")
        for r in by_group[group]:
            marker = ""
            if not r.enabled:
                marker = dim("  (disabled)")
                any_disabled = True
            elif not r.in_registry:
                marker = dim("  (not in registry)")
                any_unregistered = True
            print(f"  {r.name}{marker}")

    if any_disabled or any_unregistered:
        print()
        print(gray("Legend:"))
        if any_disabled:
            print(gray("  (disabled)         in fleet.json but sync:false "
                       "or excluded — skipped by `fleet sync`"))
        if any_unregistered:
            print(gray("  (not in registry)  on disk but not listed in "
                       "fleet.json — run `fleet scan` to add it"))

    name_counts: dict[str, int] = {}
    for r in repos:
        name_counts[r.name] = name_counts.get(r.name, 0) + 1
    dupes = sorted(n for n, c in name_counts.items() if c > 1)
    if dupes:
        print(f"\nNote: {len(dupes)} repo name(s) appear in more than one "
              f"group: {', '.join(dupes)}")
        print("      Use 'group/path/name' syntax with --repos to disambiguate.")

    enabled = sum(1 for r in repos if r.enabled)
    scope = f" matching {pattern!r}" if pattern else ""
    print(f"\nTotal: {len(repos)} repos{scope} ({enabled} enabled) across "
          f"{len(by_group)} group(s).")
    return 0


def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet repos`` subcommand."""
    p = subparsers.add_parser(
        "repos", parents=[fleet_arg],
        help="list every git repo under the active fleet's root",
    )
    p.add_argument("--filter", metavar="GLOB", default=None,
                   help="only show repos whose name or group/path/name "
                        "matches this glob (e.g. 'infra/*', '*-cli')")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="emit one JSON object per repo on stdout")
    p.set_defaults(func=cmd_repos)

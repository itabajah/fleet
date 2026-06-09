"""``fleet doctor`` — read-only health check across config, registries, bundles, tasks.

Surfaces the problems that otherwise only show up as confusing failures deep
inside another command:

  * a fleet whose root directory no longer exists,
  * a ``fleet.json`` / ``bundles.json`` that won't parse,
  * a fleet with no ``fleet.json`` yet (needs ``fleet scan``),
  * bundle members that no longer resolve to a repo on disk,
  * task workspaces whose worktree or canonical paths have vanished,
  * no default fleet set.

Never mutates anything. Exits :data:`fleet.errors.EXIT_PARTIAL` (2) when any
problem is found so it's usable as a CI / pre-flight gate; 0 when clean.
"""

from __future__ import annotations

import argparse

from fleet.console import cyan, gray, green, red, yellow
from fleet.errors import EXIT_OK, EXIT_PARTIAL, FleetError
from fleet.fleets_config import FleetsConfig, config_path
from fleet.state import (
    invalidate_registry_cache,
    load_registry,
    registry_path,
    set_active_fleet,
    tasks_root,
)


class _Report:
    """Accumulates problems and notices, then renders a summary."""

    def __init__(self) -> None:
        self.problems = 0
        self.notices = 0

    def ok(self, msg: str) -> None:
        print(f"  {green('✓')} {msg}")

    def problem(self, msg: str) -> None:
        self.problems += 1
        print(f"  {red('✗')} {msg}")

    def notice(self, msg: str) -> None:
        self.notices += 1
        print(f"  {yellow('⚠')} {msg}")


def _check_fleets(cfg: FleetsConfig, report: _Report) -> None:
    print(cyan("Fleets"))
    bc = cfg.branch
    sample = (f"{bc.prefix}/<fleet>/<task>" if bc.scoped
              else f"{bc.prefix}/<task>")
    print(gray(f"  branch convention: {sample}"))
    if not cfg.fleets:
        report.notice("No fleets configured. Add one with `fleet fleets add`.")
        return
    if cfg.default is None:
        report.notice("No default fleet set. "
                      "Run `fleet fleets default <name>`.")
    for name in sorted(cfg.fleets):
        entry = cfg.fleets[name]
        if not entry.root.is_dir():
            report.problem(
                f"fleet '{name}': root {entry.root} does not exist. "
                f"Re-add with `fleet fleets add {name} --root <path> --force`."
            )
            continue
        report.ok(f"fleet '{name}': root {entry.root}")


def _check_active(cfg: FleetsConfig, override: str | None,
                  report: _Report) -> None:
    """Deep checks (registry parse, bundles, tasks) for one fleet."""
    from fleet.bundles_config import BundlesConfig
    from fleet.discovery import discover_repos
    from fleet.tasks.manifest import Manifest
    from fleet.tasks.validation import resolve_repo
    try:
        entry = cfg.resolve(override)
    except FleetError as e:
        report.notice(f"Skipping registry/bundle/task checks: {e}")
        return
    if not entry.root.is_dir():
        return  # already reported in _check_fleets

    set_active_fleet(entry.name, entry.root, cfg.branch)
    invalidate_registry_cache()

    print(cyan(f"\nRegistry (fleet '{entry.name}')"))
    reg_path = registry_path()
    if not reg_path.is_file():
        report.notice(f"No fleet.json yet at {reg_path}. Run `fleet scan`.")
    else:
        try:
            load_registry()
            report.ok(f"{reg_path} parses")
        except FleetError as e:
            report.problem(str(e))

    print(cyan(f"\nBundles (fleet '{entry.name}')"))
    try:
        bundles = BundlesConfig.load()
    except FleetError as e:
        report.problem(str(e))
        bundles = None
    if bundles is not None:
        if not bundles.bundles:
            report.ok("no bundles defined")
        else:
            try:
                all_repos = discover_repos()
            except FleetError:
                all_repos = []
            for bname in bundles.names():
                missing = []
                for tok in bundles.bundles[bname]:
                    if all_repos:
                        try:
                            resolve_repo(tok, all_repos)
                        except FleetError:
                            missing.append(tok)
                if missing:
                    report.problem(
                        f"bundle '{bname}': unresolved member(s): "
                        f"{', '.join(missing)}"
                    )
                else:
                    report.ok(f"bundle '{bname}' ({len(bundles.bundles[bname])} repo(s))")

    print(cyan(f"\nTasks (fleet '{entry.name}')"))
    troot = tasks_root()
    if not troot.is_dir():
        report.ok("no task workspaces")
        return
    task_dirs = sorted(
        p for p in troot.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )
    if not task_dirs:
        report.ok("no task workspaces")
        return
    for ws in task_dirs:
        manifest = Manifest.try_load(ws)
        if manifest is None:
            report.problem(f"task '{ws.name}': task.json missing or unparseable")
            continue
        broken = []
        for r in manifest.repos:
            if not r.canonical_path.is_dir():
                broken.append(f"{r.name} (canonical gone)")
            elif not r.worktree_path.is_dir():
                broken.append(f"{r.name} (worktree gone)")
        if broken:
            report.problem(
                f"task '{ws.name}': {', '.join(broken)}"
            )
        else:
            report.ok(f"task '{ws.name}' ({len(manifest.repos)} repo(s))")


def cmd_doctor(args: argparse.Namespace) -> int:
    print(gray(f"Config file: {config_path()}\n"))
    report = _Report()

    # Load defensively: a malformed config (e.g. a bad ``branch`` convention)
    # is exactly what doctor exists to diagnose, so report it as a problem
    # rather than letting the FleetError abort the whole check.
    try:
        cfg: FleetsConfig | None = FleetsConfig.load()
    except FleetError as e:
        print(cyan("Fleets"))
        report.problem(str(e))
        cfg = None

    if cfg is not None:
        _check_fleets(cfg, report)
        _check_active(cfg, getattr(args, "fleet", None), report)

    print()
    if report.problems:
        print(red(f"✗ {report.problems} problem(s) found"
                  + (f", {report.notices} notice(s)" if report.notices else "")))
        return EXIT_PARTIAL
    if report.notices:
        print(yellow(f"⚠ No problems, {report.notices} notice(s)."))
        return EXIT_OK
    print(green("✓ All checks passed."))
    return EXIT_OK


def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet doctor`` subcommand."""
    p = subparsers.add_parser(
        "doctor", parents=[fleet_arg],
        help="diagnose fleet config, registries, bundles, and tasks",
    )
    p.set_defaults(func=cmd_doctor)

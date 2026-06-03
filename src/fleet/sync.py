"""Parallel sync runner: fetch + pull --ff-only across all enabled repos.

Pre-flight per-host auth probe runs sequentially so we prompt for
credentials at most once per host before the worker pool launches. The
host-info computed there is cached and reused by the per-repo workers so
they don't re-shell-out to ``git remote get-url``.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from fleet import git_ops
from fleet.console import cyan, dim, gray, green, magenta, red, yellow
from fleet.discovery import RepoInfo, repos_to_sync
from fleet.errors import FleetError

# Cap to avoid trip-wiring git host abuse rate-limits / exhausting local network.
_MAX_PARALLEL = 32
DEFAULT_WORKERS = 10
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 2
# Cap exponential backoff so future bumps to _MAX_RETRIES can't silently
# produce minute-long waits between retries.
_MAX_BACKOFF_SECONDS = 30


@dataclass
class _HostInfo:
    """Cached origin info for a single repo, populated during the auth probe."""
    host: str | None       # None when repo has no origin (or isn't a git repo)
    is_repo: bool
    has_origin: bool


@dataclass
class _Line:
    text: str
    color: str = ""  # one of '', 'red', 'yellow', 'green', 'magenta', 'cyan', 'gray'


@dataclass
class _Result:
    index: int
    name: str
    status: str = "Unknown"  # Success | DryRun | Skipped | Failed | NotGitRepo
    message: str = ""
    output: list[_Line] = field(default_factory=list)


def _color_for(name: str):
    """Return the colorising function for a status-color name."""
    return {
        "": lambda s: s,
        "red": red, "yellow": yellow, "green": green,
        "magenta": magenta, "cyan": cyan, "gray": gray, "dim": dim,
    }.get(name, lambda s: s)


def _print_lines(lines: list[_Line]) -> None:
    for line in lines:
        print(_color_for(line.color)(line.text))


# ---------------------------------------------------------------------------
# Per-host auth probe (also caches host info for every repo)
# ---------------------------------------------------------------------------

def _gather_host_info(repos: list[RepoInfo]) -> dict[int, _HostInfo]:
    """Compute (is_repo, has_origin, host) for each repo, keyed by ``id()``.

    Returned mapping is keyed by ``id(repo)`` to avoid forcing RepoInfo to
    be hashable on its full tuple (it's a frozen dataclass with a ``Path``,
    which IS hashable, but ``id()`` is cheaper and unambiguous).
    """
    info: dict[int, _HostInfo] = {}
    for r in repos:
        if not git_ops.is_git_repo(r.path):
            info[id(r)] = _HostInfo(host=None, is_repo=False, has_origin=False)
            continue
        url = git_ops.origin_url(r.path)
        if not url:
            info[id(r)] = _HostInfo(host=None, is_repo=True, has_origin=False)
            continue
        info[id(r)] = _HostInfo(
            host=git_ops.origin_host(url), is_repo=True, has_origin=True,
        )
    return info


def _probe_host(repo: RepoInfo) -> tuple[bool, str]:
    """Run the auth probe for one repo. Returns ``(ok, error_text)``.

    Retries transient failures up to ``_MAX_RETRIES`` so a brief network
    blip doesn't permanently cull a host's repos for the run. Treats
    warning-only output as success (Windows reftable / case-insensitive
    advisories etc.).
    """
    delay = _INITIAL_BACKOFF_SECONDS
    last_err = ""
    for attempt in range(1, _MAX_RETRIES + 1):
        result = git_ops.ls_remote_head(repo.path)
        if result.ok or git_ops.is_warning_only(result):
            return True, ""
        last_err = (result.stderr or result.stdout).strip()
        if attempt < _MAX_RETRIES and git_ops.is_transient_error(result):
            time.sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
            continue
        break
    return False, last_err


def _auth_probe(repos: list[RepoInfo],
                host_info: dict[int, _HostInfo]) -> list[str]:
    """Probe one repo per unique git host. Returns the list of failed hosts."""
    print(cyan("====================================="))
    print(cyan("Authentication Check"))
    print(cyan("====================================="))
    print()

    by_host: dict[str, RepoInfo] = {}
    for r in repos:
        info = host_info[id(r)]
        if info.host is None:
            continue
        by_host.setdefault(info.host, r)

    if not by_host:
        print(yellow("⚠ No repositories with an origin remote were found; "
                     "skipping auth check."))
        print()
        return []

    print(yellow(f"Probing {len(by_host)} host(s) to cache credentials..."))
    print()

    failures: list[str] = []
    for host, repo in by_host.items():
        print(f"  {host:<30} ({repo.name})")
        ok, err = _probe_host(repo)
        if ok:
            print(green("    ✓ OK"))
        else:
            print(red(f"    ✗ Failed: {err}"))
            failures.append(host)
    print()
    return failures


# ---------------------------------------------------------------------------
# Per-repo worker
# ---------------------------------------------------------------------------

def _retry_call(label: str, fn, output: list[_Line]) -> git_ops.GitResult:
    """Run ``fn()`` (returns ``GitResult``) with retry on transient failures."""
    delay = _INITIAL_BACKOFF_SECONDS
    last: git_ops.GitResult | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        result = fn()
        last = result
        if result.ok:
            return result
        if git_ops.is_warning_only(result):
            output.append(_Line(
                f"  ⚠ {label} succeeded with filesystem warnings (safe to ignore)",
                "gray",
            ))
            return result

        if attempt < _MAX_RETRIES and git_ops.is_transient_error(result):
            output.append(_Line(
                f"  ⚠ {label} failed (attempt {attempt}/{_MAX_RETRIES}), "
                f"retrying in {delay}s...",
                "yellow",
            ))
            time.sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
            continue
        break
    assert last is not None  # _MAX_RETRIES >= 1
    return last


def _process_repo(repo: RepoInfo, info: _HostInfo, index: int, dry_run: bool,
                  progress_lock: threading.Lock) -> _Result:
    res = _Result(index=index, name=repo.name)
    out = res.output

    try:
        if not info.is_repo:
            res.status = "NotGitRepo"
            return res

        out.append(_Line(f"Processing: {repo.display_name}", "yellow"))

        cur = git_ops.current_branch(repo.path)
        if cur:
            out.append(_Line(f"  Current branch: {cur}", "gray"))
        else:
            out.append(_Line(
                "  ⚠ Detached HEAD; skipping to avoid clobbering checkout state",
                "yellow",
            ))
            res.status = "Skipped"
            res.message = "Detached HEAD"
            return res

        if git_ops.is_dirty(repo.path):
            out.append(_Line("  ⚠ Uncommitted changes. Skipping.", "yellow"))
            res.status = "Skipped"
            res.message = "Uncommitted changes"
            return res

        if not info.has_origin:
            out.append(_Line("  ⚠ No origin remote. Skipping.", "yellow"))
            res.status = "Skipped"
            res.message = "No origin remote"
            return res

        if dry_run:
            out.append(_Line("  [DRY RUN] Would fetch and pull", "magenta"))
            res.status = "DryRun"
            return res

        out.append(_Line("  Fetching from origin...", "gray"))
        fetch = _retry_call("Fetch", lambda: git_ops.fetch_prune(repo.path), out)
        if not fetch.ok and not git_ops.is_warning_only(fetch):
            out.append(_Line(f"  ✗ Fetch failed: {fetch.combined}", "red"))
            res.status = "Failed"
            res.message = "Fetch failed"
            return res

        try:
            default = git_ops.detect_default_branch(repo.path)
        except FleetError:
            out.append(_Line("  ⚠ Could not determine default branch", "yellow"))
            res.status = "Skipped"
            res.message = "No default branch"
            return res

        if cur != default:
            out.append(_Line(f"  Switching to {default}...", "gray"))
            co = git_ops.checkout(repo.path, default)
            if not co.ok:
                out.append(_Line("  ✗ Checkout failed", "red"))
                res.status = "Failed"
                res.message = "Checkout failed"
                return res

        out.append(_Line("  Pulling latest changes...", "gray"))
        pull = _retry_call(
            "Pull",
            lambda: git_ops.pull_ff_only(repo.path, default),
            out,
        )
        if not pull.ok and not git_ops.is_warning_only(pull):
            out.append(_Line(f"  ✗ Pull failed: {pull.combined}", "red"))
            res.status = "Failed"
            res.message = "Pull failed"
            return res

        out.append(_Line("  ✓ Successfully updated", "green"))
        res.status = "Success"

    except Exception as e:  # noqa: BLE001 — surface unexpected errors per-repo
        out.append(_Line(f"  ✗ Error: {e}", "red"))
        res.status = "Failed"
        res.message = str(e)
    finally:
        with progress_lock:
            sys.stdout.write(green("."))
            sys.stdout.flush()

    return res


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    repos = repos_to_sync()

    print(cyan("Configuration Stats:"))
    print(f"  Repositories enabled to sync: {len(repos)}")
    print()

    if not repos:
        print(yellow("⚠ No repositories enabled to sync."))
        print(gray("  Try `fleet scan` to populate fleet.json first."))
        return 0

    # Cache host info up-front for every repo. Both the auth probe and the
    # per-repo worker read from this map, so we shell out once per repo.
    host_info = _gather_host_info(repos)

    # Determine worker count. --workers 0 means "auto: as many as repos, capped".
    if args.workers == 0:
        workers = min(len(repos), _MAX_PARALLEL)
        if len(repos) > _MAX_PARALLEL:
            print(yellow(
                f"⚠ --workers 0 requested for {len(repos)} repos; "
                f"capped at {_MAX_PARALLEL} to avoid network/host exhaustion."
            ))
    else:
        workers = max(1, min(args.workers, _MAX_PARALLEL))

    auth_skipped: list[RepoInfo] = []
    if not args.no_auth_check:
        failures = _auth_probe(repos, host_info)
        if failures:
            failed_set = set(failures)
            kept: list[RepoInfo] = []
            for r in repos:
                host = host_info[id(r)].host
                if host is not None and host in failed_set:
                    auth_skipped.append(r)
                else:
                    kept.append(r)
            print(red(f"✗ Authentication failed for: {', '.join(failures)}"))
            print(yellow(
                f"  Skipping {len(auth_skipped)} repo(s) on those host(s); "
                "use --no-auth-check to bypass the probe entirely."
            ))
            if not kept:
                print(red("✗ Every enabled repo is on a failed-auth host. "
                          "Nothing to do."))
                return 1
            repos = kept
        else:
            print(green("✓ Authentication cached for all hosts."))
        print()

    print(cyan("====================================="))
    if args.dry_run:
        print(magenta("DRY RUN - No changes will be made"))
    print(cyan(f"Processing {len(repos)} repositories with {workers} worker(s)"))
    print(cyan("====================================="))
    print()
    sys.stdout.write(cyan("Progress: "))
    sys.stdout.flush()

    progress_lock = threading.Lock()
    results: list[_Result] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_process_repo, r, host_info[id(r)], i,
                        args.dry_run, progress_lock)
            for i, r in enumerate(repos)
        ]
        for fut in as_completed(futures):
            results.append(fut.result())

    sys.stdout.write(green(" Done!\n"))
    sys.stdout.flush()
    print()

    # Synthesize Skipped results for any repos the auth probe culled, so
    # they show up in per-repo output and in the summary counts.
    base_index = len(repos)
    for offset, r in enumerate(auth_skipped):
        skipped = _Result(
            index=base_index + offset,
            name=r.name,
            status="Skipped",
            message="auth failed for host",
        )
        skipped.output.append(_Line(f"Processing: {r.display_name}", "yellow"))
        skipped.output.append(
            _Line("  ⚠ Skipped: host failed the auth probe.", "yellow")
        )
        results.append(skipped)

    results.sort(key=lambda res: res.index)

    for result in results:
        if result.output:
            _print_lines(result.output)
            print()

    counts = {
        "Success": 0, "DryRun": 0, "Skipped": 0,
        "Failed": 0, "NotGitRepo": 0,
    }
    errors: list[str] = []
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "Failed":
            errors.append(f"{result.name}: {result.message}")

    print(cyan("====================================="))
    print(cyan("Summary:"))
    print(cyan("====================================="))
    print(f"  Total processed:      {len(results)}")
    print(green(f"  Successfully updated: {counts['Success']}"))
    if counts["DryRun"]:
        print(magenta(f"  Dry run (would pull): {counts['DryRun']}"))
    print(yellow(f"  Skipped:              {counts['Skipped']}"))
    print(red(f"  Failed:               {counts['Failed']}"))
    if counts["NotGitRepo"]:
        print(gray(f"  Not a git repo:       {counts['NotGitRepo']}"))
    print()
    if errors:
        print(red("Errors encountered:"))
        for e in errors:
            print(red(f"  - {e}"))
        print()

    if counts["Failed"] > 0:
        # Match the partial-failure exit code used by `fleet task sync`
        # (and FleetError's documented convention).
        print(yellow("⚠ Completed with errors"))
        return 2
    if counts["Success"] == 0 and counts["DryRun"] == 0:
        print(yellow("⚠ No repositories were updated"))
        return 0
    print(green("✓ All operations completed successfully!"))
    return 0


def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet sync`` subcommand."""
    p = subparsers.add_parser(
        "sync", parents=[fleet_arg],
        help="parallel git pull --ff-only across every enabled repo",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="preview changes without modifying repos")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"number of parallel workers "
                        f"(default {DEFAULT_WORKERS}, 0 = auto, capped at 32)")
    p.add_argument("--no-auth-check", action="store_true",
                   help="skip the per-host credential probe")
    p.set_defaults(func=cmd_sync)

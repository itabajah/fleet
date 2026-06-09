"""Read-only task commands: ``fleet task list``, ``info``, ``path``, ``sync``.

``sync`` does mutate (it pulls) but only in a recoverable way (FF-only on
already-fetched refs), so it lives here rather than in
:mod:`fleet.tasks.lifecycle`.
"""

from __future__ import annotations

import argparse
import json
import sys

from fleet import git_ops
from fleet.console import cyan, dim, green, red, yellow
from fleet.errors import FleetError
from fleet.refnames import validate_task_name
from fleet.state import active_fleet_name, tasks_root
from fleet.tasks.manifest import Manifest

# ---------------------------------------------------------------------------
# task list
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _list_workspaces():
    """Yield ``(workspace_dir, manifest_or_None)`` for every active task."""
    root = tasks_root()
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        yield entry, Manifest.try_load(entry)


def cmd_list(args: argparse.Namespace) -> int:
    root = tasks_root()
    if not root.is_dir():
        if getattr(args, "as_json", False):
            return 0
        print(f"No tasks yet for fleet '{active_fleet_name()}' "
              f"({root} doesn't exist).")
        return 0

    if getattr(args, "as_json", False):
        # JSON mode: minimal one-object-per-line on stdout. Always quick.
        for workspace, manifest in _list_workspaces():
            if manifest is None:
                payload = {"name": workspace.name, "error": "unparseable_manifest"}
            else:
                payload = {
                    "name": manifest.name,
                    "branch": manifest.branch,
                    "created_at": manifest.created_at,
                    "repos": [
                        {"name": r.name, "group": r.group}
                        for r in manifest.repos
                    ],
                }
            print(json.dumps(payload, separators=(",", ":")))
        return 0

    rows: list[dict] = []
    for workspace, manifest in _list_workspaces():
        if manifest is None:
            rows.append({
                "name": workspace.name,
                "branch": "-",
                "created": "(no manifest)",
                "repos": "-",
                "status": "?",
            })
            continue
        repo_names = [r.name for r in manifest.repos]

        status_parts: list[str] = []
        if not args.quick:
            dirty_count = 0
            unpushed_total = 0
            unpushed_unknown = 0
            missing = 0
            for r in manifest.repos:
                if not r.worktree_path.is_dir():
                    missing += 1
                    continue
                # A worktree can vanish or its git state break between the
                # is_dir() check and these calls; treat any git failure as
                # "missing" so one bad repo doesn't abort the whole listing.
                try:
                    if git_ops.is_dirty(r.worktree_path):
                        dirty_count += 1
                    n = git_ops.unpushed_count(r.worktree_path, manifest.branch)
                except FleetError:
                    missing += 1
                    continue
                if n is None:
                    unpushed_unknown += 1
                elif n > 0:
                    unpushed_total += n
            if dirty_count:
                status_parts.append(red(f"dirty:{dirty_count}"))
            if unpushed_total:
                status_parts.append(yellow(f"unpushed:{unpushed_total}"))
            if unpushed_unknown:
                status_parts.append(dim(f"not-pushed:{unpushed_unknown}"))
            if missing:
                status_parts.append(red(f"missing:{missing}"))
            if not status_parts:
                status_parts.append(green("clean"))

        rows.append({
            "name": manifest.name,
            "branch": manifest.branch,
            "created": manifest.created_at,
            "repos": ", ".join(repo_names) or "-",
            "status": "  ".join(status_parts) if status_parts else "",
        })

    if not rows:
        print(f"No active tasks under {root}.")
        return 0

    name_w = max(len(r["name"]) for r in rows)
    branch_w = max(len(r["branch"]) for r in rows)
    for r in rows:
        branch_padded = f"{r['branch']:<{branch_w}}"
        line = (f"{r['name']:<{name_w}}  "
                f"{cyan(branch_padded)}  "
                f"created={r['created']}  "
                f"repos=[{r['repos']}]")
        if r["status"]:
            line += f"  {r['status']}"
        print(line)
    print(f"\nTotal: {len(rows)} task(s) in fleet '{active_fleet_name()}'.")
    return 0


# ---------------------------------------------------------------------------
# task info
# ---------------------------------------------------------------------------

def cmd_info(args: argparse.Namespace) -> int:
    name: str = args.name
    validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")
    manifest = Manifest.load(workspace)

    print(cyan(f"Task: {name}"))
    print(f"  branch:      {manifest.branch}")
    print(f"  created:     {manifest.created_at}")
    print(f"  workspace:   {workspace}")
    desc = manifest.description.strip()
    if desc:
        first = desc.splitlines()[0]
        if len(first) > 80:
            first = first[:77] + "..."
        print(f"  description: {first}")

    scratch = workspace / "scratch"
    if scratch.is_dir():
        files = [p for p in scratch.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        print(f"  scratch:     {len(files)} file(s), {_human_size(total)}")

    if not manifest.repos:
        print("\n(no repos in manifest)")
        return 0

    print(f"\n{cyan('Repos:')}")
    for r in manifest.repos:
        print(f"  - {r.display_name}")
        print(f"      canonical: {r.canonical_path}")
        print(f"      worktree:  {r.worktree_path}")
        if not r.worktree_path.is_dir():
            print(f"      status:    {red('worktree directory missing')}")
            continue
        try:
            flags: list[str] = []
            if git_ops.is_dirty(r.worktree_path):
                flags.append(red("dirty"))
            n = git_ops.unpushed_count(r.worktree_path, manifest.branch)
        except FleetError as e:
            print(f"      status:    {red(f'unavailable ({e})')}")
            continue
        if n is None:
            flags.append(yellow("never pushed"))
        elif n > 0:
            flags.append(yellow(f"{n} unpushed"))
        else:
            flags.append(green("in sync with origin"))
        print(f"      status:    {', '.join(flags)}")
        head_line = git_ops.run_git(
            "log", "-1", "--format=%H %h %s",
            cwd=r.worktree_path, check=False,
        )
        if head_line.ok:
            full, _, rest = head_line.stdout.strip().partition(" ")
            short, _, subject = rest.partition(" ")
            ref = git_ops.run_git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=r.worktree_path, check=False)
            cur = ref.stdout.strip() if ref.ok else full[:7]
            print(f"      head:      {cur} :: {short} {subject}")
    return 0


# ---------------------------------------------------------------------------
# task path  (printed for shell integration: `cd $(fleet task path foo)`)
# ---------------------------------------------------------------------------

def cmd_path(args: argparse.Namespace) -> int:
    name: str = args.name
    validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(
            f"No such task in fleet '{active_fleet_name()}': {workspace}"
        )
    # Explicit raw write so the contract (one line, terminated with a
    # single newline) is obvious to shell-integration callers.
    sys.stdout.write(str(workspace) + "\n")
    return 0


# ---------------------------------------------------------------------------
# task sync
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    name: str = args.name
    validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")
    manifest = Manifest.load(workspace)

    if not manifest.repos:
        print("(manifest has no repos)")
        return 0

    print(f"Syncing task '{name}' (branch {manifest.branch}):")
    any_failure = False
    for r in manifest.repos:
        label = f"  [{r.name}]"
        wt = r.worktree_path
        if not wt.is_dir():
            print(f"{label} worktree missing on disk; skipping")
            any_failure = True
            continue
        if git_ops.is_dirty(wt):
            print(f"{label} uncommitted changes; skipping pull "
                  f"(commit/stash, then re-run)")
            any_failure = True
            continue

        fetch = git_ops.fetch_prune(wt)
        if not fetch.ok and not git_ops.is_warning_only(fetch):
            print(f"{label} fetch failed:\n    {fetch.stderr.strip()}")
            any_failure = True
            continue

        # After fetch, the local origin/<branch> ref is current. Use it
        # rather than another network round-trip via ls-remote.
        rp = git_ops.run_git("rev-parse", "--verify", "--quiet",
                             f"refs/remotes/origin/{manifest.branch}",
                             cwd=wt, check=False)
        if not rp.ok or not rp.stdout.strip():
            print(f"{label} fetched (branch '{manifest.branch}' not on origin yet)")
            continue

        pull = git_ops.pull_ff_only(wt, manifest.branch)
        if pull.ok or git_ops.is_warning_only(pull):
            summary = (pull.stdout or "").strip().splitlines()
            tail = summary[-1] if summary else "ok"
            print(f"{label} {tail}")
        else:
            print(f"{label} pull --ff-only failed:\n    {pull.stderr.strip()}")
            any_failure = True

    return 2 if any_failure else 0

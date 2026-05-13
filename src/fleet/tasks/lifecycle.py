"""Mutating task commands: ``fleet task new`` and ``fleet task end``."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fleet import git_ops
from fleet.console import yellow
from fleet.discovery import RepoInfo, discover_repos
from fleet.errors import FleetError
from fleet.state import archive_root, tasks_root
from fleet.tasks.manifest import Manifest, RepoEntry, now_iso
from fleet.tasks.validation import resolve_repo, task_branch, validate_task_name

# ---------------------------------------------------------------------------
# Per-repo task setup helpers
# ---------------------------------------------------------------------------

def _prepare_canonical(repo: RepoInfo, no_pull: bool) -> str:
    """Fetch (and FF-pull when safe) the canonical repo. Return default branch.

    With ``no_pull=True`` we skip both the network fetch and the local pull,
    and use the offline-only default-branch detector so we don't surprise
    the user with a hidden network round-trip.
    """
    label = f"[{repo.name}]"

    if no_pull:
        default = git_ops.detect_default_branch(repo.path, offline=True)
        print(f"  {label} default branch: {default} (skipping fetch + pull)")
        return default

    print(f"  {label} fetching...")
    fetch = git_ops.fetch_prune(repo.path)
    if not fetch.ok and not git_ops.is_warning_only(fetch):
        print(f"  {label} WARN: fetch failed:\n    {fetch.stderr.strip()}\n"
              f"    (continuing with whatever origin/* is cached)")

    default = git_ops.detect_default_branch(repo.path)

    if git_ops.is_dirty(repo.path):
        print(f"  {label} canonical has local changes; "
              f"skipping local pull (worktree will branch from origin/{default})")
        return default

    cur = git_ops.current_branch(repo.path)
    if cur == default:
        pull = git_ops.pull_ff_only(repo.path, default)
        if pull.ok or git_ops.is_warning_only(pull):
            print(f"  {label} pulled latest on {default}")
        else:
            print(f"  {label} WARN: pull --ff-only failed:\n    "
                  f"{pull.stderr.strip()}\n"
                  f"    (worktree will branch from origin/{default})")
    else:
        # Update the local default ref to match origin without checking out.
        upd = git_ops.run_git("fetch", "origin", f"{default}:{default}",
                              cwd=repo.path, check=False)
        if upd.ok:
            print(f"  {label} updated local {default} from origin")
        else:
            print(f"  {label} WARN: could not fast-forward local {default}:\n"
                  f"    {upd.stderr.strip()}\n"
                  f"    (worktree will branch from origin/{default})")
    return default


def _add_worktree(repo: RepoInfo, name: str, default_branch: str,
                  task_workspace: Path) -> Path:
    """Create a worktree for ``repo`` under the task workspace. Returns its path."""
    branch = task_branch(name)
    wt_path = task_workspace / repo.name
    label = f"[{repo.name}]"

    if wt_path.exists():
        raise FleetError(f"Worktree path already exists: {wt_path}")

    existing = git_ops.run_git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
        cwd=repo.path, check=False,
    )
    if existing.ok:
        raise FleetError(
            f"Branch '{branch}' already exists in {repo.name}. "
            f"Pick a different task name or delete the branch with "
            f"`git -C \"{repo.path}\" branch -D {branch}`."
        )

    # Also reject if the branch already exists on origin (e.g. a previous
    # task with the same name was pushed before being ended). Otherwise we'd
    # create a fresh local branch from origin/<default> that diverges from
    # the remote one and the first non-force push would be rejected.
    origin_existing = git_ops.run_git(
        "show-ref", "--verify", "--quiet",
        f"refs/remotes/origin/{branch}",
        cwd=repo.path, check=False,
    )
    if origin_existing.ok:
        raise FleetError(
            f"Branch '{branch}' already exists on origin in {repo.name}. "
            f"Pick a different task name, or delete it from origin first "
            f"with `git -C \"{repo.path}\" push origin --delete {branch}`."
        )

    # `--no-track` is critical: without it, branching from `origin/<default>`
    # silently sets the new branch's upstream to `origin/<default>`. That
    # means the first plain `git push` either errors confusingly
    # (push.default = simple) or, worse, pushes to the default branch on
    # origin (push.default = upstream). With `--no-track`, the user must
    # explicitly `git push -u origin <branch>` the first time — which is
    # what we want.
    git_ops.run_git(
        "worktree", "add", "--no-track", str(wt_path),
        "-b", branch, f"origin/{default_branch}",
        cwd=repo.path,
    )
    print(f"  {label} worktree -> {wt_path} (branch {branch})")
    return wt_path


# ---------------------------------------------------------------------------
# task new
# ---------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    name: str = args.name
    validate_task_name(name)

    root = tasks_root()
    workspace = root / name
    if workspace.exists():
        raise FleetError(
            f"Task '{name}' already exists at {workspace}. "
            f"Pick a different name or `fleet task end {name}` first."
        )

    repo_tokens = [t.strip() for t in args.repos.split(",") if t.strip()]
    if not repo_tokens:
        raise FleetError("--repos requires at least one repo name.")

    all_repos = discover_repos()
    if not all_repos:
        raise FleetError("No repos discovered under the configured Repos root.")

    seen: set[tuple[str, str]] = set()
    chosen: list[RepoInfo] = []
    for tok in repo_tokens:
        repo = resolve_repo(tok, all_repos)
        key = (repo.group_path, repo.name)
        if key in seen:
            print(f"note: ignoring duplicate '{tok}'")
            continue
        seen.add(key)
        chosen.append(repo)

    # Worktree paths are `<workspace>/<repo.name>`, so two repos that
    # resolve to the same leaf name (e.g. `foo` and `bar/foo`) would
    # collide on disk halfway through scaffolding. Catch it before any
    # mutation.
    by_leaf: dict[str, RepoInfo] = {}
    for r in chosen:
        prior = by_leaf.get(r.name)
        if prior is not None:
            raise FleetError(
                f"Two selected repos share leaf name '{r.name}': "
                f"'{prior.display_name}' and '{r.display_name}'. "
                f"Their worktrees would collide at {workspace / r.name}. "
                f"Pick a different combination of repos."
            )
        by_leaf[r.name] = r

    print(f"Creating task '{name}' with {len(chosen)} repo(s):")
    for r in chosen:
        print(f"  - {r.display_name}")
    print()

    if args.dry_run:
        print(yellow("--dry-run: would pull each canonical (default branch), "
                     "then create one worktree per repo at:"))
        for r in chosen:
            print(f"  {workspace / r.name}  (branch {task_branch(name)})")
        print(f"  + {workspace / 'context.md'}")
        print(f"  + {workspace / 'scratch'}")
        print(f"  + {workspace / 'task.json'}")
        return 0

    # Compute the branch name (and resolve the active fleet) BEFORE any
    # filesystem mutation so a misconfiguration doesn't leave behind an
    # empty workspace dir.
    branch = task_branch(name)
    description = args.description

    workspace.mkdir(parents=True, exist_ok=False)

    created_worktrees: list[tuple[RepoInfo, Path]] = []
    try:
        for repo in chosen:
            default = _prepare_canonical(repo, no_pull=args.no_pull)
            wt = _add_worktree(repo, name, default, workspace)
            created_worktrees.append((repo, wt))

        context_md = workspace / "context.md"
        context_md.write_text(
            f"# {name}\n\n"
            f"_Branch:_ `{branch}`\n"
            f"_Created:_ {now_iso()}\n\n"
            f"## Description\n\n{description}\n\n"
            f"## Repos\n\n"
            + "\n".join(
                f"- `{r.name}`"
                + (f"  _(group: {r.group_path})_" if r.group_path else "")
                for r in chosen
            )
            + "\n\n## Notes\n\n"
            "_Use this file to capture acceptance criteria, decisions, links._\n",
            encoding="utf-8",
        )
        (workspace / "scratch").mkdir(exist_ok=True)

        manifest = Manifest(
            name=name,
            branch=branch,
            created_at=now_iso(),
            description=description,
            repos=[
                RepoEntry(
                    name=r.name,
                    group=r.group_path or None,
                    canonical_path=r.path,
                    worktree_path=workspace / r.name,
                )
                for r in chosen
            ],
        )
        manifest.save(workspace)
    except BaseException:
        # Catch BaseException so Ctrl-C also triggers rollback.
        print("\nERROR during scaffolding — rolling back created worktrees...",
              file=sys.stderr)
        for repo, wt in created_worktrees:
            git_ops.run_git("worktree", "remove", "--force", str(wt),
                            cwd=repo.path, check=False)
            git_ops.run_git("branch", "-D", branch,
                            cwd=repo.path, check=False)
        with contextlib.suppress(Exception):
            shutil.rmtree(workspace, ignore_errors=True)
        raise

    print(f"\nTask ready: {workspace}")
    print(f"Next: fleet open {name}")
    return 0


# ---------------------------------------------------------------------------
# task end
# ---------------------------------------------------------------------------

def _archive_workspace(workspace: Path, name: str) -> Path:
    """Zip up ``task.json`` + ``context.md`` + ``scratch/`` into the archive root.

    Uses a UTC timestamp so two ``task end`` runs in different time zones
    don't collide subtly. The collision guard handles same-second retries.
    """
    archive_dir = archive_root()
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"{name}-{stamp}.zip"
    if archive_path.exists():
        i = 2
        while True:
            candidate = archive_dir / f"{name}-{stamp}-{i}.zip"
            if not candidate.exists():
                archive_path = candidate
                break
            i += 1

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for top in ("task.json", "context.md"):
            f = workspace / top
            if f.is_file():
                zf.write(f, arcname=top)
        scratch = workspace / "scratch"
        if scratch.is_dir():
            for item in scratch.rglob("*"):
                if item.is_file():
                    zf.write(item, arcname=str(item.relative_to(workspace)))
    return archive_path


def cmd_end(args: argparse.Namespace) -> int:
    name: str = args.name
    validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")

    manifest = Manifest.load(workspace)
    branch = manifest.branch

    dirty: list[str] = []
    unpushed_warnings: list[str] = []
    live_worktrees: list[RepoEntry] = []
    for r in manifest.repos:
        if not r.worktree_path.is_dir():
            print(f"note: worktree '{r.name}' already gone ({r.worktree_path})")
            continue
        live_worktrees.append(r)
        if git_ops.is_dirty(r.worktree_path):
            dirty.append(r.name)
        n = git_ops.unpushed_count(r.worktree_path, branch)
        if n is None:
            unpushed_warnings.append(
                f"  {r.name}: branch '{branch}' was never pushed"
            )
        elif n > 0:
            unpushed_warnings.append(
                f"  {r.name}: {n} unpushed commit(s) on '{branch}'"
            )

    if dirty and not args.force:
        raise FleetError(
            "Refusing to end — uncommitted changes in: "
            + ", ".join(dirty)
            + "\nCommit/stash, or re-run with --force."
        )
    if dirty and args.force:
        print(f"WARN: --force ignoring uncommitted changes in: "
              f"{', '.join(dirty)}")
    if unpushed_warnings:
        print("WARN: unpushed work that will become orphan-able once "
              "the worktree is removed:")
        for line in unpushed_warnings:
            print(line)
        print("(branch stays in the canonical repo, so this is recoverable.)")

    archive_path = _archive_workspace(workspace, name)
    print(f"Archived metadata -> {archive_path}")

    failures: list[str] = []
    for r in live_worktrees:
        flag = ["--force"] if args.force else []
        proc = git_ops.run_git("worktree", "remove", *flag,
                               str(r.worktree_path),
                               cwd=r.canonical_path, check=False)
        if not proc.ok:
            failures.append(f"  {r.name}: {proc.stderr.strip()}")
        else:
            print(f"  removed worktree {r.name}")

    for r in manifest.repos:
        if r.canonical_path.is_dir():
            git_ops.run_git("worktree", "prune", cwd=r.canonical_path, check=False)

    if failures:
        print("\nWARN: some worktrees could not be removed:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        print(f"\nFix those manually, then delete the workspace folder:\n"
              f"  {workspace}", file=sys.stderr)
        return 2

    try:
        shutil.rmtree(workspace)
        print(f"Removed workspace {workspace}")
    except OSError as e:
        raise FleetError(
            f"Couldn't remove {workspace}: {e}\n"
            f"(VS Code or another process may still be holding it open.)"
        ) from e

    print(f"\nTask '{name}' ended. Branch '{branch}' remains in each "
          f"canonical repo (and on origin if pushed).")
    return 0

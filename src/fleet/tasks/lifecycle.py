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
from fleet.bundles_config import expand_bundle_tokens
from fleet.console import yellow
from fleet.discovery import RepoInfo, discover_repos
from fleet.errors import FleetError
from fleet.state import archive_root, tasks_root
from fleet.tasks.manifest import Manifest, RepoEntry, now_iso
from fleet.tasks.validation import resolve_repo, task_branch, validate_task_name
from fleet.tasks.worktree import add_worktree, prepare_canonical


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
    repo_tokens = expand_bundle_tokens(repo_tokens)
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

    created_worktrees: list[tuple[RepoInfo, Path, str]] = []
    try:
        for repo in chosen:
            default = prepare_canonical(repo, no_pull=args.no_pull)
            wt, _branch_is_new = add_worktree(repo, name, default, workspace)
            created_worktrees.append((repo, wt, branch))

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
        # Catch BaseException so Ctrl-C also triggers rollback. Only the
        # branches we actually created (paired with worktrees in this run)
        # are deleted — never delete a branch we didn't make.
        print("\nERROR during scaffolding — rolling back created worktrees...",
              file=sys.stderr)
        for repo, wt, br in created_worktrees:
            git_ops.run_git("worktree", "remove", "--force", str(wt),
                            cwd=repo.path, check=False)
            git_ops.run_git("branch", "-D", br,
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
    don't collide subtly. The collision guard handles same-second retries
    and is bounded so a pathological archive directory can't hang us.
    """
    archive_dir = archive_root()
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"{name}-{stamp}.zip"
    if archive_path.exists():
        for i in range(2, 1002):
            candidate = archive_dir / f"{name}-{stamp}-{i}.zip"
            if not candidate.exists():
                archive_path = candidate
                break
        else:
            raise FleetError(
                f"Refusing to archive: more than 1000 archives already exist "
                f"with prefix {name}-{stamp} in {archive_dir}."
            )

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for top in ("task.json", "context.md"):
            f = workspace / top
            if f.is_file():
                zf.write(f, arcname=top)
        scratch = workspace / "scratch"
        if scratch.is_dir():
            for item in scratch.rglob("*"):
                if item.is_file():
                    # POSIX-style arcname so archives extracted on
                    # Linux/macOS nest correctly (zip spec mandates '/').
                    rel = item.relative_to(workspace).as_posix()
                    zf.write(item, arcname=rel)
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
        # Worktree teardown failed. The archive is still useful; tell the
        # user it's there so a retry can short-circuit re-archiving.
        print("\nWARN: some worktrees could not be removed:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        print(f"\nFix those manually, then delete the workspace folder:\n"
              f"  {workspace}\n"
              f"(metadata already archived to {archive_path}; safe to "
              f"discard the workspace once the worktrees are gone.)",
              file=sys.stderr)
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

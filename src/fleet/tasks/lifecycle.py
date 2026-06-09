"""Mutating task commands: ``fleet task new`` and ``fleet task end``."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fleet import git_ops
from fleet.console import yellow
from fleet.discovery import RepoInfo, discover_repos
from fleet.errors import FleetError
from fleet.refnames import validate_task_name
from fleet.state import archive_root, tasks_root
from fleet.tasks.manifest import Manifest, RepoEntry, now_iso
from fleet.tasks.status import block_or_warn_dirty, repo_status, unpushed_warning
from fleet.tasks.validation import select_repos, split_repo_tokens, task_branch
from fleet.tasks.worktree import (
    add_worktree,
    assert_no_leaf_collision,
    prepare_canonicals_parallel,
)

# ---------------------------------------------------------------------------
# task new
# ---------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    name: str = args.name
    validate_task_name(name)

    root = tasks_root()
    workspace = root / name
    # Best-effort fast path for a friendly message; the authoritative guard
    # is the exclusive mkdir below (closes the exists()→mkdir TOCTOU window).
    if workspace.exists():
        raise FleetError(
            f"Task '{name}' already exists at {workspace}. "
            f"Pick a different name or `fleet task end {name}` first."
        )

    repo_tokens = split_repo_tokens(args.repos)

    all_repos = discover_repos()
    if not all_repos:
        raise FleetError("No repos discovered under the configured Repos root.")

    chosen = select_repos(repo_tokens, all_repos)

    # Worktree paths are `<workspace>/<repo.name>`, so two repos that
    # resolve to the same leaf name (e.g. `foo` and `bar/foo`) would
    # collide on disk halfway through scaffolding. Catch it before any
    # mutation. Case-insensitive on Windows/macOS so 'Foo' vs 'foo'
    # collides there too.
    assert_no_leaf_collision(chosen, workspace)

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

    try:
        workspace.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # Lost the race against a concurrent `task new` (or a stray dir
        # appeared after the pre-check). Same user-facing message.
        raise FleetError(
            f"Task '{name}' already exists at {workspace}. "
            f"Pick a different name or `fleet task end {name}` first."
        ) from None

    created_worktrees: list[tuple[RepoInfo, Path, str]] = []
    try:
        # Prepare every canonical concurrently (network fetch/pull is the
        # slow part), then create worktrees serially in selection order so
        # output and rollback stay deterministic.
        branch_by_repo = prepare_canonicals_parallel(chosen, no_pull=args.no_pull)
        for repo in chosen:
            default = branch_by_repo[id(repo)]
            wt, _branch_is_new = add_worktree(repo, branch, default, workspace)
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
    don't collide subtly. A short random suffix disambiguates same-second
    runs without any retry cap.
    """
    archive_dir = archive_root()
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"{name}-{stamp}.zip"
    if archive_path.exists():
        # Same-second collision (or a re-run): tack on a random suffix.
        # uuid4 makes a clash astronomically unlikely, so no bounded retry
        # loop is needed.
        while archive_path.exists():
            archive_path = archive_dir / f"{name}-{stamp}-{uuid.uuid4().hex[:8]}.zip"

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
        st = repo_status(r.worktree_path, branch)
        if st.dirty:
            dirty.append(r.name)
        warn = unpushed_warning(r.name, branch, st.unpushed)
        if warn is not None:
            unpushed_warnings.append(warn)

    block_or_warn_dirty(dirty, unpushed_warnings, force=args.force, action="end")

    archive_path = _archive_workspace(workspace, name)
    print(f"Archived metadata -> {archive_path}")

    failures: list[str] = []
    touched_canonicals: set[Path] = set()
    for r in live_worktrees:
        flag = ["--force"] if args.force else []
        proc = git_ops.run_git("worktree", "remove", *flag,
                               str(r.worktree_path),
                               cwd=r.canonical_path, check=False)
        touched_canonicals.add(r.canonical_path)
        if not proc.ok:
            failures.append(f"  {r.name}: {proc.stderr.strip()}")
        else:
            print(f"  removed worktree {r.name}")

    # Prune only the canonicals we actually removed a worktree from, rather
    # than every repo in the manifest (cheaper, and avoids touching repos
    # whose worktree was already gone).
    for cpath in touched_canonicals:
        if cpath.is_dir():
            git_ops.run_git("worktree", "prune", cwd=cpath, check=False)

    if failures:
        # Teardown hit an unexpected error (the dirty/unpushed gate ran
        # earlier, so this isn't user data we promised to preserve). Finish
        # the job best-effort so the task isn't left half-dead: drop the
        # workspace, then re-prune so no orphan `.git/worktrees/<leaf>`
        # admin entries linger.
        print("\nWARN: some worktrees could not be removed cleanly:",
              file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        shutil.rmtree(workspace, ignore_errors=True)
        for cpath in touched_canonicals:
            if cpath.is_dir():
                git_ops.run_git("worktree", "prune", cwd=cpath, check=False)
        still_there = workspace.exists()
        if still_there:
            print(f"\nThe workspace folder could not be fully removed:\n"
                  f"  {workspace}\n"
                  f"Close anything holding it open and delete it manually.",
                  file=sys.stderr)
        else:
            print(f"\nWorkspace removed despite the errors above; metadata is "
                  f"archived at {archive_path}.", file=sys.stderr)
        print(f"\nBranch '{branch}' still exists in each canonical (recoverable).",
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

"""Reusable per-repo task scaffolding helpers.

Extracted from :mod:`fleet.tasks.lifecycle` so both ``task new`` and
``task add-repo`` go through the same code path (same ``--no-track``
rationale, same branch-exists guards in both the canonical and origin).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from fleet import git_ops
from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.tasks.validation import task_branch


# True on Windows / default macOS HFS+ where 'Foo' and 'foo' collide on disk.
_FS_CASE_INSENSITIVE = os.path.normcase("A") == os.path.normcase("a")


def _leaf_key(name: str) -> str:
    """Normalize a worktree leaf for collision checks.

    On case-insensitive filesystems (Windows, macOS default), ``Foo`` and
    ``foo`` resolve to the same directory and the second ``git worktree
    add`` will fail mid-scaffold. Casefold there; keep exact match on
    case-sensitive filesystems so two legitimately-distinct repos with
    only-case-differing names still work on Linux.
    """
    return name.casefold() if _FS_CASE_INSENSITIVE else name


def assert_no_leaf_collision(
    chosen: list[RepoInfo],
    workspace: Path,
    *,
    existing_leaves: Iterable[str] = (),
) -> None:
    """Pre-flight check that no two worktrees will land at the same path.

    Detects three cases:
      * Two newly-selected repos share a (normalized) leaf name.
      * A newly-selected repo's leaf collides with an existing manifest entry.

    Raises :class:`FleetError` BEFORE any disk mutation so partial scaffolds
    can't happen.
    """
    existing_keys = {_leaf_key(n) for n in existing_leaves}
    by_leaf: dict[str, RepoInfo] = {}
    for r in chosen:
        key = _leaf_key(r.name)
        prior = by_leaf.get(key)
        if prior is not None:
            raise FleetError(
                f"Two selected repos share leaf name '{r.name}': "
                f"'{prior.display_name}' and '{r.display_name}'. "
                f"Their worktrees would collide at {workspace / r.name}. "
                f"Pick a different combination of repos."
            )
        if key in existing_keys:
            raise FleetError(
                f"Worktree leaf '{r.name}' already exists in task at "
                f"{workspace / r.name}. Pick a different repo."
            )
        by_leaf[key] = r


def prepare_canonical(repo: RepoInfo, no_pull: bool) -> str:
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
        upd = git_ops.run_git("fetch", "origin", f"{default}:{default}",
                              cwd=repo.path, check=False)
        if upd.ok:
            print(f"  {label} updated local {default} from origin")
        else:
            print(f"  {label} WARN: could not fast-forward local {default}:\n"
                  f"    {upd.stderr.strip()}\n"
                  f"    (worktree will branch from origin/{default})")
    return default


def add_worktree(repo: RepoInfo, name: str, default_branch: str,
                 task_workspace: Path, *,
                 reuse_existing: bool = False) -> tuple[Path, bool]:
    """Create a worktree for ``repo`` under the task workspace.

    Returns ``(worktree_path, branch_is_new)``. ``branch_is_new`` is False
    when ``reuse_existing`` was honoured (the canonical already had the
    task's branch) — callers must NOT ``git branch -D`` that branch during
    rollback, since it pre-existed this invocation.
    """
    branch = task_branch(name)
    wt_path = task_workspace / repo.name
    label = f"[{repo.name}]"

    if wt_path.exists():
        raise FleetError(f"Worktree path already exists: {wt_path}")

    local_exists = git_ops.run_git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
        cwd=repo.path, check=False,
    ).ok

    if local_exists and not reuse_existing:
        raise FleetError(
            f"Branch '{branch}' already exists in {repo.name}. "
            f"Pick a different task name or delete the branch with "
            f"`git -C \"{repo.path}\" branch -D {branch}`."
        )

    origin_existing = git_ops.run_git(
        "show-ref", "--verify", "--quiet",
        f"refs/remotes/origin/{branch}",
        cwd=repo.path, check=False,
    )
    if origin_existing.ok and not local_exists:
        # If the local branch exists we already vouched for its history; only
        # block when there's a pushed branch we'd otherwise diverge from.
        raise FleetError(
            f"Branch '{branch}' already exists on origin in {repo.name}. "
            f"Pick a different task name, or delete it from origin first "
            f"with `git -C \"{repo.path}\" push origin --delete {branch}`."
        )

    if local_exists:
        # Re-attach to the existing branch. No -b, no --no-track — we're
        # not creating anything new; the branch (and its upstream config,
        # if any) is preserved verbatim.
        git_ops.run_git(
            "worktree", "add", str(wt_path), branch,
            cwd=repo.path,
        )
        print(f"  {label} worktree -> {wt_path} (reattached existing branch {branch})")
        return wt_path, False

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
    return wt_path, True

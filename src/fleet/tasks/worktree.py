"""Reusable per-repo task scaffolding helpers.

Extracted from :mod:`fleet.tasks.lifecycle` so both ``task new`` and
``task add-repo`` go through the same code path (same ``--no-track``
rationale, same branch-exists guards in both the canonical and origin).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fleet import git_ops
from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.tasks.validation import task_branch

# True on Windows / default macOS HFS+ where 'Foo' and 'foo' collide on disk.
_FS_CASE_INSENSITIVE = os.path.normcase("A") == os.path.normcase("a")

# Cap parallel canonical preparation so a large task doesn't open dozens of
# simultaneous fetches against one host. Mirrors sync's conservative default.
_PREP_MAX_WORKERS = 10


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


def prepare_canonical(repo: RepoInfo, no_pull: bool) -> tuple[str, bool, list[str]]:
    """Fetch (and FF-pull when safe) the canonical repo.

    Returns ``(default_branch, fetch_failed, output_lines)``. Output is
    *buffered* into ``output_lines`` rather than printed directly so callers
    can run several of these concurrently (see
    :func:`prepare_canonicals_parallel`) and still emit per-repo lines in a
    stable order. ``fetch_failed`` is True when the network fetch errored and
    we fell back to cached refs, so the caller can surface one aggregated
    warning.

    With ``no_pull=True`` we skip both the network fetch and the local pull,
    and use the offline-only default-branch detector so we don't surprise
    the user with a hidden network round-trip.
    """
    label = f"[{repo.name}]"
    lines: list[str] = []

    if no_pull:
        default = git_ops.detect_default_branch(repo.path, offline=True)
        lines.append(f"  {label} default branch: {default} (skipping fetch + pull)")
        return default, False, lines

    lines.append(f"  {label} fetching...")
    fetch = git_ops.fetch_prune(repo.path)
    fetch_failed = False
    if not fetch.ok and not git_ops.is_warning_only(fetch):
        fetch_failed = True
        lines.append(f"  {label} WARN: fetch failed:\n    {fetch.stderr.strip()}\n"
                     f"    (continuing with whatever origin/* is cached)")

    default = git_ops.detect_default_branch(repo.path)

    if git_ops.is_dirty(repo.path):
        lines.append(f"  {label} canonical has local changes; "
                     f"skipping local pull (worktree will branch from origin/{default})")
        return default, fetch_failed, lines

    cur = git_ops.current_branch(repo.path)
    if cur == default:
        pull = git_ops.pull_ff_only(repo.path, default)
        if pull.ok or git_ops.is_warning_only(pull):
            lines.append(f"  {label} pulled latest on {default}")
        else:
            lines.append(f"  {label} WARN: pull --ff-only failed:\n    "
                         f"{pull.stderr.strip()}\n"
                         f"    (worktree will branch from origin/{default})")
    else:
        upd = git_ops.fetch_branch_ff(repo.path, default)
        if upd.ok:
            lines.append(f"  {label} updated local {default} from origin")
        else:
            lines.append(f"  {label} WARN: could not fast-forward local {default}:\n"
                         f"    {upd.stderr.strip()}\n"
                         f"    (worktree will branch from origin/{default})")
    return default, fetch_failed, lines


def prepare_canonicals_parallel(
    chosen: list[RepoInfo], *, no_pull: bool,
) -> dict[int, str]:
    """Prepare every canonical concurrently; print per-repo lines in order.

    Returns ``{id(repo): default_branch}`` so the (serial) worktree-creation
    pass can look up each repo's branch. Per-repo output is buffered and
    flushed in selection order so concurrency doesn't scramble the log. If
    any prepare raised, the first error (by selection order) is re-raised
    after flushing the successful output \u2014 callers create worktrees only
    after this returns, so a prep failure means nothing to roll back yet.

    A single aggregated warning is emitted when one or more fetches failed
    and fell back to cached refs.
    """
    n = len(chosen)
    branches: list[str | None] = [None] * n
    fetch_failed: list[bool] = [False] * n
    out_lines: list[list[str]] = [[] for _ in range(n)]
    errors: list[BaseException | None] = [None] * n

    def _work(i: int, repo: RepoInfo) -> None:
        try:
            default, failed, lines = prepare_canonical(repo, no_pull=no_pull)
            branches[i] = default
            fetch_failed[i] = failed
            out_lines[i] = lines
        except BaseException as e:  # noqa: BLE001 — recorded, re-raised in order
            errors[i] = e

    workers = max(1, min(n, _PREP_MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_work, i, r) for i, r in enumerate(chosen)]
        for f in futs:
            f.result()  # _work swallows; this only surfaces internal errors

    # Flush buffered output in selection order.
    for lines in out_lines:
        for line in lines:
            print(line)

    # Re-raise the first prep error (after output is visible).
    for err in errors:
        if err is not None:
            raise err

    failed_repos = [chosen[i].name for i in range(n) if fetch_failed[i]]
    if failed_repos:
        print(f"  WARN: fetch failed for {len(failed_repos)} repo(s) "
              f"({', '.join(failed_repos)}); branched from cached refs.")

    result: dict[int, str] = {}
    for i in range(n):
        branch = branches[i]
        if branch is not None:
            result[id(chosen[i])] = branch
    return result


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
        # Reattaching to a pre-existing branch (typical after a
        # remove-repo / add-repo cycle). Guard against a *stale* branch
        # whose history has diverged from the canonical's default branch —
        # e.g. a leftover from an interrupted run on an old base. If
        # origin/<default> isn't an ancestor of the branch, reattaching
        # would silently resurrect divergent work, so refuse with a clear
        # message instead. (Best-effort: a missing origin/<default> ref
        # skips the check rather than blocking offline reuse.)
        base_ref = f"origin/{default_branch}"
        have_base = git_ops.run_git(
            "show-ref", "--verify", "--quiet",
            f"refs/remotes/{base_ref}", cwd=repo.path, check=False,
        ).ok
        if have_base:
            ancestor = git_ops.run_git(
                "merge-base", "--is-ancestor", base_ref, branch,
                cwd=repo.path, check=False,
            )
            if not ancestor.ok:
                raise FleetError(
                    f"Existing branch '{branch}' in {repo.name} has diverged "
                    f"from {base_ref} (not a fast-forward descendant). "
                    f"Reattaching could resurrect stale work. Inspect it, then "
                    f"either rebase it onto {base_ref} or delete it with "
                    f"`git -C \"{repo.path}\" branch -D {branch}` and retry."
                )
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

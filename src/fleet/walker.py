"""Single disk-walk implementation shared by discovery and scan.

A "git repo" is any directory containing a ``.git`` entry — directory for
normal repos, regular file (``gitdir: ...``) for git worktrees. The walker
yields every such directory under the given root, descending into found
repos because the convention here is to clone sibling repos under a
repo's ``temp/`` subdirectory.

Pruning:

  * names in :data:`fleet.paths.PRUNE_DIRS` (``node_modules``, ``.venv``, …)
  * any path already visited (resolved-path dedup, so symlink/junction
    loops can't crash the walk)

No directory names are hard-coded as fleet-specific exclusions: users who
want a folder skipped should set ``sync: false`` on it in ``fleet.json``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from fleet.paths import IN_REPO_PRUNE_DIRS, PRUNE_DIRS

# Names skipped during disk walks at every level. ``.git`` is internal git state.
_PRUNE_DIRS = PRUNE_DIRS | {".git"}
# Names additionally skipped when descending into an already-discovered repo.
_IN_REPO_PRUNE_DIRS = IN_REPO_PRUNE_DIRS


def looks_like_git_repo(p: Path) -> bool:
    """True if ``p`` contains a ``.git`` entry (dir for normal repos, file for worktrees)."""
    return (p / ".git").exists()


def walk_repos(root: Path) -> Iterator[Path]:
    """Yield every directory under ``root`` that contains a ``.git`` entry.

    Iterative (stack-based) to avoid recursion-depth issues on huge trees.
    Order is unspecified — callers that need a stable order should sort
    after collecting.
    """
    seen: set[Path] = set()
    # Yield root itself if it's a repo, so a fleet rooted directly at a
    # single git checkout still discovers it. The bool in each stack entry
    # tracks whether we're already descending inside a discovered repo, so
    # build-output dirs (``target``/``build``/etc.) can be pruned only
    # there — not at the top level where users may group repos under them.
    root_is_repo = False
    try:
        root_real = root.resolve()
        if looks_like_git_repo(root):
            seen.add(root_real)
            root_is_repo = True
            yield root
    except OSError:
        pass
    stack: list[tuple[Path, bool]] = [(root, root_is_repo)]

    while stack:
        cur, inside_repo = stack.pop()
        try:
            scan = os.scandir(cur)
        except OSError:
            continue

        # Iterate the OS handle lazily (no list() materialization) so a
        # directory with tens of thousands of entries doesn't spike memory.
        with scan:
            for entry in scan:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name in _PRUNE_DIRS:
                    continue
                if inside_repo and entry.name in _IN_REPO_PRUNE_DIRS:
                    continue
                p = Path(entry.path)
                try:
                    real = p.resolve()
                except OSError:
                    continue
                if real in seen:
                    continue
                seen.add(real)
                is_repo = looks_like_git_repo(p)
                if is_repo:
                    yield p
                stack.append((p, inside_repo or is_repo))

"""Single disk-walk implementation shared by discovery and scan.

A "git repo" is any directory containing a ``.git`` entry — directory for
normal repos, regular file (``gitdir: ...``) for git worktrees. The walker
yields every such directory under the given root, descending into found
repos because the convention here is to clone sibling repos under a
repo's ``temp/`` subdirectory.

Pruning:

  * names in :data:`fleet.paths.PRUNE_DIRS` (``node_modules``, ``.venv``, …)
  * the ``..me`` directory (the fleet checkout's local convention)
  * the directory holding fleet's own source tree
  * any path already visited (resolved-path dedup, so symlink/junction
    loops can't crash the walk)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from fleet.paths import PRUNE_DIRS, TOOL_HOME_DIRNAME, fleet_install_dir

# Names skipped during disk walks. ``.git`` is internal git state.
_PRUNE_DIRS = PRUNE_DIRS | {".git", TOOL_HOME_DIRNAME}


def looks_like_git_repo(p: Path) -> bool:
    """True if ``p`` contains a ``.git`` entry (dir for normal repos, file for worktrees)."""
    return (p / ".git").exists()


def walk_repos(root: Path) -> Iterator[Path]:
    """Yield every directory under ``root`` that contains a ``.git`` entry.

    Iterative (stack-based) to avoid recursion-depth issues on huge trees.
    Order is unspecified — callers that need a stable order should sort
    after collecting.
    """
    try:
        skip = fleet_install_dir().resolve()
    except OSError:
        skip = None

    seen: set[Path] = set()
    stack: list[Path] = [root]

    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue

        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name in _PRUNE_DIRS:
                continue
            p = Path(entry.path)
            try:
                real = p.resolve()
            except OSError:
                continue
            if skip is not None and real == skip:
                continue
            if real in seen:
                continue
            seen.add(real)
            if looks_like_git_repo(p):
                yield p
            stack.append(p)

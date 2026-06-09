"""Shared worktree status + teardown-safety helpers for the task commands.

``task end`` and ``task remove-repo`` both reduce a set of worktrees to a
``dirty`` list and a set of ``unpushed`` warning lines, then apply the same
"block unless --force, otherwise warn" gate before tearing worktrees down.
Centralising that here keeps the two teardown paths byte-for-byte identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fleet import git_ops
from fleet.errors import FleetError


@dataclass(frozen=True)
class RepoStatus:
    """Working-tree state of one task worktree."""

    dirty: bool
    # None: no local origin/<branch> ref (never pushed, or never fetched back).
    unpushed: int | None


def repo_status(worktree_path: Path, branch: str) -> RepoStatus:
    """Dirty flag + unpushed-commit count for a single worktree.

    Propagates :class:`~fleet.errors.FleetError` from git (missing binary,
    broken repo); aggregate callers that must survive one bad worktree guard
    the call themselves.
    """
    return RepoStatus(
        dirty=git_ops.is_dirty(worktree_path),
        unpushed=git_ops.unpushed_count(worktree_path, branch),
    )


def unpushed_warning(name: str, branch: str, unpushed: int | None) -> str | None:
    """One ``  <name>: …`` warning line, or None when nothing is unpushed."""
    if unpushed is None:
        return f"  {name}: branch '{branch}' was never pushed"
    if unpushed > 0:
        return f"  {name}: {unpushed} unpushed commit(s) on '{branch}'"
    return None


def block_or_warn_dirty(
    dirty: list[str], unpushed_warnings: list[str], *, force: bool, action: str,
) -> None:
    """Pre-teardown gate shared by ``task end`` and ``task remove-repo``.

    Raises :class:`~fleet.errors.FleetError` when there are dirty worktrees and
    ``force`` is False; otherwise prints the (identical) dirty-ignored and
    unpushed-orphan warnings. ``action`` is the verb in the refusal message
    (``"end"`` / ``"remove"``).
    """
    if dirty and not force:
        raise FleetError(
            f"Refusing to {action} — uncommitted changes in: "
            + ", ".join(dirty)
            + "\nCommit/stash, or re-run with --force."
        )
    if dirty and force:
        print(f"WARN: --force ignoring uncommitted changes in: "
              f"{', '.join(dirty)}")
    if unpushed_warnings:
        print("WARN: unpushed work that will become orphan-able once "
              "the worktree is removed:")
        for line in unpushed_warnings:
            print(line)
        print("(branch stays in the canonical repo, so this is recoverable.)")

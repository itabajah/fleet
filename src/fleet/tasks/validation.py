"""Task name validation + ``--repos`` token resolution + branch-name builder."""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.state import branch_config, require_active_fleet

if TYPE_CHECKING:
    from fleet.tasks.manifest import Manifest, RepoEntry

# Filesystem-safe AND valid as a git branch suffix. The regex enforces the
# character set; ``validate_task_name`` additionally rules out the
# git-ref-format edge cases the regex can't express (``..``, leading/trailing
# ``.``, ``.lock`` suffix, ``@{``).
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")

# Characters git itself forbids inside any ref segment (see git-check-ref-format).
_BAD_BRANCH_CHARS = re.compile(r"[\s~^:?*\[\\\x00-\x1f\x7f]")

# Names Windows reserves for legacy DOS devices: a directory or branch leaf
# matching one (case-insensitively, with or without an extension) can't be
# created on Windows and confuses tooling on every OS. Rejected on all
# platforms so a task created on Linux can still be opened on Windows.
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def validate_task_name(name: str) -> None:
    """Raise :class:`FleetError` if ``name`` isn't safe to use everywhere."""
    if not _TASK_NAME_RE.match(name):
        raise FleetError(
            f"Invalid task name '{name}'. "
            "Use letters, digits, '.', '_', '-' (1-64 chars, must start "
            "with a letter or digit)."
        )
    if (".." in name
            or name.startswith(".") or name.endswith(".")
            or name.endswith(".lock") or "@{" in name):
        raise FleetError(
            f"Invalid task name '{name}'. Git would refuse the resulting "
            "branch (no '..', no leading/trailing '.', no '.lock' suffix, "
            "no '@{')."
        )
    # A trailing space is silently stripped by Windows when creating a
    # directory, which would desync the workspace name from the branch
    # suffix. The regex already blocks spaces, but guard explicitly in case
    # the character set is ever widened.
    if name != name.strip():
        raise FleetError(
            f"Invalid task name '{name}': leading/trailing whitespace."
        )
    # Reject reserved device names, with or without an extension
    # (``CON``, ``con.txt``, ``LPT1`` ...).
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise FleetError(
            f"Invalid task name '{name}': '{stem}' is a reserved device name "
            "on Windows. Pick a different name."
        )


def validate_branch(branch: str, *, context: str = "branch") -> None:
    """Raise :class:`FleetError` if ``branch`` isn't a valid git ref name.

    Defends against hand-edited manifests whose ``branch`` field would
    only fail much later, deep inside a git invocation. Catches empties,
    leading dashes (would parse as a CLI flag), git-special chars, and
    the same ref-format edge cases as :func:`validate_task_name`.
    """
    if not branch:
        raise FleetError(f"Invalid {context}: empty.")
    if branch.startswith("-"):
        raise FleetError(
            f"Invalid {context} '{branch}': must not start with '-' "
            "(would parse as a CLI flag)."
        )
    if _BAD_BRANCH_CHARS.search(branch):
        raise FleetError(
            f"Invalid {context} '{branch}': contains whitespace or one of "
            "the characters git forbids in refs (~ ^ : ? * [ \\ or control)."
        )
    if (".." in branch
            or branch.startswith(".") or branch.endswith(".")
            or branch.endswith(".lock") or "@{" in branch
            or branch.startswith("/") or branch.endswith("/")
            or "//" in branch):
        raise FleetError(
            f"Invalid {context} '{branch}': violates git ref-format rules "
            "(no '..', no leading/trailing '.' or '/', no '.lock' suffix, "
            "no '@{', no '//')."
        )


def task_branch(name: str) -> str:
    """Branch name created/managed by fleet for a *new* task.

    Rendered from the global convention pinned in ``fleets.json``
    (:class:`fleet.state.BranchConfig`). With ``scoped`` true the active fleet
    is inserted as a middle segment so two fleets sharing a physical canonical
    repo can each have a same-named task without their git branches
    colliding::

        scoped=True   ->  <prefix>/<fleet>/<task>   (default: task/<fleet>/<task>)
        scoped=False  ->  <prefix>/<task>

    Only callers creating a task (``task new``) or deliberately renaming its
    branch (``task rename``) render from config; everything operating on an
    existing task reads ``manifest.branch`` instead, so a later config change
    never desyncs a task's worktrees from its recorded branch.

    Both fleet and task names are validated git-ref-safe, and the rendered
    branch is checked against :func:`validate_branch` at config-load time, so
    the resulting ref is always valid.
    """
    cfg = branch_config()
    if cfg.scoped:
        return f"{cfg.prefix}/{require_active_fleet()}/{name}"
    return f"{cfg.prefix}/{name}"


def resolve_repo(token: str, all_repos: list[RepoInfo]) -> RepoInfo:
    """Resolve a ``--repos`` token to exactly one :class:`RepoInfo`.

    Accepts bare ``name``, or ``group/path/name`` (slashes anywhere in
    group). Raises :class:`FleetError` with did-you-mean suggestions on
    miss/ambiguity, or with a clear message when the repo is
    registry-disabled.
    """
    token = token.replace("\\", "/").strip("/")

    if "/" in token:
        # Split on last slash: everything before is group_path, last is name.
        group_path, _, name = token.rpartition("/")
        matches = [r for r in all_repos
                   if r.name == name and r.group_path == group_path]
        if not matches:
            raise FleetError(
                f"Unknown repo '{group_path}/{name}'. "
                f"Try `fleet repos` to see what's available."
            )
        chosen = matches[0]
    else:
        matches = [r for r in all_repos if r.name == token]
        if len(matches) == 0:
            suggestions = difflib.get_close_matches(
                token, sorted({r.name for r in all_repos}), n=3, cutoff=0.6,
            )
            hint = (f" Did you mean: {', '.join(suggestions)}?"
                    if suggestions else "")
            raise FleetError(f"Unknown repo '{token}'.{hint}")
        if len(matches) > 1:
            disambig = ", ".join(m.display_name for m in matches)
            raise FleetError(
                f"Ambiguous repo name '{token}' — exists in multiple groups. "
                f"Use the full path, one of: {disambig}"
            )
        chosen = matches[0]

    if not chosen.enabled:
        raise FleetError(
            f"Repo '{chosen.display_name}' is disabled in fleet.json "
            f"(sync:false or excluded). Re-enable it there before using it "
            f"as a task target."
        )
    return chosen


def require_repo_in_task(token: str, manifest: Manifest) -> RepoEntry:
    """Resolve a ``--repos`` token to an entry of ``manifest.repos``.

    Accepts a bare leaf name or ``group/path/name``. Raises
    :class:`FleetError` listing current members on miss.
    """
    norm = token.replace("\\", "/").strip("/")
    by_disp = {r.display_name: r for r in manifest.repos}
    by_name: dict[str, list[RepoEntry]] = {}
    for r in manifest.repos:
        by_name.setdefault(r.name, []).append(r)

    if norm in by_disp:
        return by_disp[norm]
    if "/" not in norm:
        cands = by_name.get(norm, [])
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            disambig = ", ".join(c.display_name for c in cands)
            raise FleetError(
                f"Ambiguous repo '{token}' in task '{manifest.name}' — "
                f"matches: {disambig}. Use the full 'group/path/name' form."
            )
    members = ", ".join(sorted(by_disp)) or "(none)"
    raise FleetError(
        f"Repo '{token}' is not in task '{manifest.name}'. "
        f"Members: {members}."
    )

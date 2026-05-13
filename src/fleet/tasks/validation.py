"""Task name validation + ``--repos`` token resolution + branch-name builder."""

from __future__ import annotations

import difflib
import re

from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.state import require_active_fleet

# Filesystem-safe AND valid as a git branch suffix. The regex enforces the
# character set; ``validate_task_name`` additionally rules out the
# git-ref-format edge cases the regex can't express (``..``, leading/trailing
# ``.``, ``.lock`` suffix, ``@{``).
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


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


def task_branch(name: str) -> str:
    """Branch name created/managed by fleet for a task.

    Namespaced by the active fleet so two fleets sharing a physical
    canonical repo can each have a same-named task without their git
    branches colliding. Format: ``task/<fleet>/<task>``.

    Both fleet names and task names are validated to be git-ref-safe
    (alphanum + ``.`` ``_`` ``-``), so the resulting ref is always valid.
    """
    return f"task/{require_active_fleet()}/{name}"


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
                f"Disambiguate with one of: {disambig}"
            )
        chosen = matches[0]

    if not chosen.enabled:
        raise FleetError(
            f"Repo '{chosen.display_name}' is disabled in fleet.json "
            f"(sync:false or excluded). Re-enable it there before using it "
            f"as a task target."
        )
    return chosen

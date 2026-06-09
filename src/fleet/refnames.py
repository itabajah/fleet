"""Git-ref-safe name validation, shared by task names, fleet names, branches.

A leaf module (only :mod:`re` + :mod:`fleet.errors`) so every caller —
``tasks.manifest``, ``tasks.lifecycle``/``edit``/``inspect``, and
``fleets_config`` — can import it at module scope without dragging in the
heavier ``tasks.validation`` (which imports ``discovery`` and ``state``).
That keeps these validators off the import path that previously forced
call-site-local (lazy) imports.
"""

from __future__ import annotations

import re

from fleet.errors import FleetError

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


def has_ref_format_violation(segment: str) -> bool:
    """True if ``segment`` breaks a git ref-format rule we reject everywhere.

    Covers the edge cases the character-set regexes can't express and that
    are shared by task names, fleet names, and rendered branches: ``..``, a
    leading or trailing ``.``, a ``.lock`` suffix, or ``@{``. Callers raise
    their own message (branch validation additionally rejects the slash rules
    a single segment can't have).
    """
    return (
        ".." in segment
        or segment.startswith(".")
        or segment.endswith(".")
        or segment.endswith(".lock")
        or "@{" in segment
    )


def validate_task_name(name: str) -> None:
    """Raise :class:`FleetError` if ``name`` isn't safe to use everywhere."""
    if not _TASK_NAME_RE.match(name):
        raise FleetError(
            f"Invalid task name '{name}'. "
            "Use letters, digits, '.', '_', '-' (1-64 chars, must start "
            "with a letter or digit)."
        )
    if has_ref_format_violation(name):
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
    if (has_ref_format_violation(branch)
            or branch.startswith("/") or branch.endswith("/")
            or "//" in branch):
        raise FleetError(
            f"Invalid {context} '{branch}': violates git ref-format rules "
            "(no '..', no leading/trailing '.' or '/', no '.lock' suffix, "
            "no '@{', no '//')."
        )

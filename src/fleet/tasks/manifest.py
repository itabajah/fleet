"""Task manifest model: ``task.json`` schema + atomic I/O.

Every other module in :mod:`fleet.tasks` reads/writes manifests through this
file so the ``isinstance(repo_entry, dict)`` checks aren't scattered across
the codebase. A malformed manifest raises :class:`FleetError` once at load
time; downstream code can rely on the dataclass shape.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fleet.errors import FleetError

MANIFEST_FILENAME = "task.json"
LOCK_FILENAME = ".task.lock"

# Bump when the on-disk schema changes in a way that needs migration. v1 is
# the first explicitly-versioned schema; manifests written before versioning
# simply omit the field and are read as v1.
MANIFEST_VERSION = 1

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


@contextlib.contextmanager
def task_lock(workspace: Path) -> Iterator[None]:
    """Cross-process lock around a single task's read-modify-write.

    Concurrent ``fleet task`` commands on the *same* task (e.g. ``add-repo``
    racing ``edit``) would otherwise clobber each other's manifest writes.
    Uses an exclusive-create sidecar (``<workspace>/.task.lock``). Raises
    :class:`FleetError` if the lock can't be acquired within the timeout
    (a peer process is mid-write, or a crash left a stale lock to remove).
    """
    lock_path = workspace / LOCK_FILENAME
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise FleetError(
                    f"Could not lock task at {workspace} after "
                    f"{_LOCK_TIMEOUT_SECONDS:g}s ({lock_path} is held). "
                    f"Another fleet task command may be running; if not, "
                    f"delete the lock file and retry."
                ) from None
            time.sleep(_LOCK_POLL_SECONDS)
        except OSError as e:
            raise FleetError(
                f"Could not create task lock {lock_path}: {e}"
            ) from e
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


def _migrate_raw(raw: dict, version: int, workspace: Path) -> dict:
    """Upgrade an older manifest dict to the current schema (in-memory).

    No historical versions exist yet (v1 is the first), so this is a
    structural seam: future schema bumps add upgrade branches here. A
    *newer* version than this build understands is rejected, so a
    downgraded fleet can't silently misread a manifest written by a newer
    one.
    """
    if version > MANIFEST_VERSION:
        raise FleetError(
            f"task.json in {workspace} is schema version {version}, but this "
            f"fleet build only understands up to {MANIFEST_VERSION}. "
            f"Upgrade fleet."
        )
    return raw


def now_iso() -> str:
    """UTC ISO-8601 timestamp (e.g. ``2026-05-13T10:00:00+00:00``).

    Anchored to UTC so two machines in different time zones produce
    comparable manifest timestamps for the same instant.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RepoEntry:
    """One repo's record inside ``task.json``."""

    name: str
    group: str | None
    canonical_path: Path
    worktree_path: Path

    @classmethod
    def from_dict(cls, raw: object, manifest_path: Path) -> RepoEntry:
        if not isinstance(raw, dict):
            raise FleetError(
                f"Malformed {MANIFEST_FILENAME} at {manifest_path}: 'repos' "
                f"entry is not an object: {raw!r}"
            )
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise FleetError(
                f"Malformed {MANIFEST_FILENAME} at {manifest_path}: 'repos' "
                f"entry missing string 'name': {raw!r}"
            )
        group = raw.get("group")
        if group is not None and not isinstance(group, str):
            raise FleetError(
                f"Malformed {MANIFEST_FILENAME} at {manifest_path}: repo "
                f"'{name}' has non-string 'group': {group!r}"
            )
        canonical = raw.get("canonical_path")
        worktree = raw.get("worktree_path")
        if not isinstance(canonical, str) or not isinstance(worktree, str):
            raise FleetError(
                f"Malformed {MANIFEST_FILENAME} at {manifest_path}: repo "
                f"'{name}' missing 'canonical_path' or 'worktree_path'."
            )
        canonical_path = Path(canonical)
        worktree_path = Path(worktree)
        for field_name, p in (("canonical_path", canonical_path),
                              ("worktree_path", worktree_path)):
            if not p.is_absolute():
                raise FleetError(
                    f"Malformed {MANIFEST_FILENAME} at {manifest_path}: "
                    f"repo '{name}' has non-absolute '{field_name}': "
                    f"{p!s}. Edit the manifest to use an absolute path."
                )
        return cls(
            name=name,
            group=group or None,
            canonical_path=canonical_path,
            worktree_path=worktree_path,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "group": self.group,
            "canonical_path": str(self.canonical_path),
            "worktree_path": str(self.worktree_path),
        }

    @property
    def display_name(self) -> str:
        return f"{self.group}/{self.name}" if self.group else self.name


@dataclass
class Manifest:
    """Validated ``task.json`` contents."""

    name: str
    branch: str
    created_at: str
    description: str
    repos: list[RepoEntry] = field(default_factory=list)
    version: int = MANIFEST_VERSION

    # ------------------------------ I/O --------------------------------------

    @classmethod
    def load(cls, workspace: Path) -> Manifest:
        """Load and validate the manifest at ``<workspace>/task.json``.

        Raises :class:`FleetError` for missing, unparseable, or
        schema-incomplete manifests so callers can surface a single clear
        message to the user.
        """
        manifest_path = workspace / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise FleetError(
                f"task.json missing or unreadable in {workspace}."
            )
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise FleetError(
                f"Malformed task.json in {workspace}: {e}"
            ) from e
        if not isinstance(raw, dict):
            raise FleetError(
                f"Malformed task.json in {workspace}: top-level value is "
                f"not an object."
            )
        raw_version = raw.get("version")
        version = raw_version if isinstance(raw_version, int) else MANIFEST_VERSION
        raw = _migrate_raw(raw, version, workspace)
        name = raw.get("name")
        branch = raw.get("branch")
        if not isinstance(name, str) or not name:
            raise FleetError(
                f"task.json in {workspace} missing required string 'name'."
            )
        if not isinstance(branch, str) or not branch:
            raise FleetError(
                f"task.json in {workspace} is missing the required 'branch' "
                f"field. Refusing to guess — fix the manifest manually."
            )
        # Lazy import: validation imports state, which has module-globals
        # we don't want to touch from manifest's import path. Keeping this
        # call-site-local also documents that manifest.py is intentionally
        # free of heavier sibling imports at module scope.
        from fleet.tasks.validation import validate_branch
        try:
            validate_branch(branch, context="task.json branch")
        except FleetError as e:
            raise FleetError(
                f"task.json in {workspace}: {e}"
            ) from e
        created_at = raw.get("created_at")
        if not isinstance(created_at, str):
            created_at = "?"
        description = raw.get("description") or ""
        if not isinstance(description, str):
            description = ""
        repos_raw = raw.get("repos") or []
        if not isinstance(repos_raw, list):
            raise FleetError(
                f"Malformed task.json in {workspace}: 'repos' must be a list."
            )
        repos = [RepoEntry.from_dict(r, manifest_path) for r in repos_raw]
        return cls(
            name=name,
            branch=branch,
            created_at=created_at,
            description=description,
            repos=repos,
            version=MANIFEST_VERSION,
        )

    @classmethod
    def try_load(cls, workspace: Path) -> Manifest | None:
        """Like :meth:`load` but returns ``None`` on failure (no message).

        Used by ``task list`` where we want to render an "unparseable"
        marker for one bad manifest rather than abort the whole listing.
        """
        try:
            return cls.load(workspace)
        except FleetError:
            return None

    def save(self, workspace: Path) -> None:
        """Atomically write the manifest to ``<workspace>/task.json``.

        Raises a user-actionable :class:`FleetError` (not a raw ``OSError``)
        when the destination can't be replaced — most commonly because the
        file is open/locked (Windows holds a share lock while VS Code has
        ``task.json`` open). The temp file is always cleaned up.
        """
        manifest_path = workspace / MANIFEST_FILENAME
        payload = {
            "version": MANIFEST_VERSION,
            "name": self.name,
            "branch": self.branch,
            "created_at": self.created_at,
            "description": self.description,
            "repos": [r.to_dict() for r in self.repos],
        }
        tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8",
            )
            os.replace(tmp, manifest_path)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise FleetError(
                f"Failed to write {manifest_path}: {e}. "
                f"If the file is open elsewhere (VS Code?), close it and retry."
            ) from e

    # ------------------------------ helpers ----------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        # Keep file paths as strings in serialised form.
        d["repos"] = [r.to_dict() for r in self.repos]
        return d

    # ------------------------------ mutations --------------------------------
    # All mutations are in-memory; callers invoke ``save()`` to persist
    # atomically. Keeping I/O in one place preserves the tmp+os.replace
    # invariant.

    def add_repos(self, entries: list[RepoEntry]) -> None:
        """Append ``entries`` to ``self.repos``. Caller must ``save()``."""
        self.repos.extend(entries)

    def remove_repos(self, names: set[str]) -> list[RepoEntry]:
        """Drop and return entries whose ``name`` is in ``names``."""
        removed: list[RepoEntry] = []
        kept: list[RepoEntry] = []
        for r in self.repos:
            if r.name in names:
                removed.append(r)
            else:
                kept.append(r)
        self.repos = kept
        return removed

    def rename(self, new_name: str, new_branch: str,
               new_workspace: Path) -> None:
        """Repoint name/branch and every ``worktree_path`` under ``new_workspace``."""
        self.name = new_name
        self.branch = new_branch
        self.repos = [
            RepoEntry(
                name=r.name,
                group=r.group,
                canonical_path=r.canonical_path,
                worktree_path=new_workspace / r.name,
            )
            for r in self.repos
        ]


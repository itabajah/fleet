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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fleet.errors import FleetError

MANIFEST_FILENAME = "task.json"


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
        return cls(
            name=name,
            group=group or None,
            canonical_path=Path(canonical),
            worktree_path=Path(worktree),
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
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise FleetError(
                f"Malformed task.json in {workspace}: {e}"
            ) from e
        if not isinstance(raw, dict):
            raise FleetError(
                f"Malformed task.json in {workspace}: top-level value is "
                f"not an object."
            )
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
        """Atomically write the manifest to ``<workspace>/task.json``."""
        manifest_path = workspace / MANIFEST_FILENAME
        payload = {
            "name": self.name,
            "branch": self.branch,
            "created_at": self.created_at,
            "description": self.description,
            "repos": [r.to_dict() for r in self.repos],
        }
        tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8",
        )
        try:
            os.replace(tmp, manifest_path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    # ------------------------------ helpers ----------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        # Keep file paths as strings in serialised form.
        d["repos"] = [r.to_dict() for r in self.repos]
        return d

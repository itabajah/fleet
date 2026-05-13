"""Active-fleet state plus derived paths (registry, tasks, archive).

``cli.main()`` resolves ``--fleet`` / ``-F`` against the global fleets
config and calls :func:`set_active_fleet` once before any command handler
runs. Every command path then reads the fleet root via
:func:`find_repos_root` and per-fleet task locations via :func:`tasks_root`
/ :func:`archive_root`.

State is module-global because the CLI is one-shot per invocation. Tests
must call :func:`reset_state` between cases (the autouse fixture does this).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fleet.errors import FleetError
from fleet.paths import REGISTRY_FILENAME, tasks_root_base

_repos_root: Path | None = None
_active_fleet_name: str | None = None


def reset_state() -> None:
    """Wipe pinned active-fleet state. Used by tests; harmless in CLI runs."""
    global _repos_root, _active_fleet_name
    _repos_root = None
    _active_fleet_name = None


def set_active_fleet(name: str, root: Path) -> None:
    """Pin the active fleet for the rest of this process.

    Raises :class:`FleetError` if the recorded root no longer exists on disk
    (deleted/renamed since ``fleet fleets add``).
    """
    global _repos_root, _active_fleet_name
    if not root.is_dir():
        raise FleetError(
            f"Fleet '{name}' root no longer exists at {root}.\n"
            f"  Re-add with: fleet fleets add {name} --root <new-path> --force"
        )
    _repos_root = root.resolve()
    _active_fleet_name = name


def active_fleet_name() -> str | None:
    """Name of the active fleet, or None if no fleet has been pinned yet."""
    return _active_fleet_name


def require_active_fleet() -> str:
    """Return the active fleet name or raise :class:`FleetError`.

    Used by code paths (notably task commands) where a missing fleet would
    indicate an internal CLI bug rather than user error.
    """
    if _active_fleet_name is None:
        raise FleetError(
            "No active fleet. This is an internal error — the CLI should "
            "have set one before reaching this code."
        )
    return _active_fleet_name


def find_repos_root() -> Path:
    """Repos root for the active fleet.

    Resolution order:
      1. :func:`set_active_fleet` was called — use that root.
      2. ``FLEET_REPOS_ROOT`` env var (escape hatch for tests / scripts).
      3. Raise :class:`FleetError` — no implicit fallback.
    """
    if _repos_root is not None:
        return _repos_root
    env_override = os.environ.get("FLEET_REPOS_ROOT")
    if env_override:
        p = Path(env_override).resolve()
        if p.is_dir():
            return p
        raise FleetError(
            f"FLEET_REPOS_ROOT points at a non-existent directory: {p}"
        )
    raise FleetError(
        "No active fleet. This is an internal error — the CLI should have "
        "resolved one before reaching this code path."
    )


def registry_path() -> Path:
    """Path to the active fleet's ``fleet.json``."""
    return find_repos_root() / REGISTRY_FILENAME


def load_registry() -> dict:
    """Load and return the registry as a dict, or {} if it doesn't exist."""
    path = registry_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FleetError(f"Malformed registry at {path}: {e}") from e


def sync_root() -> Path:
    """Resolve the registry's ``root`` field (relative to repos root)."""
    reg = load_registry()
    root = reg.get("root") if isinstance(reg, dict) else None
    base = find_repos_root()
    if not root or root == ".":
        return base
    return (base / root).resolve()


def tasks_root() -> Path:
    """Per-fleet task directory: ``<TASKS_ROOT>/<fleet>``."""
    return tasks_root_base() / require_active_fleet()


def archive_root() -> Path:
    """Per-fleet archive directory: ``<TASKS_ROOT>/<fleet>/_archive``."""
    return tasks_root() / "_archive"

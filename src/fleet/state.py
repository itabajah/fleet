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
from dataclasses import dataclass
from pathlib import Path

from fleet.errors import FleetError
from fleet.paths import BUNDLES_FILENAME, REGISTRY_FILENAME, tasks_root_base


@dataclass(frozen=True)
class BranchConfig:
    """How fleet renders the git branch name for a *new* task.

    ``prefix`` is the leading ref segment; ``scoped`` controls whether the
    active fleet name is inserted as a middle segment. The defaults reproduce
    the historical ``task/<fleet>/<task>`` format, so an absent ``branch`` key
    in ``fleets.json`` changes nothing.
    """

    prefix: str = "task"
    scoped: bool = True


_repos_root: Path | None = None
_active_fleet_name: str | None = None
_branch_config: BranchConfig | None = None
_registry_cache: dict | None = None
_registry_cache_path: Path | None = None


def reset_state() -> None:
    """Wipe pinned active-fleet state. Used by tests; harmless in CLI runs."""
    global _repos_root, _active_fleet_name, _branch_config
    global _registry_cache, _registry_cache_path
    _repos_root = None
    _active_fleet_name = None
    _branch_config = None
    _registry_cache = None
    _registry_cache_path = None


def set_active_fleet(name: str, root: Path,
                     branch: BranchConfig | None = None) -> None:
    """Pin the active fleet for the rest of this process.

    ``branch`` is the global branch-naming convention loaded from
    ``fleets.json``; ``None`` selects the built-in default
    (``task/<fleet>/<task>``). It is pinned here so
    :func:`fleet.tasks.validation.task_branch` can render new branches
    without re-reading the config.

    Raises :class:`FleetError` if the recorded root no longer exists on disk
    (deleted/renamed since ``fleet fleets add``).
    """
    global _repos_root, _active_fleet_name, _branch_config
    if not root.is_dir():
        raise FleetError(
            f"Fleet '{name}' root no longer exists at {root}.\n"
            f"  Re-add with: fleet fleets add {name} --root <new-path> --force"
        )
    _repos_root = root.resolve()
    _active_fleet_name = name
    _branch_config = branch


def active_fleet_name() -> str | None:
    """Name of the active fleet, or None if no fleet has been pinned yet."""
    return _active_fleet_name


def branch_config() -> BranchConfig:
    """Active branch-naming convention, or the built-in default if unset."""
    return _branch_config if _branch_config is not None else BranchConfig()


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


def bundles_path() -> Path:
    """Path to the active fleet's ``bundles.json``."""
    return find_repos_root() / BUNDLES_FILENAME


def load_registry() -> dict:
    """Load and return the registry as a dict, or {} if it doesn't exist.

    Cached per invocation (keyed by path) so a command that consults the
    registry several times — discovery, then validation, then save — reads
    ``fleet.json`` from disk once. The cache is cleared by
    :func:`reset_state` (tests) and whenever the active fleet changes.
    """
    global _registry_cache, _registry_cache_path
    path = registry_path()
    if _registry_cache is not None and _registry_cache_path == path:
        return _registry_cache
    if not path.is_file():
        _registry_cache = {}
        _registry_cache_path = path
        return _registry_cache
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise FleetError(f"Malformed registry at {path}: {e}") from e
    _registry_cache = data
    _registry_cache_path = path
    return data


def invalidate_registry_cache() -> None:
    """Drop the cached registry so the next :func:`load_registry` re-reads disk.

    Called after ``fleet scan`` rewrites ``fleet.json`` within the same
    process (tests, or any future in-process re-scan).
    """
    global _registry_cache, _registry_cache_path
    _registry_cache = None
    _registry_cache_path = None


def sync_root() -> Path:
    """Resolve the registry's ``root`` field (relative to repos root).

    Raises :class:`FleetError` if the resolved path doesn't exist, so a
    stale ``root`` surfaces a clear message here rather than a confusing
    "no repos found" deep inside sync/discovery.
    """
    reg = load_registry()
    root = reg.get("root") if isinstance(reg, dict) else None
    base = find_repos_root()
    if not root or root == ".":
        return base
    resolved = (base / root).resolve()
    if not resolved.is_dir():
        raise FleetError(
            f"The registry 'root' ({root!r}) resolves to {resolved}, which "
            f"does not exist. Edit {registry_path()} or re-run `fleet scan`."
        )
    return resolved


def tasks_root() -> Path:
    """Per-fleet task directory: ``<TASKS_ROOT>/<fleet>``."""
    return tasks_root_base() / require_active_fleet()


def archive_root() -> Path:
    """Per-fleet archive directory: ``<TASKS_ROOT>/<fleet>/_archive``."""
    return tasks_root() / "_archive"

"""Filesystem paths and disk-scan constants used across the package.

Pure-data module: no mutable state, no I/O at import time. The active-fleet
state (``set_active_fleet`` / ``find_repos_root``) lives in :mod:`fleet.state`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REGISTRY_FILENAME = "fleet.json"
BUNDLES_FILENAME = "bundles.json"

# Directory names skipped during disk walks at every level. Restricted to
# names that are unambiguously not (and never contain) a git checkout.
PRUNE_DIRS = frozenset({
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
})

# Directory names skipped only when encountered INSIDE an already-discovered
# git repo. These are common build-output folders — a user may legitimately
# group repos under a top-level ``target/`` or ``build/`` directory, but
# inside a repo they're guaranteed to be artifact dumps and walking them
# wastes time on huge trees (Rust ``target/``, Java ``build/``, etc.).
IN_REPO_PRUNE_DIRS = frozenset({
    ".next",
    ".turbo",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
})

# Top-level keys in fleet.json that aren't folder nodes.
ROOT_META_KEYS = frozenset({"root"})


def _default_tasks_root() -> Path:
    """Per-platform default for the tasks root.

    Override at any time with the ``FLEET_TASKS_ROOT`` environment variable
    (consumed by ``tasks_root()`` in :mod:`fleet.state`).

    On Windows we default to ``%LOCALAPPDATA%\\fleet-tasks`` rather than a
    drive root like ``C:\\Tasks``: the latter requires write access to the
    drive root, which many locked-down/corp-managed accounts don't have.
    LOCALAPPDATA is always user-writable. Falls back to
    ``~/AppData/Local/fleet-tasks`` in the rare case the env var is unset.
    """
    # Read sys.platform via getattr so mypy doesn't conclude one branch is
    # unreachable based on the platform it's checking against.
    if getattr(sys, "platform", "") == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "fleet-tasks"
        return Path.home() / "AppData" / "Local" / "fleet-tasks"
    return Path.home() / "fleet-tasks"


def tasks_root_base() -> Path:
    """Resolve the parent of all per-fleet task directories.

    Reads ``FLEET_TASKS_ROOT`` on every call so tests can flip it via
    ``monkeypatch.setenv`` without the package needing reimport.
    """
    override = os.environ.get("FLEET_TASKS_ROOT")
    if override:
        return Path(override)
    return _default_tasks_root()

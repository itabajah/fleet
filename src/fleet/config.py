"""Configuration: paths, errors, and color helpers shared by all fleet modules."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REGISTRY_FILENAME = "fleet.json"


def _default_tasks_root() -> Path:
    """Pick a reasonable per-platform default for the tasks root.

    Override at any time with the ``FLEET_TASKS_ROOT`` environment variable.
    """
    if sys.platform == "win32":
        return Path(r"C:\Tasks")
    return Path.home() / "fleet-tasks"


TASKS_ROOT = Path(os.environ.get("FLEET_TASKS_ROOT") or _default_tasks_root())

# Top-level keys in fleet.json that aren't folder nodes.
ROOT_META_KEYS = frozenset({"root"})

# Subtree under REPOS_ROOT that holds fleet itself; never offered as a
# task target and never synced even if the user re-enables ..me later.
TOOL_HOME_DIRNAME = "..me"


class FleetError(Exception):
    """User-visible error. Carries a process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Active-fleet state. ``cli.main()`` resolves the --fleet flag against
# fleets_config and calls ``set_active_fleet(name, root)`` BEFORE any
# command handler runs. Everything downstream reads through these accessors.
# ---------------------------------------------------------------------------

_repos_root: Path | None = None
_active_fleet_name: str | None = None


def set_active_fleet(name: str, root: Path) -> None:
    """Pin the active fleet for the rest of this process.

    Called by ``cli.main()`` after resolving ``--fleet``/``-F`` against the
    global fleets config. All further calls to ``find_repos_root()`` /
    ``active_fleet_name()`` return these values.
    """
    global _repos_root, _active_fleet_name
    _repos_root = root.resolve()
    _active_fleet_name = name


def active_fleet_name() -> str | None:
    """Name of the active fleet, or None if no fleet has been pinned yet."""
    return _active_fleet_name


def find_repos_root() -> Path:
    """Repos root for the active fleet.

    Resolution order:
      1. ``set_active_fleet`` was called — use that root.
      2. ``FLEET_REPOS_ROOT`` env var (escape hatch for tests / scripts).
      3. Raise ``FleetError`` — no implicit fallback.
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
        raise FleetError(f"Malformed registry at {path}: {e}")


def sync_root() -> Path:
    """Resolve the configured `root` field (relative to repos root)."""
    reg = load_registry()
    root = reg.get("root") if isinstance(reg, dict) else None
    base = find_repos_root()
    if not root or root == ".":
        return base
    return (base / root).resolve()


# ---------------------------------------------------------------------------
# Per-fleet task workspace paths.
#
# Tasks are namespaced by fleet so two fleets can have a task named the same
# thing without collision: ``<TASKS_ROOT>/<fleet-name>/<task-name>/``.
# Archives go under the same per-fleet directory:
# ``<TASKS_ROOT>/<fleet>/_archive/``. ``TASKS_ROOT`` itself defaults to
# ``C:\Tasks`` on Windows and ``~/fleet-tasks`` elsewhere; override with the
# ``FLEET_TASKS_ROOT`` environment variable.
# ---------------------------------------------------------------------------

def _require_active_fleet() -> str:
    """Return the active fleet name or raise FleetError.

    The CLI sets the active fleet in ``cli.main()`` before any task command
    runs, so this raise should never fire in normal use.
    """
    if _active_fleet_name is None:
        raise FleetError(
            "No active fleet. This is an internal error — the CLI should "
            "have set one before reaching task code."
        )
    return _active_fleet_name


def tasks_root() -> Path:
    """Path to the active fleet's task directory: ``TASKS_ROOT / <fleet>``."""
    return TASKS_ROOT / _require_active_fleet()


def archive_root() -> Path:
    """Path to the active fleet's archive directory: ``tasks_root() / _archive``."""
    return tasks_root() / "_archive"


# ---------------------------------------------------------------------------
# Tiny color helper — emits ANSI when stdout is a TTY, otherwise plain text.
# Windows 10+ consoles (Windows Terminal, modern conhost) handle ANSI fine.
# ---------------------------------------------------------------------------

_USE_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)


def _c(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def red(s: str) -> str: return _c(s, "31")
def green(s: str) -> str: return _c(s, "32")
def yellow(s: str) -> str: return _c(s, "33")
def magenta(s: str) -> str: return _c(s, "35")
def cyan(s: str) -> str: return _c(s, "36")
def gray(s: str) -> str: return _c(s, "90")
def dim(s: str) -> str: return _c(s, "2")

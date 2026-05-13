"""Multi-fleet support: maps fleet names to repos roots.

Stored at ``%LOCALAPPDATA%\\fleet\\fleets.json`` on Windows or
``$XDG_CONFIG_HOME/fleet/fleets.json`` (default ``~/.config/fleet/fleets.json``)
elsewhere. Override the location with the ``FLEET_CONFIG_PATH`` env var.
Layout::

    {
      "default": "main",
      "fleets": {
        "main": {"root": "C:\\\\Users\\\\me\\\\Repos"},
        "work": {"root": "C:\\\\work"}
      }
    }

The first fleet added becomes the default automatically. Removing the
current default falls back to the alphabetically-first remaining fleet, or
``None`` if none remain.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from fleet.errors import FleetError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$")

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05


@contextlib.contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    """Cross-process advisory lock around the fleets config.

    Uses an exclusive-create sidecar (``<path>.lock``) so concurrent
    ``fleet fleets add`` invocations serialize their read-modify-write
    rather than losing updates. Best-effort: gives up after
    ``_LOCK_TIMEOUT_SECONDS`` and proceeds anyway, so a stale lock from a
    crashed process never permanently wedges the CLI.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Stale lock fallback: proceed without locking. Two writers
                # can still collide here, but better than hanging forever.
                fd = None
                break
            time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


def _config_file() -> Path:
    """Resolve the path to ``fleets.json``.

    Resolution order:
      1. ``FLEET_CONFIG_PATH`` env var (explicit override).
      2. On Windows: ``%LOCALAPPDATA%\\fleet\\fleets.json``.
      3. On other platforms: ``$XDG_CONFIG_HOME/fleet/fleets.json``,
         falling back to ``~/.config/fleet/fleets.json``.
    """
    override = os.environ.get("FLEET_CONFIG_PATH")
    if override:
        return Path(override).resolve()
    # Read sys.platform via getattr so mypy doesn't decide one branch is
    # unreachable based on the platform it's currently type-checking on.
    if getattr(sys, "platform", "") == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "fleet" / "fleets.json"
        # Fallback when LOCALAPPDATA isn't set (rare; exotic environments).
        return Path.home() / "AppData" / "Local" / "fleet" / "fleets.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "fleet" / "fleets.json"
    return Path.home() / ".config" / "fleet" / "fleets.json"


@dataclass
class FleetEntry:
    name: str
    root: Path


@dataclass
class FleetsConfig:
    default: str | None = None
    fleets: dict[str, FleetEntry] = field(default_factory=dict)

    # ------------------------------ load / save ------------------------------

    @classmethod
    def load(cls) -> FleetsConfig:
        path = _config_file()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise FleetError(f"Malformed fleets config at {path}: {e}") from e
            fleets: dict[str, FleetEntry] = {}
            raw = data.get("fleets") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                for name, entry in raw.items():
                    if (isinstance(entry, dict)
                            and isinstance(entry.get("root"), str)):
                        fleets[name] = FleetEntry(name=name,
                                                  root=Path(entry["root"]))
            default = data.get("default") if isinstance(data, dict) else None
            if not isinstance(default, str) or default not in fleets:
                default = None
            return cls(default=default, fleets=fleets)
        return cls(default=None, fleets={})

    def save(self) -> None:
        path = _config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "default": self.default,
            "fleets": {
                name: {"root": str(entry.root)}
                for name, entry in sorted(self.fleets.items())
            },
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    # ------------------------------ mutators ---------------------------------

    def add(self, name: str, root: Path, force: bool = False) -> None:
        _validate_fleet_name(name)
        if name in self.fleets and not force:
            raise FleetError(
                f"Fleet '{name}' already registered "
                f"(root: {self.fleets[name].root}). Use --force to overwrite."
            )
        root = root.resolve()
        if not root.is_dir():
            raise FleetError(
                f"Root path does not exist or is not a directory: {root}"
            )
        self.fleets[name] = FleetEntry(name=name, root=root)
        if self.default is None:
            self.default = name

    def remove(self, name: str) -> None:
        if name not in self.fleets:
            raise FleetError(f"No such fleet: '{name}'")
        del self.fleets[name]
        if self.default == name:
            self.default = sorted(self.fleets)[0] if self.fleets else None

    def set_default(self, name: str) -> None:
        if name not in self.fleets:
            raise FleetError(
                f"No such fleet: '{name}'. "
                f"Known fleets: {', '.join(sorted(self.fleets)) or '(none)'}"
            )
        self.default = name

    # ------------------------------ resolution -------------------------------

    def resolve(self, override: str | None) -> FleetEntry:
        """Return the active fleet entry. ``override`` wins over ``default``."""
        if override is not None:
            entry = self.fleets.get(override)
            if entry is None:
                raise FleetError(
                    f"No such fleet: '{override}'. "
                    "Run `fleet fleets list` to see configured fleets."
                )
            return entry
        if not self.fleets:
            raise FleetError(
                "No fleets configured. Add one with: "
                "`fleet fleets add <name> [--root <path>]`"
            )
        if self.default is None or self.default not in self.fleets:
            raise FleetError(
                "No default fleet set. Run `fleet fleets default <name>` "
                f"(known fleets: {', '.join(sorted(self.fleets))})."
            )
        return self.fleets[self.default]


def _validate_fleet_name(name: str) -> None:
    """Reject fleet names that aren't safe to use as a git ref segment.

    The name appears in ``task/<fleet>/<task>`` git refs, so it must satisfy
    git's ref-format rules in addition to our character whitelist.
    """
    if not _NAME_RE.match(name):
        raise FleetError(
            f"Invalid fleet name '{name}'. Must start with a letter/digit "
            "and contain only A-Z, a-z, 0-9, '.', '_', '-' (max 32 chars)."
        )
    if (".." in name
            or name.startswith(".") or name.endswith(".")
            or name.endswith(".lock") or "@{" in name):
        raise FleetError(
            f"Invalid fleet name '{name}': would produce an invalid git "
            "ref (no '..', no leading/trailing '.', no '.lock' suffix, "
            "no '@{')."
        )


def config_path() -> Path:
    """Public helper for diagnostics."""
    return _config_file()

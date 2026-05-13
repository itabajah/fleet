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

The first fleet added becomes the default automatically. Removing the current
default falls back to the alphabetically-first remaining fleet, or ``None``
if none remain.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from fleet.config import FleetError

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$")


def _config_file() -> Path:
    """Resolve the path to fleets.json.

    Resolution order:
      1. ``FLEET_CONFIG_PATH`` env var (explicit override; absolute path)
      2. ``%LOCALAPPDATA%\\fleet\\fleets.json`` on Windows
      3. ``$XDG_CONFIG_HOME/fleet/fleets.json`` on Linux/macOS
      4. ``~/.config/fleet/fleets.json`` (XDG default)
    """
    override = os.environ.get("FLEET_CONFIG_PATH")
    if override:
        return Path(override).resolve()
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "fleet" / "fleets.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "fleet" / "fleets.json"
    return Path.home() / ".config" / "fleet" / "fleets.json"


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------

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
    def load(cls) -> "FleetsConfig":
        path = _config_file()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise FleetError(f"Malformed fleets config at {path}: {e}")
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
        try:
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ------------------------------ mutators ---------------------------------

    def add(self, name: str, root: Path, force: bool = False) -> None:
        if not _NAME_RE.match(name):
            raise FleetError(
                f"Invalid fleet name '{name}'. Must start with a letter/digit "
                "and contain only A-Z, a-z, 0-9, '.', '_', '-' (max 32 chars)."
            )
        # The name appears as a component of `task/<fleet>/<task>` git refs.
        # Rule out the same git-ref edge cases _validate_task_name covers.
        if (".." in name
                or name.startswith(".") or name.endswith(".")
                or name.endswith(".lock") or "@{" in name):
            raise FleetError(
                f"Invalid fleet name '{name}': would produce an invalid git "
                "ref (no '..', no leading/trailing '.', no '.lock' suffix, "
                "no '@{')."
            )
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
        """Return the active fleet entry. `override` wins over `default`."""
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


def config_path() -> Path:
    """Public helper for diagnostics."""
    return _config_file()

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

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fleet.errors import FleetError
from fleet.jsonstore import read_json, write_json_atomic
from fleet.refnames import has_ref_format_violation, validate_branch
from fleet.state import BranchConfig

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$")


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
    branch: BranchConfig = field(default_factory=BranchConfig)

    # ------------------------------ load / save ------------------------------

    @classmethod
    def load(cls) -> FleetsConfig:
        path = _config_file()
        if path.is_file():
            data = read_json(path, what=f"fleets config at {path}")
            fleets: dict[str, FleetEntry] = {}
            raw = data.get("fleets") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                for name, entry in raw.items():
                    if (isinstance(entry, dict)
                            and isinstance(entry.get("root"), str)):
                        # Resolve on load so a hand-edited relative root
                        # (e.g. "../repos") doesn't resolve differently
                        # depending on the CLI's current directory.
                        fleets[name] = FleetEntry(
                            name=name,
                            root=Path(entry["root"]).expanduser().resolve(),
                        )
            default = data.get("default") if isinstance(data, dict) else None
            if not isinstance(default, str) or default not in fleets:
                default = None
            branch = _parse_branch_config(data, path)
            return cls(default=default, fleets=fleets, branch=branch)
        return cls(default=None, fleets={})

    def save(self) -> None:
        path = _config_file()
        data: dict[str, object] = {
            "default": self.default,
            "fleets": {
                name: {"root": str(entry.root)}
                for name, entry in sorted(self.fleets.items())
            },
        }
        # Only persist a ``branch`` key when it diverges from the built-in
        # default, so a default config stays byte-for-byte what it was before
        # this setting existed.
        if self.branch != BranchConfig():
            data["branch"] = {
                "prefix": self.branch.prefix,
                "scoped": self.branch.scoped,
            }
        write_json_atomic(path, data)

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

    def remove(self, name: str) -> bool:
        """Remove fleet ``name``. Returns True if it was the active default.

        When the default is removed we deliberately do NOT auto-pick a
        replacement: silently promoting the alphabetically-first remaining
        fleet risks pointing later commands at the wrong repos. The default
        is cleared and the caller prompts the user to choose explicitly.
        """
        if name not in self.fleets:
            raise FleetError(f"No such fleet: '{name}'")
        del self.fleets[name]
        was_default = self.default == name
        if was_default:
            self.default = None
        return was_default

    def set_default(self, name: str) -> None:
        if name not in self.fleets:
            raise FleetError(
                f"No such fleet: '{name}'. "
                f"Known fleets: {', '.join(sorted(self.fleets)) or '(none)'}"
            )
        self.default = name

    def rename(self, old: str, new: str) -> None:
        """Rename fleet ``old`` to ``new``, preserving its root and default-ness.

        Does not touch anything on disk under the fleet root (``fleet.json``,
        tasks, branches): only the registry mapping changes. Existing task
        branches keep whatever name is recorded in their ``task.json`` (under
        the default scoped convention that's ``task/<old>/...``) — the command
        handler tells the user as much, tailored to the active convention.
        """
        if old not in self.fleets:
            raise FleetError(
                f"No such fleet: '{old}'. "
                f"Known fleets: {', '.join(sorted(self.fleets)) or '(none)'}"
            )
        if old == new:
            return
        _validate_fleet_name(new)
        if new in self.fleets:
            raise FleetError(
                f"Fleet '{new}' already exists (root: {self.fleets[new].root}). "
                f"Pick a different name or remove it first."
            )
        entry = self.fleets.pop(old)
        self.fleets[new] = FleetEntry(name=new, root=entry.root)
        if self.default == old:
            self.default = new

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

    Under the default (scoped) convention the name appears as a middle
    segment of ``<prefix>/<fleet>/<task>`` git refs, so it must satisfy
    git's ref-format rules in addition to our character whitelist. We enforce
    this unconditionally — even when ``scoped`` is off — so toggling the
    convention can never retroactively invalidate an existing fleet name.
    """
    if not _NAME_RE.match(name):
        raise FleetError(
            f"Invalid fleet name '{name}'. Must start with a letter/digit "
            "and contain only A-Z, a-z, 0-9, '.', '_', '-' (max 32 chars)."
        )
    if has_ref_format_violation(name):
        raise FleetError(
            f"Invalid fleet name '{name}': would produce an invalid git "
            "ref (no '..', no leading/trailing '.', no '.lock' suffix, "
            "no '@{')."
        )


def _parse_branch_config(data: object, path: Path) -> BranchConfig:
    """Parse + validate the optional ``branch`` key from ``fleets.json``.

    An absent key yields the default convention. When present it must be an
    object with an optional non-empty string ``prefix`` (default ``"task"``)
    and optional bool ``scoped`` (default ``True``). The *rendered* branch is
    checked against the same git-ref-safety rules the rest of the CLI uses, so
    a typo'd or hostile prefix fails loudly here rather than deep inside a
    later ``git`` invocation.
    """
    if not isinstance(data, dict) or "branch" not in data:
        return BranchConfig()
    raw = data["branch"]
    if not isinstance(raw, dict):
        raise FleetError(
            f"Malformed fleets config at {path}: 'branch' must be an object "
            f"with optional 'prefix' and 'scoped' keys."
        )
    prefix = raw.get("prefix", "task")
    if not isinstance(prefix, str) or not prefix:
        raise FleetError(
            f"Malformed fleets config at {path}: branch 'prefix' must be a "
            f"non-empty string (got {prefix!r})."
        )
    scoped = raw.get("scoped", True)
    if not isinstance(scoped, bool):
        raise FleetError(
            f"Malformed fleets config at {path}: branch 'scoped' must be a "
            f"boolean (got {scoped!r})."
        )
    cfg = BranchConfig(prefix=prefix, scoped=scoped)
    _validate_branch_config(cfg, path)
    return cfg


def _validate_branch_config(cfg: BranchConfig, path: Path) -> None:
    """Ensure a branch rendered from ``cfg`` passes git-ref-safety checks."""
    # Representative segments (each independently valid) so any failure is
    # attributable to ``prefix`` / ``scoped``, not to the sample fleet/task.
    sample = (f"{cfg.prefix}/fleet/task" if cfg.scoped
              else f"{cfg.prefix}/task")
    try:
        validate_branch(sample, context="rendered task branch")
    except FleetError as e:
        raise FleetError(
            f"Invalid branch convention in fleets config at {path}: {e} "
            f"(prefix={cfg.prefix!r}, scoped={cfg.scoped})."
        ) from e


def config_path() -> Path:
    """Public helper for diagnostics."""
    return _config_file()

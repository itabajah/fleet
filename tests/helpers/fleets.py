"""Helpers for setting up named fleets in tests."""

from __future__ import annotations

from pathlib import Path

from fleet.fleets_config import FleetsConfig
from fleet.state import set_active_fleet


def make_fleet(name: str, root: Path, *, default: bool = True) -> FleetsConfig:
    """Register ``name`` -> ``root`` in the (sandboxed) fleets config.

    Returns the persisted :class:`FleetsConfig` so tests can assert on it
    directly.
    """
    cfg = FleetsConfig.load()
    cfg.add(name, root, force=True)
    if default:
        cfg.set_default(name)
    cfg.save()
    return cfg


def activate(name: str, root: Path) -> None:
    """Pin ``name`` -> ``root`` as the active fleet for the current process."""
    set_active_fleet(name, root)

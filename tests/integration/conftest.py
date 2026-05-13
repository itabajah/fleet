"""Integration-test fixtures: bare remotes + populated fleets."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fleet.fleets_config import FleetsConfig
from fleet.scan import cmd_scan
from fleet.state import set_active_fleet
from helpers.git import clone_into, init_bare_remote


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001
    """Mark every integration test with the ``integration`` marker."""
    for item in items:
        item.add_marker(pytest.mark.integration)


@dataclass
class PopulatedFleet:
    """A fleet ready for command-level tests.

    ``repos`` maps display-name (``group/sub/name`` or just ``name``) to
    the cloned repo's path on disk.
    """
    name: str
    root: Path
    repos: dict[str, Path] = field(default_factory=dict)


@pytest.fixture
def bare_remote_factory(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory creating uniquely-named bare remotes under tmp_path."""
    counter = {"n": 0}

    def _make(name: str | None = None, *, default_branch: str = "main") -> Path:
        counter["n"] += 1
        n = counter["n"]
        path = tmp_path / "_remotes" / f"{name or 'remote'}-{n}.git"
        return init_bare_remote(path, default_branch=default_branch)

    return _make


@pytest.fixture
def cloned_repo_factory(bare_remote_factory: Callable[..., Path]) -> Callable[..., Path]:
    """Return a factory that creates a bare remote and clones it into a target dir."""

    def _make(dest: Path, *, default_branch: str = "main") -> Path:
        remote = bare_remote_factory(name=dest.name, default_branch=default_branch)
        return clone_into(remote, dest)

    return _make


@pytest.fixture
def populated_fleet(
    tmp_path: Path,
    cloned_repo_factory: Callable[..., Path],
) -> Iterator[PopulatedFleet]:
    """Build a fleet with three real cloned repos and run an initial scan."""
    root = tmp_path / "repos"
    root.mkdir()

    repos: dict[str, Path] = {}
    repos["alpha"] = cloned_repo_factory(root / "alpha")
    repos["beta"] = cloned_repo_factory(root / "beta")
    repos["group/gamma"] = cloned_repo_factory(root / "group" / "gamma")

    # Register + activate the fleet.
    cfg = FleetsConfig.load()
    cfg.add("demo", root, force=True)
    cfg.set_default("demo")
    cfg.save()
    set_active_fleet("demo", root)

    # Initial scan to populate fleet.json (so repos_to_sync sees them).
    import argparse
    cmd_scan(argparse.Namespace())

    yield PopulatedFleet(name="demo", root=root, repos=repos)

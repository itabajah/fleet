"""Shared pytest fixtures.

Two autouse fixtures keep tests isolated from one another and from the
real user environment:

  * :func:`reset_fleet_state` — wipes the active-fleet module globals
    in :mod:`fleet.state` between tests so module state can't leak.
  * :func:`fleet_env_sandbox` — points every ``FLEET_*`` env var (and
    ``LOCALAPPDATA`` / ``XDG_CONFIG_HOME`` / ``HOME``) at a per-test
    temp dir so no test can ever touch the user's real ``fleets.json``,
    real tasks root, or real repos root. Also force-disables ANSI
    colour via ``NO_COLOR=1`` so output assertions don't have to strip
    escape sequences; tests that need colour set ``FORCE_COLOR``
    explicitly.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from fleet import state as fleet_state


@pytest.fixture(autouse=True)
def reset_fleet_state() -> Iterator[None]:
    """Clear pinned active-fleet state between tests."""
    fleet_state.reset_state()
    yield
    fleet_state.reset_state()


@pytest.fixture(autouse=True)
def fleet_env_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Sandbox every fleet-relevant env var to a per-test temp dir.

    The sandbox is a directory; we expose three subdirs via env vars:

      * ``FLEET_CONFIG_PATH``  -> ``<sandbox>/fleets.json``
      * ``FLEET_TASKS_ROOT``   -> ``<sandbox>/tasks``
      * ``FLEET_REPOS_ROOT``   -> unset (most tests pin via
        ``set_active_fleet`` directly; tests that want the env-fallback
        path set it themselves).

    We also point ``LOCALAPPDATA`` (Windows) and ``XDG_CONFIG_HOME`` /
    ``HOME`` (POSIX) at the sandbox so the platform-default branch in
    :func:`fleet.fleets_config._config_file` can't accidentally touch the
    real config either when ``FLEET_CONFIG_PATH`` is explicitly cleared by
    a test.
    """
    sandbox = tmp_path / "_sandbox"
    sandbox.mkdir()
    cfg = sandbox / "fleets.json"
    tasks = sandbox / "tasks"
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("FLEET_TASKS_ROOT", str(tasks))
    monkeypatch.delenv("FLEET_REPOS_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(sandbox / "LocalAppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "xdg"))
    monkeypatch.setenv("HOME", str(sandbox / "home"))
    monkeypatch.setenv("USERPROFILE", str(sandbox / "home"))
    # Color off in tests by default; tests that want color set FORCE_COLOR.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    yield sandbox


# Make tests/helpers importable without an installed-package tax.
sys.path.insert(0, str(Path(__file__).parent))

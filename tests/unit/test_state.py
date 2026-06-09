"""Active-fleet state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet import state
from fleet.errors import FleetError


def test_no_active_fleet_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_REPOS_ROOT", raising=False)
    with pytest.raises(FleetError, match="No active fleet"):
        state.find_repos_root()


def test_set_active_fleet_pins_root(tmp_path: Path) -> None:
    state.set_active_fleet("demo", tmp_path)
    assert state.active_fleet_name() == "demo"
    assert state.find_repos_root() == tmp_path.resolve()


def test_set_active_fleet_rejects_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(FleetError, match="root no longer exists"):
        state.set_active_fleet("demo", missing)


def test_env_repos_root_fallback(monkeypatch: pytest.MonkeyPatch,
                                 tmp_path: Path) -> None:
    monkeypatch.setenv("FLEET_REPOS_ROOT", str(tmp_path))
    assert state.find_repos_root() == tmp_path.resolve()


def test_env_repos_root_missing_dir(monkeypatch: pytest.MonkeyPatch,
                                    tmp_path: Path) -> None:
    monkeypatch.setenv("FLEET_REPOS_ROOT", str(tmp_path / "nope"))
    with pytest.raises(FleetError, match="non-existent"):
        state.find_repos_root()


def test_reset_state_wipes_globals(tmp_path: Path) -> None:
    state.set_active_fleet("demo", tmp_path)
    state.reset_state()
    assert state.active_fleet_name() is None


def test_require_active_fleet_raises_when_unset() -> None:
    with pytest.raises(FleetError):
        state.require_active_fleet()


# ---------------------------------------------------------------------------
# branch config
# ---------------------------------------------------------------------------

def test_branch_config_defaults_when_unset() -> None:
    bc = state.branch_config()
    assert bc == state.BranchConfig()
    assert bc.prefix == "task"
    assert bc.scoped is True


def test_set_active_fleet_pins_branch_config(tmp_path: Path) -> None:
    bc = state.BranchConfig(prefix="wip", scoped=False)
    state.set_active_fleet("demo", tmp_path, bc)
    assert state.branch_config() == bc


def test_set_active_fleet_without_branch_uses_default(tmp_path: Path) -> None:
    state.set_active_fleet("demo", tmp_path)
    assert state.branch_config() == state.BranchConfig()


def test_reset_state_clears_branch_config(tmp_path: Path) -> None:
    state.set_active_fleet("demo", tmp_path,
                           state.BranchConfig(prefix="wip", scoped=False))
    state.reset_state()
    assert state.branch_config() == state.BranchConfig()


def test_tasks_root_per_fleet(tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_TASKS_ROOT", str(tmp_path / "T"))
    state.set_active_fleet("alpha", tmp_path)
    assert state.tasks_root() == tmp_path / "T" / "alpha"
    assert state.archive_root() == tmp_path / "T" / "alpha" / "_archive"


def test_load_registry_returns_empty_when_missing(tmp_path: Path) -> None:
    state.set_active_fleet("demo", tmp_path)
    assert state.load_registry() == {}


def test_load_registry_parses_json(tmp_path: Path) -> None:
    (tmp_path / "fleet.json").write_text('{"root": "."}', encoding="utf-8")
    state.set_active_fleet("demo", tmp_path)
    assert state.load_registry() == {"root": "."}


def test_load_registry_raises_on_malformed(tmp_path: Path) -> None:
    (tmp_path / "fleet.json").write_text("not json {", encoding="utf-8")
    state.set_active_fleet("demo", tmp_path)
    with pytest.raises(FleetError, match="Malformed registry"):
        state.load_registry()


def test_load_registry_strips_utf8_bom(tmp_path: Path) -> None:
    """Editors that save with a UTF-8 BOM must not break registry reads."""
    (tmp_path / "fleet.json").write_bytes(
        b"\xef\xbb\xbf" + b'{"root": "."}'
    )
    state.set_active_fleet("demo", tmp_path)
    assert state.load_registry() == {"root": "."}


def test_sync_root_default_is_repos_root(tmp_path: Path) -> None:
    state.set_active_fleet("demo", tmp_path)
    assert state.sync_root() == tmp_path.resolve()


def test_sync_root_with_explicit_root(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "fleet.json").write_text(
        '{"root": "sub"}', encoding="utf-8",
    )
    state.set_active_fleet("demo", tmp_path)
    assert state.sync_root() == sub.resolve()

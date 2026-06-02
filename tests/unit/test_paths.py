"""Pure-data path constants and per-platform defaults."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fleet import paths


def test_prune_dirs_includes_node_modules() -> None:
    assert "node_modules" in paths.PRUNE_DIRS
    assert ".venv" in paths.PRUNE_DIRS


def test_root_meta_keys_just_root() -> None:
    assert frozenset({"root"}) == paths.ROOT_META_KEYS


def test_tasks_root_base_respects_env(monkeypatch: pytest.MonkeyPatch,
                                      tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("FLEET_TASKS_ROOT", str(target))
    assert paths.tasks_root_base() == target


def test_tasks_root_base_default_windows(monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    monkeypatch.delenv("FLEET_TASKS_ROOT", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.tasks_root_base() == tmp_path / "fleet-tasks"


def test_tasks_root_base_default_windows_no_localappdata(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_TASKS_ROOT", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    expected = Path.home() / "AppData" / "Local" / "fleet-tasks"
    assert paths.tasks_root_base() == expected


def test_tasks_root_base_default_posix(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
    monkeypatch.delenv("FLEET_TASKS_ROOT", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads HOME on POSIX; on Windows we'd need USERPROFILE.
    expected = Path.home() / "fleet-tasks"
    assert paths.tasks_root_base() == expected

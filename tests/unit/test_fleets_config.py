"""FleetsConfig: load/save round-trip, name validation, platform precedence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fleet.errors import FleetError
from fleet.fleets_config import (
    FleetEntry,
    FleetsConfig,
    _config_file,
    _validate_fleet_name,
    config_path,
)
from fleet.state import BranchConfig

# ---------------------------------------------------------------------------
# _config_file precedence
# ---------------------------------------------------------------------------

def test_explicit_env_var_wins(monkeypatch: pytest.MonkeyPatch,
                               tmp_path: Path) -> None:
    target = tmp_path / "custom.json"
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(target))
    assert _config_file() == target.resolve()


def test_windows_uses_localappdata(monkeypatch: pytest.MonkeyPatch,
                                   tmp_path: Path) -> None:
    monkeypatch.delenv("FLEET_CONFIG_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert _config_file() == tmp_path / "AppData" / "fleet" / "fleets.json"


def test_posix_prefers_xdg(monkeypatch: pytest.MonkeyPatch,
                           tmp_path: Path) -> None:
    monkeypatch.delenv("FLEET_CONFIG_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "shouldnt-be-used"))
    assert _config_file() == tmp_path / "xdg" / "fleet" / "fleets.json"


def test_posix_falls_back_to_home(monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path) -> None:
    monkeypatch.delenv("FLEET_CONFIG_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "shouldnt-be-used"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert _config_file() == Path.home() / ".config" / "fleet" / "fleets.json"


def test_config_path_helper_matches() -> None:
    assert config_path() == _config_file()


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", ["main", "Work", "a", "a.b-c_1", "x" * 32])
def test_valid_names(good: str) -> None:
    _validate_fleet_name(good)  # no raise


@pytest.mark.parametrize("bad", [
    "", "_underscore_first", "-dash", ".dot", "weird@name", "with space",
    "x" * 33, "..parent", "trailing.", "trailing.lock", "has@{ref",
])
def test_invalid_names(bad: str) -> None:
    with pytest.raises(FleetError):
        _validate_fleet_name(bad)


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------

def test_first_add_becomes_default(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    assert cfg.default == "alpha"


def test_second_add_does_not_override_default(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.add("beta", tmp_path)
    assert cfg.default == "alpha"


def test_add_rejects_duplicate(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    with pytest.raises(FleetError, match="already registered"):
        cfg.add("alpha", tmp_path)


def test_add_force_overwrites(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.add("alpha", other, force=True)
    assert cfg.fleets["alpha"].root == other.resolve()


def test_add_rejects_missing_root(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    with pytest.raises(FleetError, match="does not exist"):
        cfg.add("alpha", tmp_path / "nope")


def test_remove_unknown_raises(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    with pytest.raises(FleetError, match="No such fleet"):
        cfg.remove("ghost")


def test_remove_default_clears_default(tmp_path: Path) -> None:
    # Removing the active default deliberately clears it (rather than
    # silently promoting another fleet) so the user re-chooses explicitly.
    other = tmp_path / "other"
    other.mkdir()
    cfg = FleetsConfig()
    cfg.add("zeta", tmp_path)
    cfg.add("alpha", other)
    assert cfg.default == "zeta"
    was_default = cfg.remove("zeta")
    assert was_default is True
    assert cfg.default is None
    assert set(cfg.fleets) == {"alpha"}


def test_remove_non_default_keeps_default(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cfg = FleetsConfig()
    cfg.add("zeta", tmp_path)
    cfg.add("alpha", other)
    assert cfg.default == "zeta"
    was_default = cfg.remove("alpha")
    assert was_default is False
    assert cfg.default == "zeta"


def test_remove_last_clears_default(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.remove("alpha")
    assert cfg.default is None


def test_rename_preserves_root_and_default(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    assert cfg.default == "alpha"
    cfg.rename("alpha", "beta")
    assert "alpha" not in cfg.fleets
    assert cfg.fleets["beta"].root == tmp_path.resolve()
    assert cfg.default == "beta"


def test_rename_unknown_raises(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    with pytest.raises(FleetError, match="No such fleet"):
        cfg.rename("ghost", "beta")


def test_rename_onto_existing_raises(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.add("beta", other)
    with pytest.raises(FleetError, match="already exists"):
        cfg.rename("alpha", "beta")


def test_rename_validates_new_name(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    with pytest.raises(FleetError, match="Invalid fleet name"):
        cfg.rename("alpha", "bad name")


def test_load_resolves_relative_roots(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    # A hand-edited relative root must resolve consistently regardless of cwd.
    sub = tmp_path / "repos"
    sub.mkdir()
    cfg_file = tmp_path / "fleets.json"
    cfg_file.write_text(
        '{"default": "main", "fleets": {"main": {"root": "repos"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(cfg_file))
    monkeypatch.chdir(tmp_path)
    cfg = FleetsConfig.load()
    assert cfg.fleets["main"].root == sub.resolve()


def test_set_default_unknown(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    with pytest.raises(FleetError, match="No such fleet"):
        cfg.set_default("ghost")


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------

def test_resolve_override_wins(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.add("beta", other)
    assert cfg.resolve("beta").root == other.resolve()


def test_resolve_unknown_override(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    with pytest.raises(FleetError, match="No such fleet"):
        cfg.resolve("ghost")


def test_resolve_no_fleets() -> None:
    cfg = FleetsConfig()
    with pytest.raises(FleetError, match="No fleets configured"):
        cfg.resolve(None)


def test_resolve_no_default(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.default = None  # simulate broken state
    with pytest.raises(FleetError, match="No default"):
        cfg.resolve(None)


# ---------------------------------------------------------------------------
# load / save round-trip
# ---------------------------------------------------------------------------

def test_save_then_load_preserves(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.save()
    loaded = FleetsConfig.load()
    assert loaded.default == "alpha"
    assert "alpha" in loaded.fleets
    assert loaded.fleets["alpha"].root == tmp_path.resolve()


def test_load_missing_file_returns_empty() -> None:
    cfg = FleetsConfig.load()
    assert cfg.default is None
    assert cfg.fleets == {}


def test_load_malformed_raises(tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json {", encoding="utf-8")
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(bad))
    with pytest.raises(FleetError, match="Malformed fleets"):
        FleetsConfig.load()


def test_load_drops_invalid_default(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "f.json"
    p.write_text(json.dumps({
        "default": "missing-name",
        "fleets": {"alpha": {"root": str(tmp_path)}},
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(p))
    cfg = FleetsConfig.load()
    assert cfg.default is None  # bogus default scrubbed
    assert "alpha" in cfg.fleets


def test_load_skips_malformed_entries(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "f.json"
    p.write_text(json.dumps({
        "default": "alpha",
        "fleets": {
            "alpha": {"root": str(tmp_path)},
            "broken": "not-a-dict",
            "missing-root": {},
        },
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(p))
    cfg = FleetsConfig.load()
    assert set(cfg.fleets) == {"alpha"}


def test_fleet_entry_dataclass(tmp_path: Path) -> None:
    """Sanity: the public dataclass is what callers import."""
    e = FleetEntry(name="x", root=tmp_path)
    assert e.name == "x"
    assert e.root == tmp_path


def test_load_strips_utf8_bom(tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Notepad / some IDEs prepend a UTF-8 BOM on save; ``json.loads``
    doesn't tolerate it, so we read with ``utf-8-sig``."""
    p = tmp_path / "f.json"
    payload = json.dumps({
        "default": "alpha",
        "fleets": {"alpha": {"root": str(tmp_path)}},
    })
    p.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    monkeypatch.setenv("FLEET_CONFIG_PATH", str(p))
    cfg = FleetsConfig.load()
    assert cfg.default == "alpha"


def test_config_lock_falls_back_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """A stale lock from a crashed peer must not wedge the CLI forever:
    after the timeout we proceed without the lock but emit a stderr warning
    so the user can notice and clean up."""
    from fleet import fleets_config

    monkeypatch.setattr(fleets_config, "_LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(fleets_config, "_LOCK_POLL_SECONDS", 0.01)

    target = tmp_path / "fleets.json"
    lock = target.with_suffix(target.suffix + ".lock")
    lock.touch()  # pre-existing stale lock

    with fleets_config._config_lock(target):
        pass

    err = capsys.readouterr().err
    assert "acquired" in err
    assert "without lock" in err


# ---------------------------------------------------------------------------
# branch convention
# ---------------------------------------------------------------------------

def _write_config(text: str) -> None:
    """Write raw fleets.json text to the sandboxed config path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_branch_absent_yields_default() -> None:
    _write_config('{"default": null, "fleets": {}}')
    assert FleetsConfig.load().branch == BranchConfig()


def test_branch_custom_parsed() -> None:
    _write_config('{"fleets": {}, "branch": {"prefix": "wip", "scoped": false}}')
    assert FleetsConfig.load().branch == BranchConfig(prefix="wip", scoped=False)


def test_branch_prefix_only_defaults_scoped() -> None:
    _write_config('{"fleets": {}, "branch": {"prefix": "feature"}}')
    assert FleetsConfig.load().branch == BranchConfig(prefix="feature", scoped=True)


def test_branch_scoped_only_defaults_prefix() -> None:
    _write_config('{"fleets": {}, "branch": {"scoped": false}}')
    assert FleetsConfig.load().branch == BranchConfig(prefix="task", scoped=False)


@pytest.mark.parametrize("bad", [
    '{"branch": []}',
    '{"branch": "task"}',
    '{"branch": {"prefix": ""}}',
    '{"branch": {"prefix": 5}}',
    '{"branch": {"scoped": "yes"}}',
    '{"branch": {"scoped": 1}}',
    '{"branch": {"prefix": "bad prefix"}}',
    '{"branch": {"prefix": "task/"}}',
    '{"branch": {"prefix": "/task"}}',
    '{"branch": {"prefix": "-lead"}}',
])
def test_branch_malformed_raises(bad: str) -> None:
    _write_config(bad)
    with pytest.raises(FleetError):
        FleetsConfig.load()


def test_save_omits_default_branch(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.save()
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert "branch" not in data


def test_save_writes_custom_branch_roundtrip(tmp_path: Path) -> None:
    cfg = FleetsConfig()
    cfg.add("alpha", tmp_path)
    cfg.branch = BranchConfig(prefix="wip", scoped=False)
    cfg.save()
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["branch"] == {"prefix": "wip", "scoped": False}
    assert FleetsConfig.load().branch == BranchConfig(prefix="wip", scoped=False)

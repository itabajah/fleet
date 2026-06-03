"""Unit tests for :mod:`fleet.bundles_config`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.bundles_config import (
    BundlesConfig,
    expand_bundle_tokens,
    validate_bundle_name,
)
from fleet.errors import FleetError
from fleet.state import bundles_path, set_active_fleet

pytestmark = pytest.mark.unit


@pytest.fixture
def active_fleet(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    set_active_fleet("demo", root)
    return root


# ---------------------------------------------------------------------------
# validate_bundle_name
# ---------------------------------------------------------------------------

class TestValidateBundleName:
    @pytest.mark.parametrize("name", ["a", "core", "core-1", "core_v2", "C1.x"])
    def test_accepted(self, name: str) -> None:
        validate_bundle_name(name)

    @pytest.mark.parametrize("name", ["", "-bad", ".bad", "a/b", "@x", "a b",
                                       "a" * 65])
    def test_rejected(self, name: str) -> None:
        with pytest.raises(FleetError):
            validate_bundle_name(name)


# ---------------------------------------------------------------------------
# BundlesConfig CRUD + I/O
# ---------------------------------------------------------------------------

class TestBundlesConfig:
    def test_load_missing_returns_empty(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        assert cfg.bundles == {}

    def test_add_save_load_roundtrip(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("core", ["alpha", "group/gamma"], force=False)
        cfg.save()

        # On-disk schema is { "bundles": { name: [tokens...] } }.
        data = json.loads(bundles_path().read_text(encoding="utf-8"))
        assert data == {"bundles": {"core": ["alpha", "group/gamma"]}}

        loaded = BundlesConfig.load()
        assert loaded.get("core") == ["alpha", "group/gamma"]
        assert loaded.names() == ["core"]

    def test_add_dedupes_and_preserves_order(
        self, active_fleet: Path, capsys
    ) -> None:
        cfg = BundlesConfig.load()
        stored = cfg.add("b", ["a", "b", "a", "c"], force=False)
        assert stored == ["a", "b", "c"]
        assert "ignoring duplicate" in capsys.readouterr().out

    def test_add_rejects_at_sigil(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        with pytest.raises(FleetError, match="cannot start with '@'"):
            cfg.add("b", ["@other"], force=False)

    def test_add_rejects_empty(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        with pytest.raises(FleetError, match="would be empty"):
            cfg.add("b", [], force=False)

    def test_add_dup_without_force(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a"], force=False)
        with pytest.raises(FleetError, match="already exists"):
            cfg.add("b", ["c"], force=False)

    def test_add_force_overwrites(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a"], force=False)
        cfg.add("b", ["c"], force=True)
        assert cfg.get("b") == ["c"]

    def test_remove(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a"], force=False)
        cfg.remove("b")
        assert cfg.names() == []
        with pytest.raises(FleetError, match="No such bundle"):
            cfg.remove("b")

    def test_get_unknown(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        with pytest.raises(FleetError, match="No such bundle"):
            cfg.get("x")

    def test_edit_add_remove(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a", "b", "c"], force=False)
        result = cfg.edit("b", add=["d"], remove=["b"])
        assert result == ["a", "c", "d"]
        assert cfg.get("b") == ["a", "c", "d"]

    def test_edit_add_dedup_against_existing(
        self, active_fleet: Path
    ) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a"], force=False)
        cfg.edit("b", add=["a", "x"], remove=[])
        assert cfg.get("b") == ["a", "x"]

    def test_edit_rejects_at_sigil_in_add(
        self, active_fleet: Path
    ) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a"], force=False)
        with pytest.raises(FleetError, match="cannot start with '@'"):
            cfg.edit("b", add=["@other"], remove=[])

    def test_atomic_save_leaves_no_tmp(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("b", ["a"], force=False)
        cfg.save()
        siblings = list(bundles_path().parent.iterdir())
        assert not any(s.name.endswith(".tmp") for s in siblings)

    def test_load_rejects_malformed_json(self, active_fleet: Path) -> None:
        bundles_path().write_text("{ not json", encoding="utf-8")
        with pytest.raises(FleetError, match="Malformed bundles config"):
            BundlesConfig.load()

    def test_load_rejects_nested_at_sigil(self, active_fleet: Path) -> None:
        bundles_path().write_text(
            json.dumps({"bundles": {"b": ["@other"]}}), encoding="utf-8",
        )
        with pytest.raises(FleetError, match="nested reference"):
            BundlesConfig.load()

    def test_load_rejects_non_list_members(self, active_fleet: Path) -> None:
        bundles_path().write_text(
            json.dumps({"bundles": {"b": "not-a-list"}}), encoding="utf-8",
        )
        with pytest.raises(FleetError, match="list of strings"):
            BundlesConfig.load()

    def test_load_strips_utf8_bom(self, active_fleet: Path) -> None:
        """Editors that save with a UTF-8 BOM must not break bundle reads."""
        bundles_path().write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps({"bundles": {"b": ["alpha"]}}).encode("utf-8")
        )
        cfg = BundlesConfig.load()
        assert cfg.get("b") == ["alpha"]


# ---------------------------------------------------------------------------
# expand_bundle_tokens
# ---------------------------------------------------------------------------

class TestExpandBundleTokens:
    def test_passthrough_when_no_at(self, active_fleet: Path) -> None:
        out = expand_bundle_tokens(["alpha", "group/gamma"])
        assert out == ["alpha", "group/gamma"]

    def test_expands_in_order(self, active_fleet: Path) -> None:
        cfg = BundlesConfig.load()
        cfg.add("core", ["alpha", "group/gamma"], force=False)
        cfg.save()

        out = expand_bundle_tokens(["@core"])
        assert out == ["alpha", "group/gamma"]

    def test_mixed_with_raw_preserves_order(
        self, active_fleet: Path
    ) -> None:
        cfg = BundlesConfig.load()
        cfg.add("core", ["alpha"], force=False)
        cfg.save()

        out = expand_bundle_tokens(["beta", "@core", "delta"])
        assert out == ["beta", "alpha", "delta"]

    def test_unknown_bundle_raises(self, active_fleet: Path) -> None:
        with pytest.raises(FleetError, match="Unknown bundle '@nope'"):
            expand_bundle_tokens(["@nope"])

    def test_empty_at_raises(self, active_fleet: Path) -> None:
        with pytest.raises(FleetError, match="Empty bundle reference"):
            expand_bundle_tokens(["@"])

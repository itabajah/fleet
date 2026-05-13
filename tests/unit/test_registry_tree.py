"""Registry-tree normalization, expansion, and resolution."""

from __future__ import annotations

from fleet.registry_tree import (
    empty_node,
    expanded_registry,
    insert_collapsed,
    normalize_node,
    resolve_state,
)


def test_empty_node_shape() -> None:
    assert empty_node() == {
        "sync": True, "repos": [], "exclude": [], "subfolders": {},
    }


def test_normalize_node_handles_non_dict() -> None:
    assert normalize_node("not a dict") == empty_node()
    assert normalize_node(None) == empty_node()


def test_normalize_node_defaults_missing_fields() -> None:
    out = normalize_node({})
    assert out == empty_node()


def test_normalize_node_keeps_explicit_values() -> None:
    out = normalize_node({
        "sync": False,
        "repos": ["a"],
        "exclude": ["b"],
        "subfolders": {"c": {"sync": True}},
    })
    assert out["sync"] is False
    assert out["repos"] == ["a"]
    assert out["exclude"] == ["b"]
    assert "c" in out["subfolders"]


def test_insert_collapsed_creates_chain() -> None:
    parent: dict = {}
    insert_collapsed(parent, "a/b/c", {"repos": ["x"]})
    assert "a" in parent
    assert "b" in parent["a"]["subfolders"]
    leaf = parent["a"]["subfolders"]["b"]["subfolders"]["c"]
    assert leaf["repos"] == ["x"]


def test_insert_collapsed_handles_backslash() -> None:
    parent: dict = {}
    insert_collapsed(parent, "a\\b/c", {"repos": ["y"]})
    assert parent["a"]["subfolders"]["b"]["subfolders"]["c"]["repos"] == ["y"]


def test_insert_collapsed_ignores_empty_key() -> None:
    parent: dict = {}
    insert_collapsed(parent, "//", {"repos": ["x"]})
    assert parent == {}


def test_insert_collapsed_merge_more_restrictive_sync() -> None:
    parent: dict = {}
    insert_collapsed(parent, "a", {"sync": True, "repos": ["x"]})
    insert_collapsed(parent, "a", {"sync": False, "repos": ["y"]})
    assert parent["a"]["sync"] is False
    assert parent["a"]["repos"] == ["x", "y"]


def test_expanded_registry_skips_root_meta() -> None:
    raw = {"root": ".", "alpha": {"sync": True, "repos": ["x"]}}
    out = expanded_registry(raw)
    assert "root" not in out
    assert "alpha" in out


def test_expanded_registry_handles_non_dict() -> None:
    assert expanded_registry("nope") == {}


# ---------------------------------------------------------------------------
# resolve_state
# ---------------------------------------------------------------------------

def _expanded_from(raw: dict) -> dict:
    return expanded_registry(raw)


def test_resolve_top_level_unknown_is_enabled_unregistered() -> None:
    expanded = _expanded_from({})
    assert resolve_state(expanded, ("foo",)) == (True, False)


def test_resolve_top_level_in_registry() -> None:
    expanded = _expanded_from({".": {"sync": True, "repos": ["foo"]}})
    assert resolve_state(expanded, ("foo",)) == (True, True)


def test_resolve_top_level_excluded() -> None:
    expanded = _expanded_from({
        ".": {"sync": True, "repos": ["foo"], "exclude": ["foo"]},
    })
    assert resolve_state(expanded, ("foo",)) == (False, True)


def test_resolve_top_level_disabled_node() -> None:
    expanded = _expanded_from({".": {"sync": False, "repos": ["foo"]}})
    enabled, in_reg = resolve_state(expanded, ("foo",))
    assert enabled is False
    assert in_reg is True


def test_resolve_group_unknown_branch() -> None:
    expanded = _expanded_from({})
    assert resolve_state(expanded, ("group", "repo")) == (True, False)


def test_resolve_disabled_at_root_of_group() -> None:
    expanded = _expanded_from({"group": {"sync": False}})
    enabled, in_reg = resolve_state(expanded, ("group", "repo"))
    assert enabled is False
    assert in_reg is False


def test_resolve_disabled_mid_walk_finds_leaf_in_registry() -> None:
    raw = {
        "group": {
            "sync": False,
            "subfolders": {"sub": {"sync": True, "repos": ["repo"]}},
        },
    }
    expanded = _expanded_from(raw)
    enabled, in_reg = resolve_state(expanded, ("group", "sub", "repo"))
    assert enabled is False  # ancestor disabled
    assert in_reg is True   # but the leaf was listed


def test_resolve_excluded_at_leaf() -> None:
    raw = {"group": {"sync": True, "subfolders": {
        "sub": {"sync": True, "repos": ["a"], "exclude": ["a"]},
    }}}
    expanded = _expanded_from(raw)
    enabled, in_reg = resolve_state(expanded, ("group", "sub", "a"))
    assert enabled is False
    assert in_reg is True


def test_resolve_listed_at_leaf() -> None:
    raw = {"group": {"sync": True, "subfolders": {
        "sub": {"sync": True, "repos": ["a"]},
    }}}
    expanded = _expanded_from(raw)
    enabled, in_reg = resolve_state(expanded, ("group", "sub", "a"))
    assert enabled is True
    assert in_reg is True


def test_resolve_collapsed_key_expands_first() -> None:
    raw = {"group/sub": {"sync": True, "repos": ["a"]}}
    expanded = _expanded_from(raw)
    enabled, in_reg = resolve_state(expanded, ("group", "sub", "a"))
    assert enabled is True
    assert in_reg is True


def test_resolve_empty_parts_neutral() -> None:
    assert resolve_state({}, ()) == (True, False)

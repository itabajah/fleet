"""Normalization + traversal helpers for the registry tree (``fleet.json``).

The on-disk registry is a recursive dict: each node has ``sync``, ``repos``,
``exclude``, ``subfolders``, and (some) keys may collapse multiple path
segments into one (e.g. ``"a/b/c": {...}``). Both :mod:`fleet.discovery`
and :mod:`fleet.scan` need to:

  - expand collapsed keys into single-segment-keyed trees, and
  - resolve a (parts...) path against the expanded tree to determine
    enabled/registered state.

This module owns those operations so the two callers can never drift apart.
"""

from __future__ import annotations

import re

from fleet.paths import ROOT_META_KEYS

_SLASH_RE = re.compile(r"[\\/]+")


def empty_node() -> dict:
    """A fresh, empty, sync-enabled registry node."""
    return {"sync": True, "repos": [], "exclude": [], "subfolders": {}}


def normalize_node(raw: object) -> dict:
    """Convert a raw registry node into normalized form (recursive).

    Tolerant of missing/typo'd keys: anything not recognised is dropped,
    and string lists are coerced via ``list(... or [])``.
    """
    if not isinstance(raw, dict):
        return empty_node()
    subfolders: dict = {}
    node: dict = {
        "sync": bool(raw.get("sync", True)),
        "repos": list(raw.get("repos") or []),
        "exclude": list(raw.get("exclude") or []),
        "subfolders": subfolders,
    }
    raw_subs = raw.get("subfolders") or {}
    if isinstance(raw_subs, dict):
        for key, val in raw_subs.items():
            insert_collapsed(subfolders, key, val)
    return node


def insert_collapsed(parent: dict, raw_key: str, raw_value: object) -> None:
    """Insert a possibly-collapsed key like 'a/b/c' as nested sub-nodes.

    When the leaf already exists, we merge: prefer the more restrictive
    ``sync`` flag and union ``repos`` / ``exclude`` / ``subfolders``.
    """
    parts = [p for p in _SLASH_RE.split(raw_key) if p]
    if not parts:
        return
    cur = parent
    for seg in parts[:-1]:
        if seg not in cur:
            cur[seg] = empty_node()
        cur = cur[seg]["subfolders"]
    leaf = parts[-1]
    expanded = normalize_node(raw_value)
    if leaf in cur:
        existing = cur[leaf]
        existing["sync"] = existing["sync"] and expanded["sync"]
        existing["repos"] = sorted(set(existing["repos"]) | set(expanded["repos"]))
        existing["exclude"] = sorted(set(existing["exclude"]) | set(expanded["exclude"]))
        for k, v in expanded["subfolders"].items():
            existing["subfolders"].setdefault(k, v)
    else:
        cur[leaf] = expanded


def expanded_registry(raw: object) -> dict:
    """Normalize an entire registry into a tree of single-segment keys.

    Top-level keys in ``ROOT_META_KEYS`` (currently just ``"root"``) are
    skipped — they're metadata, not folder nodes.
    """
    expanded: dict = {}
    if not isinstance(raw, dict):
        return expanded
    for key, val in raw.items():
        if key in ROOT_META_KEYS:
            continue
        insert_collapsed(expanded, key, val)
    return expanded


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _is_listed_under(node: dict, rel_parts: tuple[str, ...]) -> bool:
    """Recursive: is ``rel_parts`` listed somewhere under ``node``?"""
    if not rel_parts:
        return False
    if len(rel_parts) == 1:
        return rel_parts[0] in node.get("repos", [])
    sub = node.get("subfolders", {}).get(rel_parts[0])
    if sub is None:
        return False
    return _is_listed_under(sub, rel_parts[1:])


def resolve_state(expanded: dict, parts: tuple[str, ...]) -> tuple[bool, bool]:
    """Walk ``expanded`` along ``parts``. Return ``(enabled, in_registry)``.

    ``enabled`` is False when any ancestor node has ``sync:false`` or when
    the leaf name appears in the parent node's ``exclude``. ``in_registry``
    is True iff the leaf name is explicitly listed in the parent node's
    ``repos`` array.
    """
    if not parts:
        return True, False

    if len(parts) == 1:
        # Top-level repo lives in the synthetic "." node.
        node = expanded.get(".")
        if node is None:
            return True, False
        if not node["sync"]:
            return False, parts[0] in node["repos"]
        excluded = parts[0] in node["exclude"]
        in_reg = parts[0] in node["repos"]
        return (not excluded), in_reg

    cur = expanded.get(parts[0])
    if cur is None:
        return True, False
    if not cur["sync"]:
        return False, _is_listed_under(cur, parts[1:])

    # parts[1:-1] are intermediate segments; index `i` (1-based into parts)
    # tells us where we are so we can hand the right tail to _is_listed_under
    # if we hit a sync:false node mid-walk.
    for i in range(1, len(parts) - 1):
        seg = parts[i]
        sub = cur["subfolders"].get(seg)
        if sub is None:
            return True, False
        if not sub["sync"]:
            return False, _is_listed_under(sub, parts[i + 1:])
        cur = sub

    leaf = parts[-1]
    excluded = leaf in cur["exclude"]
    in_reg = leaf in cur["repos"]
    return (not excluded), in_reg

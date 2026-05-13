"""``fleet scan`` — walk the disk, rewrite ``fleet.json`` preserving manual settings.

Output shape::

    {
      "root": ".",
      "..me": { "sync": false },
      ".clusters": {
        "sync": true,
        "repos": [...],
        "subfolders": {
          "Infra.K8s.Clusters/temp": { ... }   # single-child chains collapsed
        }
      },
      ...
    }
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path

from fleet.console import cyan, gray, green, yellow
from fleet.paths import REGISTRY_FILENAME
from fleet.registry_tree import empty_node, expanded_registry
from fleet.state import find_repos_root, load_registry
from fleet.walker import walk_repos

# ---------------------------------------------------------------------------
# Build the new structure from disk + existing config
# ---------------------------------------------------------------------------

def _get_or_create(parent: dict, name: str, existing_node: dict | None) -> dict:
    """Return ``parent[name]``, creating it (and recovering sync/exclude from existing)."""
    if name in parent:
        return parent[name]

    sync = True
    exclude: list[str] = []

    if existing_node is not None:
        old: dict | None = None
        # Two ways the existing tree might address this name:
        #   1. As a direct child of `existing_node` (when existing_node is a
        #      level-up in the expanded-existing tree).
        #   2. Under existing_node["subfolders"][name] (when existing_node IS
        #      a node).
        direct = existing_node.get(name)
        if isinstance(direct, dict) and "sync" in direct:
            old = direct
        elif "subfolders" in existing_node:
            sub = existing_node["subfolders"].get(name)
            if isinstance(sub, dict):
                old = sub
        if old is not None:
            sync = bool(old.get("sync", True))
            exclude = list(old.get("exclude") or [])

    node = empty_node()
    node["sync"] = sync
    node["exclude"] = exclude
    parent[name] = node
    return node


def _build_structure(scan_root: Path, expanded_existing: dict) -> tuple[dict, int, int]:
    """Walk disk, drop every repo into the right node. Returns ``(structure, total, new)``."""
    structure: dict = {}
    total = 0
    new_count = 0

    scan_root_abs = scan_root.resolve()

    # Track which (group_path, repo_name) tuples were already in the
    # existing registry so we can count "new" finds for the summary.
    previously_listed: set[tuple[str, ...]] = set()

    def _collect_listed(node: dict, prefix: tuple[str, ...]) -> None:
        for repo_name in node.get("repos", []):
            previously_listed.add(prefix + (repo_name,))
        for sub_name, sub in node.get("subfolders", {}).items():
            _collect_listed(sub, prefix + (sub_name,))

    for top_name, top_node in expanded_existing.items():
        _collect_listed(top_node, (top_name,))

    for repo_dir in walk_repos(scan_root):
        try:
            rel = repo_dir.resolve().relative_to(scan_root_abs)
        except ValueError:
            continue
        parts = list(rel.parts)
        if not parts:
            continue

        repo_name = parts[-1]
        parent_parts = parts[:-1]

        # Walk down structure + existing in lockstep.
        cur_dict = structure
        cur_existing: dict | None = expanded_existing
        target_node: dict | None = None

        if not parent_parts:
            target_node = _get_or_create(cur_dict, ".", cur_existing)
        else:
            disabled = False
            node: dict | None = None
            for seg in parent_parts:
                node = _get_or_create(cur_dict, seg, cur_existing)
                if not node["sync"]:
                    disabled = True
                    break
                cur_dict = node["subfolders"]
                # Advance existing pointer.
                if cur_existing is None:
                    nxt: dict | None = None
                else:
                    direct = cur_existing.get(seg)
                    if isinstance(direct, dict) and "subfolders" in direct:
                        nxt = direct
                    else:
                        subs_obj = cur_existing.get("subfolders")
                        subs = subs_obj if isinstance(subs_obj, dict) else None
                        nxt = subs.get(seg) if subs else None
                cur_existing = nxt
            if disabled:
                continue
            target_node = node

        if target_node is None or not target_node["sync"]:
            continue
        if repo_name in target_node["exclude"]:
            continue
        if repo_name not in target_node["repos"]:
            target_node["repos"].append(repo_name)
            total += 1
            structural_key: tuple[str, ...] = (
                (".", repo_name)
                if not parent_parts
                else tuple(parent_parts) + (repo_name,)
            )
            if structural_key not in previously_listed:
                new_count += 1

    return structure, total, new_count


# ---------------------------------------------------------------------------
# Post-processing: collapse single-child, sort, cleanup
# ---------------------------------------------------------------------------

def _collapse(structure: dict) -> dict:
    """Recursively collapse single-child folder nodes into ``parent/child`` keys.

    Preserves insertion order so collapsed entries take the slot of their
    parent.
    """
    # Recurse first (bottom-up) so child structures are already collapsed.
    for key in list(structure.keys()):
        structure[key]["subfolders"] = _collapse(structure[key]["subfolders"])

    rebuilt: dict = {}
    changed = False
    for key, node in structure.items():
        has_repos = bool(node["repos"])
        has_exclude = bool(node["exclude"])
        if (not has_repos and not has_exclude
                and len(node["subfolders"]) == 1):
            sub_key, sub_node = next(iter(node["subfolders"].items()))
            sub_node["sync"] = bool(node["sync"] and sub_node["sync"])
            rebuilt[f"{key}/{sub_key}"] = sub_node
            changed = True
        else:
            rebuilt[key] = node
    return rebuilt if changed else structure


def _sort_arrays(structure: dict) -> None:
    """Sort ``repos`` and ``exclude`` alphabetically (case-insensitive) at every level."""
    for node in structure.values():
        node["repos"] = sorted(node["repos"], key=str.casefold)
        node["exclude"] = sorted(node["exclude"], key=str.casefold)
        _sort_arrays(node["subfolders"])


def _preserve_disabled(structure: dict, existing: dict) -> None:
    """Re-attach ``sync:false`` nodes from existing config that the walk missed.

    Folders pruned by the walker (notably ``..me``) never produce nodes
    during the disk walk. Without this pass, hand-curated disable flags
    would silently disappear on every ``fleet scan``.
    """
    for name, node in existing.items():
        if not node.get("sync", True):
            if name not in structure:
                structure[name] = {
                    "sync": False, "repos": [],
                    "exclude": [], "subfolders": {},
                }
            # Don't recurse: _cleanup collapses disabled nodes to {"sync": false}.
            continue
        # Enabled in existing — only recurse into subfolders the walk created.
        if name in structure:
            _preserve_disabled(structure[name]["subfolders"],
                               node.get("subfolders") or {})


def _cleanup(structure: dict) -> None:
    """Drop empty fields. Disabled nodes keep only their ``sync`` flag."""
    for key in list(structure.keys()):
        node = structure[key]
        if not node["sync"]:
            structure[key] = {"sync": False}
            continue
        _cleanup(node["subfolders"])
        if not node["repos"]:
            del node["repos"]
        if not node["exclude"]:
            del node["exclude"]
        if not node["subfolders"]:
            del node["subfolders"]


def _count_enabled(structure: dict) -> int:
    """Total count of enabled (non-excluded) repos, recursively."""
    total = 0
    for node in structure.values():
        if not node.get("sync", True):
            continue
        repos = node.get("repos", [])
        excludes = set(node.get("exclude", []))
        total += sum(1 for r in repos if r not in excludes)
        if "subfolders" in node:
            total += _count_enabled(node["subfolders"])
    return total


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` via a temp-file + rename so a crash
    mid-write can't truncate the existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------

def cmd_scan(_args: argparse.Namespace) -> int:
    repos_root = find_repos_root()
    target_path = repos_root / REGISTRY_FILENAME

    existing_raw = load_registry()
    expanded_existing = expanded_registry(existing_raw)

    config_root = "."
    if isinstance(existing_raw.get("root"), str) and existing_raw["root"]:
        config_root = existing_raw["root"]

    scan_root = (repos_root / config_root).resolve()
    if not scan_root.is_dir():
        print(yellow(
            f"⚠ Warning: Root path '{scan_root}' does not exist. "
            "Defaulting to repos root."
        ))
        scan_root = repos_root
        config_root = "."

    print(cyan(f"Scanning for git repositories in: {scan_root}"))
    print(gray("This may take a moment..."))

    structure, total, new_count = _build_structure(scan_root, expanded_existing)
    _preserve_disabled(structure, expanded_existing)
    structure = _collapse(structure)
    _sort_arrays(structure)
    _cleanup(structure)

    final: dict = {"root": config_root, **structure}
    _atomic_write_json(target_path, final)

    enabled = _count_enabled(structure)
    print(green("✓ Scan complete."))
    print(f"  Total repositories found: {total}")
    print(f"  New repositories found:   {new_count}")
    print(f"  Repositories enabled:     {enabled}")
    print(cyan(f"Configuration saved to: {target_path}"))
    print()
    print(gray("Run `fleet sync` to update them all."))
    return 0


def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> None:
    """Register the ``fleet scan`` subcommand."""
    p = subparsers.add_parser(
        "scan", parents=[fleet_arg],
        help="rescan disk and rewrite fleet.json (preserves manual sync/exclude)",
    )
    p.set_defaults(func=cmd_scan)

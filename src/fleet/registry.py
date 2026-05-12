"""`fleet scan` — walk the disk, rewrite fleet.json preserving manual settings.

Port of sync.ps1's -Scan mode (Add-ExpandedSubfolder / ConvertTo-FolderNode /
Invoke-CollapseNodes / Invoke-SortNodes / Invoke-CleanupNodes / Get-Or-Create-Node).
The Python version is shorter because there's no PSCustomObject vs IDictionary
discrimination — everything is a plain dict.

Output shape:

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
import json
import os
import re
import sys
from pathlib import Path

from fleet.config import (
    FleetError,
    REGISTRY_FILENAME,
    ROOT_META_KEYS,
    TOOL_HOME_DIRNAME,
    cyan,
    find_repos_root,
    gray,
    green,
    load_registry,
    yellow,
)


# ---------------------------------------------------------------------------
# Node model
# ---------------------------------------------------------------------------

def _empty_node() -> dict:
    return {"sync": True, "repos": [], "exclude": [], "subfolders": {}}


def _normalize_existing(raw) -> dict:
    """Convert a raw registry node into normalized form (single-segment subkeys)."""
    if not isinstance(raw, dict):
        return _empty_node()
    node = {
        "sync": bool(raw.get("sync", True)),
        "repos": list(raw.get("repos") or []),
        "exclude": list(raw.get("exclude") or []),
        "subfolders": {},
    }
    raw_subs = raw.get("subfolders") or {}
    if isinstance(raw_subs, dict):
        for key, val in raw_subs.items():
            _insert_collapsed(node["subfolders"], key, val)
    return node


def _insert_collapsed(parent: dict, raw_key: str, raw_value) -> None:
    """Insert a possibly-collapsed key like 'a/b/c' into `parent`."""
    parts = [p for p in re.split(r"[\\/]+", raw_key) if p]
    if not parts:
        return
    cur = parent
    for seg in parts[:-1]:
        if seg not in cur:
            cur[seg] = _empty_node()
        cur = cur[seg]["subfolders"]
    leaf = parts[-1]
    expanded = _normalize_existing(raw_value)
    if leaf in cur:
        existing = cur[leaf]
        existing["sync"] = existing["sync"] and expanded["sync"]
        existing["repos"] = sorted(set(existing["repos"]) | set(expanded["repos"]))
        existing["exclude"] = sorted(
            set(existing["exclude"]) | set(expanded["exclude"])
        )
        for k, v in expanded["subfolders"].items():
            existing["subfolders"].setdefault(k, v)
    else:
        cur[leaf] = expanded


def _expanded_existing(raw_config: dict) -> dict:
    """Normalize the entire existing registry into a tree of single-segment keys."""
    expanded: dict = {}
    if not isinstance(raw_config, dict):
        return expanded
    for key, val in raw_config.items():
        if key in ROOT_META_KEYS:
            continue
        _insert_collapsed(expanded, key, val)
    return expanded


# ---------------------------------------------------------------------------
# Build the new structure from disk + existing config
# ---------------------------------------------------------------------------

def _get_or_create(parent: dict, name: str, existing_node: dict | None) -> dict:
    """Return parent[name], creating it (and recovering sync/exclude from existing)."""
    if name in parent:
        return parent[name]

    sync = True
    exclude: list[str] = []

    if existing_node is not None:
        old: dict | None = None
        # Two ways the existing tree might address this name:
        # 1. As a direct child of `existing_node` (when existing_node IS a level-up
        #    in the expanded-existing tree).
        # 2. Under existing_node["subfolders"][name] (when existing_node is a node).
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

    parent[name] = {
        "sync": sync,
        "repos": [],
        "exclude": exclude,
        "subfolders": {},
    }
    return parent[name]


def _walk_git_dirs(root: Path):
    """Yield directories under `root` that contain a `.git` entry. Skips ..me."""
    # Match sync.ps1 `Get-ChildItem -Recurse -Filter .git -Force` semantics
    # (find every .git directory under root). We additionally prune obvious
    # heavy build dirs to keep scans fast.
    prune = {"node_modules", "__pycache__", ".venv", "venv", ".tox",
             ".mypy_cache", ".pytest_cache", ".next", ".turbo",
             "dist", "build", "target", "bin", "obj", TOOL_HOME_DIRNAME}
    stack: list[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name == ".git":
                # Found a repo: yield its parent directory.
                yield cur
                continue
            if entry.name in prune:
                continue
            stack.append(Path(entry.path))


def _build_structure(scan_root: Path, expanded_existing: dict) -> tuple[dict, int, int]:
    """Walk disk, add every repo into the right node. Returns (structure, total, new)."""
    structure: dict = {}
    total = 0
    new_count = 0

    # Use absolute scan_root for relative-path arithmetic.
    scan_root_abs = scan_root.resolve()

    # Track which (group_path, repo_name) tuples were already in the existing
    # registry so we can count "new" finds.
    previously_listed: set[tuple[str, ...]] = set()

    def _collect_listed(node: dict, prefix: tuple[str, ...]) -> None:
        for repo_name in node.get("repos", []):
            previously_listed.add(prefix + (repo_name,))
        for sub_name, sub in node.get("subfolders", {}).items():
            _collect_listed(sub, prefix + (sub_name,))

    for top_name, top_node in expanded_existing.items():
        _collect_listed(top_node, (top_name,))

    for repo_dir in _walk_git_dirs(scan_root):
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
            # Top-level repo lives in synthetic "." node.
            target_node = _get_or_create(cur_dict, ".", cur_existing)
        else:
            disabled = False
            for i, seg in enumerate(parent_parts):
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
                        subs = cur_existing.get("subfolders") if isinstance(
                            cur_existing.get("subfolders"), dict) else None
                        nxt = subs.get(seg) if subs else None
                cur_existing = nxt
            if disabled:
                continue
            # Need a node at parent_parts[-1] (already created above as `node`).
            target_node = node  # type: ignore[possibly-undefined]

        if target_node is None or not target_node["sync"]:
            continue
        if repo_name in target_node["exclude"]:
            continue
        if repo_name not in target_node["repos"]:
            target_node["repos"].append(repo_name)
            total += 1
            # Match against previously_listed keys, which are (top, sub..., repo).
            # Top-level repos live under the synthetic "." node, so prefix
            # parent_parts with "." when there is no real parent group.
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
    """Recursively collapse single-child folder nodes into 'parent/child' keys.

    Preserves insertion order so collapsed entries take the slot of their parent.
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
    """Sort `repos` and `exclude` alphabetically (case-insensitive) at every level."""
    for node in structure.values():
        node["repos"] = sorted(node["repos"], key=str.casefold)
        node["exclude"] = sorted(node["exclude"], key=str.casefold)
        _sort_arrays(node["subfolders"])


def _preserve_disabled(structure: dict, existing: dict) -> None:
    """Re-attach `sync:false` nodes from existing config that the walk missed.

    Folders pruned by `_walk_git_dirs` (notably `..me`) never produce nodes
    during the disk walk. Without this pass, hand-curated disable flags would
    silently disappear on every `fleet scan`.
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
        # Enabled in existing — only recurse into subfolders the walk already created.
        if name in structure:
            _preserve_disabled(structure[name]["subfolders"],
                               node.get("subfolders") or {})


def _cleanup(structure: dict) -> None:
    """Drop empty fields. Disabled nodes keep only their `sync` flag."""
    for key in list(structure.keys()):
        node = structure[key]
        if not node["sync"]:
            # Disabled: only keep `sync: false`. Drop everything else.
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


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------

def cmd_scan(_args: argparse.Namespace) -> int:
    repos_root = find_repos_root()
    target_path = repos_root / REGISTRY_FILENAME

    existing_raw = load_registry()
    expanded_existing = _expanded_existing(existing_raw)

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

    final = {"root": config_root}
    for k, v in structure.items():
        final[k] = v

    # Atomic write: stage to .tmp then replace.
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    try:
        tmp_path.write_text(
            json.dumps(final, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    enabled = _count_enabled(structure)
    print(green("✓ Scan complete."))
    print(f"  Total repositories found: {total}")
    print(f"  New repositories found:   {new_count}")
    print(f"  Repositories enabled:     {enabled}")
    print(cyan(f"Configuration saved to: {target_path}"))
    print()
    print(gray("Run `fleet sync` to update them all."))
    return 0

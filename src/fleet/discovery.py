"""Repo discovery: walk the disk, apply registry sync/exclude rules.

The registry (fleet.json) stores user *preferences* (which repos to sync, which to
exclude). The disk is the source of truth for what actually exists. This module
combines them into a list of `RepoInfo` records.

Path semantics
--------------
Each repo's `parts` are its directory segments relative to the configured sync
root, e.g.::

    REPOS_ROOT/
      defender-developer-docs/        -> parts = ["defender-developer-docs"]
      .clusters/Infra.K8s.Clusters/   -> parts = [".clusters", "Infra.K8s.Clusters"]
      .clusters/Infra.K8s.Clusters/temp/Infra.Pipelines.Templates/
                                      -> parts = [".clusters", "Infra.K8s.Clusters",
                                                  "temp", "Infra.Pipelines.Templates"]

The registry mirrors this as a nested tree, but stores some keys in collapsed
form (e.g. ``"Infra.K8s.Clusters/temp"``). `_normalize_registry` expands those.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from fleet.config import (
    PRUNE_DIRS,
    ROOT_META_KEYS,
    TOOL_HOME_DIRNAME,
    fleet_install_dir,
    load_registry,
    sync_root,
)

# Names to skip during disk walk. `.git` is internal git state; the rest are
# common heavy build/dependency dirs that can't contain a managed repo.
_PRUNE_DIRS = PRUNE_DIRS | {".git", TOOL_HOME_DIRNAME}


@dataclass(frozen=True)
class RepoInfo:
    """A git repo on disk, optionally annotated with registry state."""

    name: str           # leaf directory name (e.g. "defender-developer-docs")
    group_path: str     # "/"-separated parent path relative to sync root; "" for top-level
    path: Path          # absolute path to the repo
    enabled: bool       # False if registry disables it (sync:false or excluded)
    in_registry: bool   # True if explicitly listed in registry's repos array

    @property
    def display_name(self) -> str:
        """Human-readable identifier: 'group/path/name' or just 'name'."""
        return f"{self.group_path}/{self.name}" if self.group_path else self.name

    @property
    def parts(self) -> tuple[str, ...]:
        """Path segments from sync root to (and including) this repo's directory."""
        if not self.group_path:
            return (self.name,)
        return (*self.group_path.split("/"), self.name)


# ---------------------------------------------------------------------------
# Disk walk
# ---------------------------------------------------------------------------

def _looks_like_git_repo(p: Path) -> bool:
    # `.git` is a directory in normal repos but a regular file in worktrees
    # (containing `gitdir: …`). `exists()` matches both.
    return (p / ".git").exists()


def _walk_repos(root: Path, _seen: set | None = None,
                _skip: Path | None = None) -> Iterator[Path]:
    """Yield every directory under `root` that contains a `.git` entry.

    Always recurses into found repos because the convention here is to clone
    sibling repos under a repo's `temp/` subdirectory (see fleet.json's
    `Infra.K8s.Clusters/temp` etc.). Skips obvious heavy dirs and dedupes by
    resolved path so symlink/junction loops can't crash the walk.
    """
    if _seen is None:
        _seen = set()
    if _skip is None:
        try:
            _skip = fleet_install_dir().resolve()
        except OSError:
            _skip = None
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if entry.name in _PRUNE_DIRS:
            continue
        p = Path(entry.path)
        try:
            real = p.resolve()
        except OSError:
            continue
        if _skip is not None and real == _skip:
            continue
        if real in _seen:
            continue
        _seen.add(real)
        if _looks_like_git_repo(p):
            yield p
        yield from _walk_repos(p, _seen, _skip)


# ---------------------------------------------------------------------------
# Registry normalization (expand collapsed "a/b/c" keys into nested form)
# ---------------------------------------------------------------------------

def _empty_node() -> dict:
    return {"sync": True, "repos": [], "exclude": [], "subfolders": {}}


def _normalize_node(raw) -> dict:
    """Convert a raw registry node (parsed JSON dict) into normalized form."""
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
    """Insert a possibly-collapsed key like 'a/b/c' as nested sub-nodes."""
    parts = [p for p in re.split(r"[\\/]+", raw_key) if p]
    if not parts:
        return
    cur = parent
    for seg in parts[:-1]:
        if seg not in cur:
            cur[seg] = _empty_node()
        cur = cur[seg]["subfolders"]
    leaf = parts[-1]
    expanded = _normalize_node(raw_value)
    if leaf in cur:
        # Merge — prefer the more restrictive sync flag.
        existing = cur[leaf]
        existing["sync"] = existing["sync"] and expanded["sync"]
        existing["repos"] = sorted(set(existing["repos"]) | set(expanded["repos"]))
        existing["exclude"] = sorted(set(existing["exclude"]) | set(expanded["exclude"]))
        for k, v in expanded["subfolders"].items():
            existing["subfolders"].setdefault(k, v)
    else:
        cur[leaf] = expanded


def _normalize_registry(raw: dict) -> dict:
    """Expand the entire registry into a per-segment-keyed tree.

    Top-level repos directly under the sync root live under the synthetic key
    ``"."``, matching the convention used by sync.ps1's scan output.
    """
    expanded: dict = {}
    if not isinstance(raw, dict):
        return expanded
    for key, val in raw.items():
        if key in ROOT_META_KEYS:
            continue
        _insert_collapsed(expanded, key, val)
    return expanded


# ---------------------------------------------------------------------------
# Apply registry rules to a discovered repo
# ---------------------------------------------------------------------------

def _resolve_state(expanded: dict, parts: tuple[str, ...]) -> tuple[bool, bool]:
    """Walk the normalized registry along `parts`. Returns (enabled, in_registry).

    `enabled` is False when any ancestor node has sync:false or when the leaf
    name appears in the parent node's `exclude`. `in_registry` is True iff the
    leaf name is explicitly listed in the parent node's `repos`.
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

    # parts[1:-1] are the intermediate segments; index `i` (1-based into parts)
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


def _is_listed_under(node: dict, rel_parts: tuple[str, ...]) -> bool:
    """Recursive helper: is `rel_parts` listed somewhere under `node`?"""
    if not rel_parts:
        return False
    if len(rel_parts) == 1:
        return rel_parts[0] in node.get("repos", [])
    sub = node.get("subfolders", {}).get(rel_parts[0])
    if sub is None:
        return False
    return _is_listed_under(sub, rel_parts[1:])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_repos() -> list[RepoInfo]:
    """Return every git repo on disk under the configured sync root.

    Always excludes the ``..me`` subtree. Each repo is annotated with its
    registry state (`enabled`, `in_registry`).
    """
    root = sync_root()
    if not root.is_dir():
        return []

    expanded = _normalize_registry(load_registry())
    base = root.resolve()
    out: list[RepoInfo] = []

    for repo_path in _walk_repos(root):
        try:
            rel = repo_path.resolve().relative_to(base)
        except ValueError:
            continue
        parts = tuple(rel.parts)
        if not parts:
            continue
        enabled, in_reg = _resolve_state(expanded, parts)
        group_path = "/".join(parts[:-1])
        out.append(RepoInfo(
            name=parts[-1],
            group_path=group_path,
            path=repo_path,
            enabled=enabled,
            in_registry=in_reg,
        ))

    out.sort(key=lambda r: (r.group_path, r.name))
    return out


def repos_to_sync() -> list[RepoInfo]:
    """Subset of `discover_repos()` that `fleet sync` should operate on.

    Matches sync.ps1 today: only repos explicitly listed in the registry and
    not disabled. Disk-only repos are skipped (use `fleet scan` to add them).
    """
    return [r for r in discover_repos() if r.enabled and r.in_registry]

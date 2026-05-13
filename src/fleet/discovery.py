"""Repo discovery: walk the disk, apply registry sync/exclude rules.

The registry (``fleet.json``) stores user *preferences* — which repos to
sync and which to exclude. The disk is the source of truth for what
actually exists. This module combines them into a list of :class:`RepoInfo`
records.

Path semantics
--------------
Each repo's ``parts`` are its directory segments relative to the configured
sync root, e.g.::

    REPOS_ROOT/
      defender-developer-docs/        -> parts = ("defender-developer-docs",)
      .clusters/Infra.K8s.Clusters/   -> parts = (".clusters", "Infra.K8s.Clusters")
      .clusters/Infra.K8s.Clusters/temp/Infra.Pipelines.Templates/
                                      -> parts = (".clusters", "Infra.K8s.Clusters",
                                                  "temp", "Infra.Pipelines.Templates")

The registry mirrors this as a nested tree, but stores some keys in
collapsed form (e.g. ``"Infra.K8s.Clusters/temp"``).
:func:`fleet.registry_tree.expanded_registry` expands those.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fleet.registry_tree import expanded_registry, resolve_state
from fleet.state import load_registry, sync_root
from fleet.walker import walk_repos


@dataclass(frozen=True)
class RepoInfo:
    """A git repo on disk, optionally annotated with registry state."""

    name: str           # leaf directory name (e.g. "defender-developer-docs")
    group_path: str     # "/"-separated parent path; "" for top-level
    path: Path          # absolute path to the repo
    enabled: bool       # False if registry disables it (sync:false or excluded)
    in_registry: bool   # True if explicitly listed in registry's repos array

    @property
    def display_name(self) -> str:
        """Human-readable identifier: ``group/path/name`` or just ``name``."""
        return f"{self.group_path}/{self.name}" if self.group_path else self.name

    @property
    def parts(self) -> tuple[str, ...]:
        """Path segments from sync root to (and including) this repo's directory."""
        if not self.group_path:
            return (self.name,)
        return (*self.group_path.split("/"), self.name)


def discover_repos() -> list[RepoInfo]:
    """Return every git repo on disk under the configured sync root.

    Each repo is annotated with its registry state (``enabled``,
    ``in_registry``). Returns ``[]`` if the sync root doesn't exist.
    """
    root = sync_root()
    if not root.is_dir():
        return []

    expanded = expanded_registry(load_registry())
    base = root.resolve()
    out: list[RepoInfo] = []

    for repo_path in walk_repos(root):
        try:
            rel = repo_path.resolve().relative_to(base)
        except ValueError:
            continue
        parts = tuple(rel.parts)
        if not parts:
            continue
        enabled, in_reg = resolve_state(expanded, parts)
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
    """Subset of :func:`discover_repos` that ``fleet sync`` should operate on.

    Only repos explicitly listed in the registry and not disabled. Disk-only
    repos are skipped — the user runs ``fleet scan`` to add them.
    """
    return [r for r in discover_repos() if r.enabled and r.in_registry]

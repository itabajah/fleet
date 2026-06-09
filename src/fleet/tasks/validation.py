"""``--repos`` token resolution + task branch-name builder."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from fleet.bundles_config import expand_bundle_tokens
from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.state import branch_config, require_active_fleet

if TYPE_CHECKING:
    from fleet.tasks.manifest import Manifest, RepoEntry


def task_branch(name: str) -> str:
    """Branch name created/managed by fleet for a *new* task.

    Rendered from the global convention pinned in ``fleets.json``
    (:class:`fleet.state.BranchConfig`). With ``scoped`` true the active fleet
    is inserted as a middle segment so two fleets sharing a physical canonical
    repo can each have a same-named task without their git branches
    colliding::

        scoped=True   ->  <prefix>/<fleet>/<task>   (default: task/<fleet>/<task>)
        scoped=False  ->  <prefix>/<task>

    Only callers creating a task (``task new``) or deliberately renaming its
    branch (``task rename``) render from config; everything operating on an
    existing task reads ``manifest.branch`` instead, so a later config change
    never desyncs a task's worktrees from its recorded branch.

    Both fleet and task names are validated git-ref-safe, and the rendered
    branch is checked against :func:`validate_branch` at config-load time, so
    the resulting ref is always valid.
    """
    cfg = branch_config()
    if cfg.scoped:
        return f"{cfg.prefix}/{require_active_fleet()}/{name}"
    return f"{cfg.prefix}/{name}"


def resolve_repo(token: str, all_repos: list[RepoInfo]) -> RepoInfo:
    """Resolve a ``--repos`` token to exactly one :class:`RepoInfo`.

    Accepts bare ``name``, or ``group/path/name`` (slashes anywhere in
    group). Raises :class:`FleetError` with did-you-mean suggestions on
    miss/ambiguity, or with a clear message when the repo is
    registry-disabled.
    """
    token = token.replace("\\", "/").strip("/")

    if "/" in token:
        # Split on last slash: everything before is group_path, last is name.
        group_path, _, name = token.rpartition("/")
        matches = [r for r in all_repos
                   if r.name == name and r.group_path == group_path]
        if not matches:
            raise FleetError(
                f"Unknown repo '{group_path}/{name}'. "
                f"Try `fleet repos` to see what's available."
            )
        chosen = matches[0]
    else:
        matches = [r for r in all_repos if r.name == token]
        if len(matches) == 0:
            suggestions = difflib.get_close_matches(
                token, sorted({r.name for r in all_repos}), n=3, cutoff=0.6,
            )
            hint = (f" Did you mean: {', '.join(suggestions)}?"
                    if suggestions else "")
            raise FleetError(f"Unknown repo '{token}'.{hint}")
        if len(matches) > 1:
            disambig = ", ".join(m.display_name for m in matches)
            raise FleetError(
                f"Ambiguous repo name '{token}' — exists in multiple groups. "
                f"Use the full path, one of: {disambig}"
            )
        chosen = matches[0]

    if not chosen.enabled:
        raise FleetError(
            f"Repo '{chosen.display_name}' is disabled in fleet.json "
            f"(sync:false or excluded). Re-enable it there before using it "
            f"as a task target."
        )
    return chosen


def require_repo_in_task(token: str, manifest: Manifest) -> RepoEntry:
    """Resolve a ``--repos`` token to an entry of ``manifest.repos``.

    Accepts a bare leaf name or ``group/path/name``. Raises
    :class:`FleetError` listing current members on miss.
    """
    norm = token.replace("\\", "/").strip("/")
    by_disp = {r.display_name: r for r in manifest.repos}
    by_name: dict[str, list[RepoEntry]] = {}
    for r in manifest.repos:
        by_name.setdefault(r.name, []).append(r)

    if norm in by_disp:
        return by_disp[norm]
    if "/" not in norm:
        cands = by_name.get(norm, [])
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            disambig = ", ".join(c.display_name for c in cands)
            raise FleetError(
                f"Ambiguous repo '{token}' in task '{manifest.name}' — "
                f"matches: {disambig}. Use the full 'group/path/name' form."
            )
    members = ", ".join(sorted(by_disp)) or "(none)"
    raise FleetError(
        f"Repo '{token}' is not in task '{manifest.name}'. "
        f"Members: {members}."
    )


def split_repo_tokens(raw: str) -> list[str]:
    """Split a ``--repos`` CSV, expand ``@bundle`` refs, require non-empty.

    Raises :class:`FleetError` if the list is empty before or after bundle
    expansion (an empty bundle would otherwise silently select nothing).
    """
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise FleetError("--repos requires at least one repo name.")
    tokens = expand_bundle_tokens(tokens)
    if not tokens:
        raise FleetError("--repos requires at least one repo name.")
    return tokens


def select_repos(
    tokens: list[str],
    all_repos: list[RepoInfo],
    *,
    already_in_task: set[tuple[str, str]] | None = None,
    task_name: str | None = None,
) -> list[RepoInfo]:
    """Resolve ``tokens`` to repos in order, dropping in-list duplicates.

    ``already_in_task`` (keyed by ``(group_path, name)``) makes a token that
    is already a task member a hard error naming ``task_name`` — used by
    ``add-repo``. ``task new`` passes neither and so allows any resolvable repo.
    """
    already = already_in_task or set()
    seen: set[tuple[str, str]] = set()
    chosen: list[RepoInfo] = []
    for tok in tokens:
        repo = resolve_repo(tok, all_repos)
        key = (repo.group_path, repo.name)
        if key in already:
            raise FleetError(
                f"Repo '{repo.display_name}' is already in task '{task_name}'."
            )
        if key in seen:
            print(f"note: ignoring duplicate '{tok}'")
            continue
        seen.add(key)
        chosen.append(repo)
    return chosen

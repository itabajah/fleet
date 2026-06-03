"""In-place task editing: ``add-repo``, ``remove-repo``, ``rename``, ``edit``.

Sibling to :mod:`fleet.tasks.lifecycle` (``new``/``end``) and
:mod:`fleet.tasks.inspect` (``list``/``info``/``sync``/``path``). Reuses
:mod:`fleet.tasks.worktree` so the canonical-prepare + worktree-add path
stays identical to ``task new``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from fleet import git_ops
from fleet.bundles_config import expand_bundle_tokens
from fleet.console import yellow
from fleet.discovery import RepoInfo, discover_repos
from fleet.errors import FleetError
from fleet.state import tasks_root
from fleet.tasks.manifest import Manifest, RepoEntry
from fleet.tasks.validation import (
    require_repo_in_task,
    resolve_repo,
    task_branch,
    validate_task_name,
)
from fleet.tasks.worktree import (
    add_worktree,
    assert_no_leaf_collision,
    prepare_canonical,
)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _split_repo_tokens(raw: str) -> list[str]:
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise FleetError("--repos requires at least one repo name.")
    tokens = expand_bundle_tokens(tokens)
    if not tokens:
        raise FleetError("--repos requires at least one repo name.")
    return tokens


def _load_task(name: str) -> tuple[Path, Manifest]:
    validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")
    return workspace, Manifest.load(workspace)


# ---------------------------------------------------------------------------
# task add-repo
# ---------------------------------------------------------------------------

def cmd_add_repo(args: argparse.Namespace) -> int:
    name: str = args.name
    workspace, manifest = _load_task(name)
    tokens = _split_repo_tokens(args.repos)

    all_repos = discover_repos()
    if not all_repos:
        raise FleetError("No repos discovered under the configured Repos root.")

    existing_keys = {(r.group or "", r.name) for r in manifest.repos}
    existing_leaves = {r.name for r in manifest.repos}

    seen: set[tuple[str, str]] = set()
    chosen: list[RepoInfo] = []
    for tok in tokens:
        repo = resolve_repo(tok, all_repos)
        key = (repo.group_path, repo.name)
        if key in existing_keys:
            raise FleetError(
                f"Repo '{repo.display_name}' is already in task '{name}'."
            )
        if key in seen:
            print(f"note: ignoring duplicate '{tok}'")
            continue
        seen.add(key)
        chosen.append(repo)

    # Leaf collision: among new repos, and against existing manifest entries.
    # Case-insensitive on Windows/macOS so 'Foo' vs 'foo' is caught before
    # the second ``git worktree add`` fails mid-scaffold.
    assert_no_leaf_collision(chosen, workspace,
                             existing_leaves=existing_leaves)

    branch = task_branch(name)
    print(f"Adding {len(chosen)} repo(s) to task '{name}' (branch {branch}):")
    for r in chosen:
        print(f"  - {r.display_name}")

    if args.dry_run:
        print(yellow("--dry-run: would pull each canonical and create one "
                     "worktree per repo at:"))
        for r in chosen:
            print(f"  {workspace / r.name}")
        return 0

    # Each entry: (repo, worktree_path, branch_name, branch_is_new). Only
    # delete branches we created in THIS invocation during rollback —
    # reattached pre-existing branches (typical after a remove-repo cycle)
    # must not be destroyed.
    created: list[tuple[RepoInfo, Path, str, bool]] = []
    try:
        for repo in chosen:
            default = prepare_canonical(repo, no_pull=args.no_pull)
            wt, branch_is_new = add_worktree(
                repo, name, default, workspace, reuse_existing=True,
            )
            created.append((repo, wt, branch, branch_is_new))

        new_entries = [
            RepoEntry(
                name=r.name,
                group=r.group_path or None,
                canonical_path=r.path,
                worktree_path=workspace / r.name,
            )
            for r in chosen
        ]
        manifest.add_repos(new_entries)
        manifest.save(workspace)
    except BaseException:
        print("\nERROR adding repos — rolling back created worktrees...",
              file=sys.stderr)
        for repo, wt, br, branch_is_new in created:
            git_ops.run_git("worktree", "remove", "--force", str(wt),
                            cwd=repo.path, check=False)
            if branch_is_new:
                git_ops.run_git("branch", "-D", br,
                                cwd=repo.path, check=False)
        raise

    try:
        _append_context_repos(workspace, chosen)
    except OSError as e:
        print(yellow(f"note: couldn't update context.md ({e}); "
                     "task.json was saved."), file=sys.stderr)

    print(f"\nUpdated task.json (now {len(manifest.repos)} repo(s)).")
    return 0


def _append_context_repos(workspace: Path, added: list[RepoInfo]) -> None:
    """Best-effort: append new repos under context.md's ``## Repos`` section."""
    ctx = workspace / "context.md"
    if not ctx.is_file():
        return
    text = ctx.read_text(encoding="utf-8")
    marker = "\n## Repos\n"
    idx = text.find(marker)
    if idx < 0:
        return
    insert_at = text.find("\n##", idx + len(marker))
    if insert_at < 0:
        insert_at = len(text)
    new_lines = "".join(
        f"- `{r.name}`"
        + (f"  _(group: {r.group_path})_" if r.group_path else "")
        + "\n"
        for r in added
    )
    ctx.write_text(text[:insert_at] + new_lines + text[insert_at:],
                   encoding="utf-8")


# ---------------------------------------------------------------------------
# task remove-repo
# ---------------------------------------------------------------------------

def cmd_remove_repo(args: argparse.Namespace) -> int:
    name: str = args.name
    workspace, manifest = _load_task(name)
    tokens = _split_repo_tokens(args.repos)

    targets: list[RepoEntry] = []
    seen: set[str] = set()
    for tok in tokens:
        entry = require_repo_in_task(tok, manifest)
        if entry.name in seen:
            print(f"note: ignoring duplicate '{tok}'")
            continue
        seen.add(entry.name)
        targets.append(entry)

    branch = manifest.branch

    dirty: list[str] = []
    unpushed_warnings: list[str] = []
    for r in targets:
        if not r.worktree_path.is_dir():
            continue
        if git_ops.is_dirty(r.worktree_path):
            dirty.append(r.name)
        n = git_ops.unpushed_count(r.worktree_path, branch)
        if n is None:
            unpushed_warnings.append(
                f"  {r.name}: branch '{branch}' was never pushed"
            )
        elif n > 0:
            unpushed_warnings.append(
                f"  {r.name}: {n} unpushed commit(s) on '{branch}'"
            )

    if dirty and not args.force:
        raise FleetError(
            "Refusing to remove — uncommitted changes in: "
            + ", ".join(dirty)
            + "\nCommit/stash, or re-run with --force."
        )
    if dirty and args.force:
        print(f"WARN: --force ignoring uncommitted changes in: "
              f"{', '.join(dirty)}")
    if unpushed_warnings:
        print("WARN: unpushed work that will become orphan-able once "
              "the worktree is removed:")
        for line in unpushed_warnings:
            print(line)
        print("(branch stays in the canonical repo, so this is recoverable.)")

    print(f"Removing {len(targets)} repo(s) from task '{name}':")
    for r in targets:
        print(f"  - {r.display_name}")

    if args.dry_run:
        print(yellow("--dry-run: would remove the above worktree(s) and "
                     "drop them from task.json."))
        return 0

    removed_names: set[str] = set()
    failures: list[str] = []
    for r in targets:
        if not r.worktree_path.is_dir():
            print(f"  {r.name}: worktree dir already gone; dropping from manifest")
            removed_names.add(r.name)
            continue
        flag = ["--force"] if args.force else []
        proc = git_ops.run_git(
            "worktree", "remove", *flag, str(r.worktree_path),
            cwd=r.canonical_path, check=False,
        )
        if proc.ok:
            print(f"  removed worktree {r.name}")
            removed_names.add(r.name)
        else:
            failures.append(f"  {r.name}: {proc.stderr.strip()}")

    for r in targets:
        if r.canonical_path.is_dir():
            git_ops.run_git("worktree", "prune",
                            cwd=r.canonical_path, check=False)

    if removed_names:
        manifest.remove_repos(removed_names)
        manifest.save(workspace)

    if not manifest.repos:
        print(yellow(f"WARN: task '{name}' now has no repos."))

    if failures:
        print("\nWARN: some worktrees could not be removed:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        return 2

    print(f"\nUpdated task.json (now {len(manifest.repos)} repo(s)).")
    return 0


# ---------------------------------------------------------------------------
# task rename
# ---------------------------------------------------------------------------

def cmd_rename(args: argparse.Namespace) -> int:
    old: str = args.old
    new: str = args.new
    validate_task_name(old)

    if old == new:
        print(f"note: '{old}' == '{new}'; nothing to do.")
        return 0

    validate_task_name(new)

    root = tasks_root()
    old_workspace = root / old
    new_workspace = root / new
    if not old_workspace.is_dir():
        raise FleetError(f"No such task: {old_workspace}")
    if new_workspace.exists():
        raise FleetError(
            f"Task '{new}' already exists at {new_workspace}. "
            f"Pick a different name."
        )

    manifest = Manifest.load(old_workspace)
    old_branch = manifest.branch
    new_branch = task_branch(new)

    print(f"Renaming task '{old}' -> '{new}':")

    # Step 1: rename branches in every canonical. Track succeeded ones so we
    # can reverse-rename on failure. A canonical that's missing the old
    # branch (e.g. user deleted it manually, or a prior rename completed
    # past this canonical) is warned-about and skipped, not rolled back.
    renamed: list[Path] = []
    for r in manifest.repos:
        proc = git_ops.rename_branch(r.canonical_path, old_branch, new_branch)
        if proc.ok:
            print(f"  [{r.name}] branch {old_branch} -> {new_branch}")
            renamed.append(r.canonical_path)
            continue
        err = (proc.stderr or "").lower()
        if ("not found" in err or "no such" in err or "doesn't exist" in err
                or "no branch named" in err or "invalid branch name" in err
                or "refname" in err):
            print(f"  [{r.name}] WARN: old branch '{old_branch}' not present; "
                  f"skipping ({proc.stderr.strip()})")
            continue
        # Real failure: roll back what we already did.
        print(f"\nERROR renaming branch in {r.name}: {proc.stderr.strip()}",
              file=sys.stderr)
        print("Rolling back already-renamed canonicals...", file=sys.stderr)
        for done in renamed:
            git_ops.rename_branch(done, new_branch, old_branch)
        raise FleetError(
            f"Branch rename failed in {r.name}; aborted before moving "
            f"workspace. No filesystem changes made."
        )

    # Step 2: move workspace dir. Local + reversible.
    try:
        shutil.move(str(old_workspace), str(new_workspace))
    except OSError as e:
        print(f"\nERROR moving workspace dir: {e}", file=sys.stderr)
        print("Reverting branch renames...", file=sys.stderr)
        for done in renamed:
            git_ops.rename_branch(done, new_branch, old_branch)
        raise FleetError(
            f"Couldn't move {old_workspace} -> {new_workspace}: {e}\n"
            f"(VS Code or another process may be holding the old path open.)"
        ) from e
    print(f"  moved workspace -> {new_workspace}")

    # Step 3: rewrite manifest (atomic) + patch context.md header.
    manifest.rename(new, new_branch, new_workspace)
    manifest.save(new_workspace)

    # Step 4: repair each canonical's worktree record so git knows where
    # the moved worktree lives. Without this, `git worktree remove/list`
    # from the canonical still resolves to the old path and fails. Run
    # AFTER the manifest save (which is the durable source of truth for
    # the new paths) so a repair-only crash leaves the manifest correct.
    for r in manifest.repos:
        git_ops.worktree_repair(r.canonical_path, r.worktree_path)

    try:
        _rewrite_context_header(new_workspace, old, new, new_branch)
    except OSError as e:
        print(yellow(f"note: couldn't update context.md ({e}); "
                     "task.json was saved."), file=sys.stderr)

    print(f"  rewrote task.json + context.md")
    print(f"\nDone. (If you were inside the old workspace, "
          f"cd to {new_workspace}.)")
    return 0


def _rewrite_context_header(workspace: Path, old: str, new: str,
                            new_branch: str) -> None:
    ctx = workspace / "context.md"
    if not ctx.is_file():
        return
    lines = ctx.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    swapped_title = False
    swapped_branch = False
    for line in lines:
        if not swapped_title and line.strip() == f"# {old}":
            out.append(f"# {new}\n")
            swapped_title = True
            continue
        if not swapped_branch and line.startswith("_Branch:_"):
            out.append(f"_Branch:_ `{new_branch}`\n")
            swapped_branch = True
            continue
        out.append(line)
    ctx.write_text("".join(out), encoding="utf-8")


# ---------------------------------------------------------------------------
# task edit
# ---------------------------------------------------------------------------

def cmd_edit(args: argparse.Namespace) -> int:
    name: str = args.name
    workspace, manifest = _load_task(name)

    description = args.description
    file_arg: str | None = args.description_file

    if description is None and file_arg is None:
        raise FleetError(
            "task edit needs --description TEXT or --description-file PATH."
        )

    if file_arg is not None:
        if file_arg == "-":
            description = sys.stdin.read()
        else:
            try:
                description = Path(file_arg).read_text(encoding="utf-8")
            except OSError as e:
                raise FleetError(
                    f"Couldn't read --description-file {file_arg}: {e}"
                ) from e

    manifest.description = description
    manifest.save(workspace)

    try:
        _rewrite_context_description(workspace, description)
    except OSError as e:
        print(yellow(f"note: couldn't update context.md ({e}); "
                     "task.json was saved."), file=sys.stderr)

    print(f"Updated description for task '{name}'.")
    return 0


def _rewrite_context_description(workspace: Path, description: str) -> None:
    """Replace the body of context.md's ``## Description`` section."""
    ctx = workspace / "context.md"
    if not ctx.is_file():
        return
    text = ctx.read_text(encoding="utf-8")
    marker = "\n## Description\n"
    idx = text.find(marker)
    if idx < 0:
        return
    body_start = idx + len(marker)
    end = text.find("\n## ", body_start)
    if end < 0:
        end = len(text)
    new_body = "\n" + description.rstrip() + "\n\n"
    ctx.write_text(text[:body_start] + new_body + text[end:], encoding="utf-8")

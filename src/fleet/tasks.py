"""Task workspace commands: new / list / info / sync / end.

A task is a folder under TASKS_ROOT containing:
  - one git worktree per chosen repo (each on a fresh `task/<fleet>/<name>` branch)
  - context.md (free-form notes seed)
  - scratch/ (free-form scratch dir)
  - task.json (manifest read by all commands)

Manifest schema::

    {
      "name": "...",
      "branch": "task/<fleet>/<name>",
      "created_at": "ISO timestamp",
      "description": "...",
      "repos": [
        {
          "name": "...",          # leaf repo dir name
          "group": "..." or null, # "/"-separated group_path
          "canonical_path": "...",
          "worktree_path": "..."
        }, ...
      ]
    }
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fleet import git_ops
from fleet.config import (
    FleetError,
    active_fleet_name,
    archive_root,
    cyan,
    dim,
    green,
    red,
    tasks_root,
    yellow,
)
from fleet.discovery import RepoInfo, discover_repos

# Filesystem-safe AND valid as a git branch name suffix. The regex enforces
# the character set; `_validate_task_name` additionally rules out the
# git-ref-format edge cases the regex can't express (`..`, leading/trailing
# `.`, `.lock` suffix, `@{`).
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

def load_manifest(task_dir: Path) -> dict | None:
    manifest_path = task_dir / "task.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"warning: malformed task.json in {task_dir.name}: {e}",
              file=sys.stderr)
        return None


def save_manifest(task_dir: Path, manifest: dict) -> None:
    manifest_path = task_dir / "task.json"
    # Atomic write: stage to .tmp then replace, so a crash mid-write can
    # never leave task.json half-written (which would brick `task end`).
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, manifest_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Validation / resolution
# ---------------------------------------------------------------------------

def _validate_task_name(name: str) -> None:
    if not _TASK_NAME_RE.match(name):
        raise FleetError(
            f"Invalid task name '{name}'. "
            "Use letters, digits, '.', '_', '-' (1-64 chars, must start "
            "with a letter or digit)."
        )
    # git ref rules the regex can't express. We don't need to invoke git for
    # these — they're cheap to check ourselves and let us fail before any
    # filesystem mutation.
    if (".." in name
            or name.startswith(".") or name.endswith(".")
            or name.endswith(".lock") or "@{" in name):
        raise FleetError(
            f"Invalid task name '{name}'. Git would refuse the resulting "
            "branch (no '..', no leading/trailing '.', no '.lock' suffix, "
            "no '@{')."
        )


def _task_branch(name: str) -> str:
    """Branch name created/managed by fleet for a task.

    Namespaced by the active fleet so two fleets that share a physical
    canonical repo can each have a task with the same name without their
    git branches colliding. Format: ``task/<fleet>/<task>``.

    Both fleet names and task names are validated to be git-ref-safe
    (alphanum + ``.`` ``_`` ``-``), so the resulting ref is always valid.
    """
    fleet = active_fleet_name()
    if fleet is None:
        # Should never reach here — cli.main() pins the fleet first.
        raise FleetError(
            "No active fleet; cannot derive task branch name. This is an "
            "internal error."
        )
    return f"task/{fleet}/{name}"


def _require_branch(manifest: dict, workspace: Path) -> str:
    """Return manifest['branch'] or raise FleetError if missing/blank."""
    branch = manifest.get("branch")
    if not isinstance(branch, str) or not branch:
        raise FleetError(
            f"task.json in {workspace} is missing the required 'branch' "
            f"field. Refusing to guess — fix the manifest manually."
        )
    return branch


def _resolve_repo(token: str, all_repos: list[RepoInfo]) -> RepoInfo:
    """Resolve a `--repos` token to exactly one RepoInfo.

    Accepts bare 'name', or 'group/path/name' (slashes anywhere in group).
    Raises FleetError with did-you-mean suggestions on miss / ambiguity, or
    with a clear message when the repo is registry-disabled.
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
                token, sorted({r.name for r in all_repos}), n=3, cutoff=0.6
            )
            hint = (f" Did you mean: {', '.join(suggestions)}?"
                    if suggestions else "")
            raise FleetError(f"Unknown repo '{token}'.{hint}")
        if len(matches) > 1:
            disambig = ", ".join(m.display_name for m in matches)
            raise FleetError(
                f"Ambiguous repo name '{token}' — exists in multiple groups. "
                f"Disambiguate with one of: {disambig}"
            )
        chosen = matches[0]

    if not chosen.enabled:
        raise FleetError(
            f"Repo '{chosen.display_name}' is disabled in fleet.json "
            f"(sync:false or excluded). Re-enable it there before using it "
            f"as a task target."
        )
    return chosen


def _manifest_repo_paths(entry: dict) -> tuple[Path, Path]:
    """Return (worktree_path, canonical_path) from a manifest repo entry.

    Centralises the `.get` defaulting so a partially-written manifest can
    never raise `KeyError` deep inside `cmd_end` / `cmd_sync` / `cmd_info`.
    """
    return (
        Path(entry.get("worktree_path", "")),
        Path(entry.get("canonical_path", "")),
    )


def _now_iso() -> str:
    """Timezone-aware ISO-8601 timestamp for manifest fields."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Per-repo task setup helpers
# ---------------------------------------------------------------------------

def _prepare_canonical(repo: RepoInfo, no_pull: bool) -> str:
    """Fetch (and FF-pull when safe) the canonical repo. Return default branch.

    With ``no_pull=True`` we skip both the network fetch and the local pull,
    and use the offline-only default-branch detector so we don't surprise the
    user with a hidden network round-trip.
    """
    label = f"[{repo.name}]"

    if no_pull:
        default = git_ops.detect_default_branch(repo.path, offline=True)
        print(f"  {label} default branch: {default} (skipping fetch + pull)")
        return default

    print(f"  {label} fetching...")
    fetch = git_ops.fetch_prune(repo.path)
    if not fetch.ok and not git_ops.is_warning_only(fetch):
        print(f"  {label} WARN: fetch failed:\n    {fetch.stderr.strip()}\n"
              f"    (continuing with whatever origin/* is cached)")

    default = git_ops.detect_default_branch(repo.path)

    if git_ops.is_dirty(repo.path):
        print(f"  {label} canonical has local changes; "
              f"skipping local pull (worktree will branch from origin/{default})")
        return default

    cur = git_ops.current_branch(repo.path)
    on_default = cur == default
    if on_default:
        pull = git_ops.pull_ff_only(repo.path, default)
        if pull.ok or git_ops.is_warning_only(pull):
            print(f"  {label} pulled latest on {default}")
        else:
            print(f"  {label} WARN: pull --ff-only failed:\n    "
                  f"{pull.stderr.strip()}\n"
                  f"    (worktree will branch from origin/{default})")
    else:
        # Update the local default ref to match origin without checking out.
        upd = git_ops.run_git("fetch", "origin", f"{default}:{default}",
                              cwd=repo.path, check=False)
        if upd.ok:
            print(f"  {label} updated local {default} from origin")
        else:
            print(f"  {label} WARN: could not fast-forward local {default}:\n"
                  f"    {upd.stderr.strip()}\n"
                  f"    (worktree will branch from origin/{default})")
    return default


def _add_worktree(repo: RepoInfo, task_name: str, default_branch: str,
                  task_workspace: Path) -> Path:
    """Create a worktree for `repo` under the task workspace. Returns its path."""
    branch = _task_branch(task_name)
    wt_path = task_workspace / repo.name
    label = f"[{repo.name}]"

    if wt_path.exists():
        raise FleetError(f"Worktree path already exists: {wt_path}")

    existing = git_ops.run_git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
        cwd=repo.path, check=False,
    )
    if existing.ok:
        raise FleetError(
            f"Branch '{branch}' already exists in {repo.name}. "
            f"Pick a different task name or delete the branch with "
            f"`git -C \"{repo.path}\" branch -D {branch}`."
        )

    # Also reject if the branch already exists on origin (e.g. a previous task
    # with the same name was pushed before being ended). Otherwise we'd create
    # a fresh local branch from origin/<default> that diverges from the remote
    # one and the first non-force push would be rejected.
    origin_existing = git_ops.run_git(
        "show-ref", "--verify", "--quiet",
        f"refs/remotes/origin/{branch}",
        cwd=repo.path, check=False,
    )
    if origin_existing.ok:
        raise FleetError(
            f"Branch '{branch}' already exists on origin in {repo.name}. "
            f"Pick a different task name, or delete it from origin first "
            f"with `git -C \"{repo.path}\" push origin --delete {branch}`."
        )

    # `--no-track` is critical: without it, branching from `origin/<default>`
    # silently sets the new branch's upstream to `origin/<default>`. That
    # means the first plain `git push` either errors confusingly (push.default
    # = simple) or, worse, pushes to the default branch on origin
    # (push.default = upstream). With `--no-track`, the branch has no upstream
    # and the user must explicitly `git push -u origin <branch>` the first
    # time — which is what we want.
    git_ops.run_git(
        "worktree", "add", "--no-track", str(wt_path),
        "-b", branch, f"origin/{default_branch}",
        cwd=repo.path,
    )
    print(f"  {label} worktree -> {wt_path} (branch {branch})")
    return wt_path


# ---------------------------------------------------------------------------
# Helpers shared with `fleet repos`
# ---------------------------------------------------------------------------

def cmd_repos(_args: argparse.Namespace) -> int:
    """List every repo on disk, grouped by parent path, with disabled markers."""
    repos = discover_repos()
    if not repos:
        print("No git repos found under the configured Repos root.")
        return 0

    by_group: dict[str, list[RepoInfo]] = {}
    for r in repos:
        by_group.setdefault(r.group_path, []).append(r)

    # Top-level (group_path == "") first, then groups alphabetically.
    ordered = sorted(by_group.keys(), key=lambda g: (g != "", g))
    for group in ordered:
        label = group if group else "(top-level)"
        print(f"\n{label}:")
        for r in by_group[group]:
            marker = ""
            if not r.enabled:
                marker = dim("  (disabled)")
            elif not r.in_registry:
                marker = dim("  (not in registry)")
            print(f"  {r.name}{marker}")

    name_counts: dict[str, int] = {}
    for r in repos:
        name_counts[r.name] = name_counts.get(r.name, 0) + 1
    dupes = sorted(n for n, c in name_counts.items() if c > 1)
    if dupes:
        print(f"\nNote: {len(dupes)} repo name(s) appear in more than one "
              f"group: {', '.join(dupes)}")
        print("      Use 'group/path/name' syntax with --repos to disambiguate.")

    enabled = sum(1 for r in repos if r.enabled)
    print(f"\nTotal: {len(repos)} repos ({enabled} enabled) across "
          f"{len(by_group)} group(s).")
    return 0


# ---------------------------------------------------------------------------
# task list / info
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def cmd_list(args: argparse.Namespace) -> int:
    root = tasks_root()
    if not root.is_dir():
        print(f"No tasks yet for fleet '{active_fleet_name()}' "
              f"({root} doesn't exist).")
        return 0

    rows: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        manifest = load_manifest(entry)
        if manifest is None:
            rows.append({
                "name": entry.name,
                "branch": "-",
                "created": "(no manifest)",
                "repos": "-",
                "status": "?",
            })
            continue
        created = manifest.get("created_at", "?")
        branch = manifest.get("branch", "?")
        repos_field = manifest.get("repos", [])
        repo_names = [r["name"] if isinstance(r, dict) else str(r)
                      for r in repos_field]

        status_parts: list[str] = []
        if not args.quick:
            dirty_count = 0
            unpushed_total = 0
            unpushed_unknown = 0
            missing = 0
            for r in repos_field:
                if not isinstance(r, dict):
                    continue
                wt = Path(r.get("worktree_path", ""))
                if not wt.is_dir():
                    missing += 1
                    continue
                if git_ops.is_dirty(wt):
                    dirty_count += 1
                n = git_ops.unpushed_count(wt, branch)
                if n is None:
                    unpushed_unknown += 1
                elif n > 0:
                    unpushed_total += n
            if dirty_count:
                status_parts.append(red(f"dirty:{dirty_count}"))
            if unpushed_total:
                status_parts.append(yellow(f"unpushed:{unpushed_total}"))
            if unpushed_unknown:
                status_parts.append(dim(f"not-pushed:{unpushed_unknown}"))
            if missing:
                status_parts.append(red(f"missing:{missing}"))
            if not status_parts:
                status_parts.append(green("clean"))

        rows.append({
            "name": entry.name,
            "branch": branch,
            "created": created,
            "repos": ", ".join(repo_names) or "-",
            "status": "  ".join(status_parts) if status_parts else "",
        })

    if not rows:
        print(f"No active tasks under {root}.")
        return 0

    name_w = max(len(r["name"]) for r in rows)
    branch_w = max(len(r["branch"]) for r in rows)
    for r in rows:
        branch_padded = f"{r['branch']:<{branch_w}}"
        line = (f"{r['name']:<{name_w}}  "
                f"{cyan(branch_padded)}  "
                f"created={r['created']}  "
                f"repos=[{r['repos']}]")
        if r["status"]:
            line += f"  {r['status']}"
        print(line)
    print(f"\nTotal: {len(rows)} task(s) in fleet '{active_fleet_name()}'.")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    name: str = args.name
    _validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")
    manifest = load_manifest(workspace)
    if manifest is None:
        raise FleetError(f"task.json missing or unreadable in {workspace}.")

    branch = _require_branch(manifest, workspace)
    print(cyan(f"Task: {name}"))
    print(f"  branch:      {branch}")
    print(f"  created:     {manifest.get('created_at', '?')}")
    print(f"  workspace:   {workspace}")
    desc = (manifest.get("description") or "").strip()
    if desc:
        first = desc.splitlines()[0]
        if len(first) > 80:
            first = first[:77] + "..."
        print(f"  description: {first}")

    scratch = workspace / "scratch"
    if scratch.is_dir():
        files = [p for p in scratch.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        print(f"  scratch:     {len(files)} file(s), {_human_size(total)}")

    repos_field = manifest.get("repos", [])
    if not repos_field:
        print("\n(no repos in manifest)")
        return 0

    print(f"\n{cyan('Repos:')}")
    for r in repos_field:
        if not isinstance(r, dict):
            raise FleetError(
                f"Malformed task.json in {workspace}: 'repos' entry is not "
                f"an object: {r!r}"
            )
        name_str = r.get("name", "?")
        group = r.get("group")
        loc = f"{group}/{name_str}" if group else name_str
        wt, canonical = _manifest_repo_paths(r)
        print(f"  - {loc}")
        print(f"      canonical: {canonical}")
        print(f"      worktree:  {wt}")
        if not wt.is_dir():
            print(f"      status:    {red('worktree directory missing')}")
            continue
        flags: list[str] = []
        if git_ops.is_dirty(wt):
            flags.append(red("dirty"))
        n = git_ops.unpushed_count(wt, branch)
        if n is None:
            flags.append(yellow("never pushed"))
        elif n > 0:
            flags.append(yellow(f"{n} unpushed"))
        else:
            flags.append(green("in sync with origin"))
        print(f"      status:    {', '.join(flags)}")
        head = git_ops.run_git("rev-parse", "--abbrev-ref", "HEAD",
                               cwd=wt, check=False)
        if head.ok:
            cur = head.stdout.strip()
            log = git_ops.run_git("log", "-1", "--format=%h %s",
                                  cwd=wt, check=False)
            if log.ok:
                print(f"      head:      {cur} :: {log.stdout.strip()}")
    return 0


# ---------------------------------------------------------------------------
# task new
# ---------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    name: str = args.name
    _validate_task_name(name)

    root = tasks_root()
    workspace = root / name
    if workspace.exists():
        raise FleetError(
            f"Task '{name}' already exists at {workspace}. "
            f"Pick a different name or `fleet task end {name}` first."
        )

    repo_tokens = [t.strip() for t in args.repos.split(",") if t.strip()]
    if not repo_tokens:
        raise FleetError("--repos requires at least one repo name.")

    all_repos = discover_repos()
    if not all_repos:
        raise FleetError("No repos discovered under the configured Repos root.")

    seen: set[tuple[str, str]] = set()
    chosen: list[RepoInfo] = []
    for tok in repo_tokens:
        repo = _resolve_repo(tok, all_repos)
        key = (repo.group_path, repo.name)
        if key in seen:
            print(f"note: ignoring duplicate '{tok}'")
            continue
        seen.add(key)
        chosen.append(repo)

    # Worktree paths are `<workspace>/<repo.name>`, so two repos that resolve
    # to the same leaf name (e.g. `foo` and `bar/foo`) would collide on disk
    # halfway through scaffolding. Catch it before any mutation.
    by_leaf: dict[str, RepoInfo] = {}
    for r in chosen:
        prior = by_leaf.get(r.name)
        if prior is not None:
            raise FleetError(
                f"Two selected repos share leaf name '{r.name}': "
                f"'{prior.display_name}' and '{r.display_name}'. "
                f"Their worktrees would collide at {workspace / r.name}. "
                f"Pick a different combination of repos."
            )
        by_leaf[r.name] = r

    print(f"Creating task '{name}' with {len(chosen)} repo(s):")
    for r in chosen:
        print(f"  - {r.display_name}")
    print()

    if args.dry_run:
        print(yellow("--dry-run: would pull each canonical (default branch), "
                     "then create one worktree per repo at:"))
        for r in chosen:
            print(f"  {workspace / r.name}  (branch {_task_branch(name)})")
        print(f"  + {workspace / 'context.md'}")
        print(f"  + {workspace / 'scratch'}")
        print(f"  + {workspace / 'task.json'}")
        return 0

    # Compute the branch name (and resolve the active fleet) BEFORE any
    # filesystem mutation so a misconfiguration doesn't leave behind an
    # empty workspace dir.
    branch = _task_branch(name)
    description = args.description

    # workspace.mkdir(parents=True) creates the per-fleet TASKS_ROOT/<fleet>
    # directory if missing; no separate mkdir needed.
    workspace.mkdir(parents=True, exist_ok=False)

    created_worktrees: list[tuple[RepoInfo, Path]] = []
    try:
        for repo in chosen:
            default = _prepare_canonical(repo, no_pull=args.no_pull)
            wt = _add_worktree(repo, name, default, workspace)
            created_worktrees.append((repo, wt))

        context_md = workspace / "context.md"
        context_md.write_text(
            f"# {name}\n\n"
            f"_Branch:_ `{branch}`\n"
            f"_Created:_ {_now_iso()}\n\n"
            f"## Description\n\n{description}\n\n"
            f"## Repos\n\n"
            + "\n".join(
                f"- `{r.name}`"
                + (f"  _(group: {r.group_path})_" if r.group_path else "")
                for r in chosen
            )
            + "\n\n## Notes\n\n"
            "_Use this file to capture acceptance criteria, decisions, links._\n",
            encoding="utf-8",
        )
        (workspace / "scratch").mkdir(exist_ok=True)

        manifest = {
            "name": name,
            "branch": branch,
            "created_at": _now_iso(),
            "description": description,
            "repos": [
                {
                    "name": r.name,
                    "group": r.group_path or None,
                    "canonical_path": str(r.path),
                    "worktree_path": str(workspace / r.name),
                }
                for r in chosen
            ],
        }
        save_manifest(workspace, manifest)
    except BaseException:
        # Catch BaseException so Ctrl-C also triggers rollback.
        print("\nERROR during scaffolding — rolling back created worktrees...",
              file=sys.stderr)
        rollback_branch = branch
        for repo, wt in created_worktrees:
            git_ops.run_git("worktree", "remove", "--force", str(wt),
                            cwd=repo.path, check=False)
            git_ops.run_git("branch", "-D", rollback_branch,
                            cwd=repo.path, check=False)
        try:
            shutil.rmtree(workspace, ignore_errors=True)
        except Exception:
            pass
        raise

    print(f"\nTask ready: {workspace}")
    print(f"Next: fleet open {name}")
    return 0


# ---------------------------------------------------------------------------
# task end
# ---------------------------------------------------------------------------

def cmd_end(args: argparse.Namespace) -> int:
    name: str = args.name
    _validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")

    manifest = load_manifest(workspace)
    if manifest is None:
        raise FleetError(
            f"task.json missing or unreadable in {workspace}. "
            f"Refusing to clean up — inspect the folder manually."
        )

    branch = _require_branch(manifest, workspace)
    repos_in_manifest = manifest.get("repos", [])

    dirty: list[str] = []
    unpushed_warnings: list[str] = []
    live_worktrees: list[dict] = []
    for r in repos_in_manifest:
        if not isinstance(r, dict):
            continue
        wt, _ = _manifest_repo_paths(r)
        if not wt.is_dir():
            print(f"note: worktree '{r.get('name', '?')}' already gone ({wt})")
            continue
        live_worktrees.append(r)
        if git_ops.is_dirty(wt):
            dirty.append(r.get("name", "?"))
        n = git_ops.unpushed_count(wt, branch)
        if n is None:
            unpushed_warnings.append(
                f"  {r.get('name', '?')}: branch '{branch}' was never pushed"
            )
        elif n > 0:
            unpushed_warnings.append(
                f"  {r.get('name', '?')}: {n} unpushed commit(s) on '{branch}'"
            )

    if dirty and not args.force:
        raise FleetError(
            "Refusing to end — uncommitted changes in: "
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

    ARCHIVE_ROOT_LOCAL = archive_root()
    ARCHIVE_ROOT_LOCAL.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = ARCHIVE_ROOT_LOCAL / f"{name}-{stamp}.zip"
    # Collision guard for the rare case of two `task end`s within the same
    # second (script-driven, fast retries): never silently overwrite an
    # existing archive.
    if archive_path.exists():
        i = 2
        while True:
            candidate = ARCHIVE_ROOT_LOCAL / f"{name}-{stamp}-{i}.zip"
            if not candidate.exists():
                archive_path = candidate
                break
            i += 1
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for top in ("task.json", "context.md"):
            f = workspace / top
            if f.is_file():
                zf.write(f, arcname=top)
        scratch = workspace / "scratch"
        if scratch.is_dir():
            for item in scratch.rglob("*"):
                if item.is_file():
                    zf.write(item, arcname=str(item.relative_to(workspace)))
    print(f"Archived metadata -> {archive_path}")

    failures: list[str] = []
    for r in live_worktrees:
        wt, cwd = _manifest_repo_paths(r)
        flag = ["--force"] if args.force else []
        proc = git_ops.run_git("worktree", "remove", *flag, str(wt),
                               cwd=cwd, check=False)
        if not proc.ok:
            failures.append(f"  {r.get('name', '?')}: {proc.stderr.strip()}")
        else:
            print(f"  removed worktree {r.get('name', '?')}")

    for r in repos_in_manifest:
        if not isinstance(r, dict):
            continue
        _, cwd = _manifest_repo_paths(r)
        if cwd.is_dir():
            git_ops.run_git("worktree", "prune", cwd=cwd, check=False)

    if failures:
        print("\nWARN: some worktrees could not be removed:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        print(f"\nFix those manually, then delete the workspace folder:\n"
              f"  {workspace}", file=sys.stderr)
        return 2

    try:
        shutil.rmtree(workspace)
        print(f"Removed workspace {workspace}")
    except OSError as e:
        raise FleetError(
            f"Couldn't remove {workspace}: {e}\n"
            f"(VS Code or another process may still be holding it open.)"
        )

    print(f"\nTask '{name}' ended. Branch '{branch}' remains in each "
          f"canonical repo (and on origin if pushed).")
    return 0


# ---------------------------------------------------------------------------
# task sync
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    name: str = args.name
    _validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(f"No such task: {workspace}")
    manifest = load_manifest(workspace)
    if manifest is None:
        raise FleetError(f"task.json missing or unreadable in {workspace}.")

    branch = _require_branch(manifest, workspace)
    repos_in_manifest = manifest.get("repos", [])
    if not repos_in_manifest:
        print("(manifest has no repos)")
        return 0

    print(f"Syncing task '{name}' (branch {branch}):")
    any_failure = False
    for r in repos_in_manifest:
        if not isinstance(r, dict):
            continue
        wt, _ = _manifest_repo_paths(r)
        label = f"  [{r.get('name', '?')}]"
        if not wt.is_dir():
            print(f"{label} worktree missing on disk; skipping")
            any_failure = True
            continue
        if git_ops.is_dirty(wt):
            print(f"{label} uncommitted changes; skipping pull "
                  f"(commit/stash, then re-run)")
            any_failure = True
            continue

        fetch = git_ops.fetch_prune(wt)
        if not fetch.ok and not git_ops.is_warning_only(fetch):
            print(f"{label} fetch failed:\n    {fetch.stderr.strip()}")
            any_failure = True
            continue

        # After fetch, the local origin/<branch> ref is current. Use it
        # rather than another network round-trip via ls-remote.
        rp = git_ops.run_git("rev-parse", "--verify", "--quiet",
                             f"refs/remotes/origin/{branch}",
                             cwd=wt, check=False)
        if not rp.ok or not rp.stdout.strip():
            print(f"{label} fetched (branch '{branch}' not on origin yet)")
            continue

        pull = git_ops.pull_ff_only(wt, branch)
        if pull.ok or git_ops.is_warning_only(pull):
            summary = (pull.stdout or "").strip().splitlines()
            tail = summary[-1] if summary else "ok"
            print(f"{label} {tail}")
        else:
            print(f"{label} pull --ff-only failed:\n    {pull.stderr.strip()}")
            any_failure = True

    return 2 if any_failure else 0


# ---------------------------------------------------------------------------
# task path  (for shell integration: `cd $(fleet path foo)`)
# ---------------------------------------------------------------------------

def cmd_path(args: argparse.Namespace) -> int:
    """Print the absolute path to a task workspace and exit 0.

    Used by Fleet.psm1's `fleet open` to compute the cd target without
    re-implementing fleet config parsing in PowerShell. Errors go to stderr
    so callers can capture stdout cleanly: ``cd $(fleet path foo)``.
    """
    name: str = args.name
    _validate_task_name(name)
    workspace = tasks_root() / name
    if not workspace.is_dir():
        raise FleetError(
            f"No such task in fleet '{active_fleet_name()}': {workspace}"
        )
    print(str(workspace))
    return 0

# In-place task editing — implementation plan

Add four mutating subcommands so users can edit a live task instead of `task end` +
`task new`:

- `fleet task add-repo <task> --repos a,b[,group/c] [--no-pull] [--dry-run]`
- `fleet task remove-repo <task> --repos a,b [--force] [--dry-run]`
- `fleet task rename <old> <new>`
- `fleet task edit <task> [--description TEXT | --description-file PATH]`
  (with `--description-file -` reading stdin)

## 1. Background & current state

Tasks live at `<TASKS_ROOT>/<fleet>/<task>/` with `task.json`, `context.md`, `scratch/`,
and one `git worktree` per repo on branch `task/<fleet>/<name>`. The code we touch:

- `src/fleet/tasks/lifecycle.py` — `_prepare_canonical`, `_add_worktree`, `cmd_new`,
  `cmd_end`. Source of the rollback-on-`BaseException` pattern and the `--force`
  dirty/unpushed handling.
- `src/fleet/tasks/manifest.py` — `Manifest` dataclass + atomic `save()` (tmp +
  `os.replace`). `RepoEntry(name, group, canonical_path, worktree_path)`.
- `src/fleet/tasks/validation.py` — `validate_task_name`, `task_branch(name)` (returns
  `task/<fleet>/<name>`), `resolve_repo(token, all_repos)`.
- `src/fleet/tasks/inspect.py` — read-only `list/info/path/sync`.
- `src/fleet/tasks/__init__.py` — argparse `register(subparsers, fleet_arg)`.
- `src/fleet/git_ops.py` — sole subprocess site for git.
- `src/fleet/completion.py` — `POS_PROVIDERS`, `OPT_PROVIDERS`, comma-aware `--repos`
  branch with `DIRECTIVE_NOSPACE`.
- `src/fleet/cli.py` — dispatch + `FleetError` → exit code.

Invariants the new commands preserve:

1. `_add_worktree` uses `worktree add --no-track ... -b <branch> origin/<default>`.
   `--no-track` is non-negotiable (first push must be explicit `-u`).
2. `_add_worktree` rejects when the branch already exists in the canonical OR on origin.
3. Per-fleet branch namespacing via `task_branch()`. `rename` recomputes via
   `task_branch(new)`, never string-munges.
4. Leaf-name collision check (worktree dir is `<workspace>/<repo.name>`).
5. Manifest writes go through `Manifest.save` (atomic).
6. Rollback-on-`BaseException`: only undo what THIS invocation created.
7. Exit codes: 0 ok, 1 hard `FleetError`, 2 partial failure.
8. git only invoked from `git_ops.py`.
9. `cmd_end` dirty/unpushed refusal unless `--force`.
10. `validate_task_name` ref-safety rules.

## 2. Command surface & UX

All new subparsers `parents=[fleet_arg]`. All call `validate_task_name` first.

### `task add-repo <task> --repos a,b[,grp/c] [--no-pull] [--dry-run]`
- 0 success; 1 on unknown task/repo, repo already in task, leaf collision, branch-exists
  guards. Manifest untouched on any failure (full BaseException rollback).
- Success:
  ```
  Adding 1 repo(s) to task 'bug-123' (branch task/main/bug-123):
    [gamma] default branch: main (skipping fetch + pull)
    [gamma] worktree -> .../bug-123/gamma (branch task/main/bug-123)
  Updated task.json (now 3 repo(s)).
  ```

### `task remove-repo <task> --repos a,b [--force] [--dry-run]`
- 0 success; 2 if some worktree removals fail (mirrors `cmd_end`); 1 on refusal-on-dirty
  without `--force` or unknown repo.
- Only drops manifest entries for successfully torn-down worktrees.

### `task rename <old> <new>`
- 0 success; 1 on invalid new name, new exists, old missing, or branch-rename failure
  after attempted rollback.
- No `--dry-run` in v1.

### `task edit <task> [--description TEXT | --description-file PATH]`
- Mutually exclusive group. `--description-file -` reads stdin.

### Tab completion
- `_repo_names_not_in_task(words)` for `add-repo --repos` (filters out current members).
- `_repo_names_in_task(words)` for `remove-repo --repos` (current members only).
- `POS_PROVIDERS` for `("task","add-repo")`, `("task","remove-repo")`, `("task","rename")`
  (old positional), `("task","edit")` → all `_task_names`.
- `--repos` branch in `_complete_option_value` inspects `words` to pick the right
  provider per subcommand. `DIRECTIVE_NOSPACE` preserved.
- rename `<new>` positional has no provider (free text).

## 3. Module-by-module changes

- **`tasks/worktree.py` (new)** — extract `prepare_canonical(repo, no_pull)` and
  `add_worktree(repo, name, default_branch, task_workspace)` verbatim from
  `lifecycle.py` for reuse by both lifecycle and edit. (No back-compat aliases — repo
  is pre-1.0.)
- **`tasks/edit.py` (new)** — `cmd_add_repo`, `cmd_remove_repo`, `cmd_rename`,
  `cmd_edit`.
- **`tasks/manifest.py`** — add `Manifest.add_repos`, `remove_repos`, `rename`.
- **`tasks/validation.py`** — add `require_repo_in_task` helper.
- **`git_ops.py`** — add `rename_branch(repo_path, old, new) -> GitResult`.
- **`tasks/__init__.py`** — register the four new subparsers.
- **`completion.py`** — add the two filtered providers + `POS_PROVIDERS` entries; tweak
  the `--repos` branch in `_complete_option_value`.
- **README.md** — command reference table + concepts updates.

## 4. Failure modes, rollback & atomicity

**add-repo.** Track `created: list[(repo, wt, branch)]` during the per-repo loop. On any
exception (including `KeyboardInterrupt`), `git worktree remove --force` + `git branch
-D` each entry, then re-raise. Manifest is saved only after the loop succeeds — never
half-written.

**remove-repo.** Refuse on dirty/unpushed unless `--force` (reuse `cmd_end`'s logic). Run
`git worktree remove` per repo, collect failures (Windows file locks). Manifest drops
ONLY the successfully removed entries; failed ones stay. Worktree dir already gone →
treat as success, drop cleanly. rc 2 if any failure, else 0.

**rename (riskiest).** Ordered, mostly-reversible strategy:

1. Validate `new` (incl. `validate_task_name`); assert new workspace dir doesn't exist;
   load manifest; compute `new_branch = task_branch(new)` (active fleet already pinned).
2. Walk canonicals, `git_ops.rename_branch(canonical, old_branch, new_branch)`. Track
   succeeded. If a canonical no longer has the old branch (manual delete), warn and
   continue. On ANY OTHER failure → reverse-rename every succeeded canonical, raise
   `FleetError`.
3. Only after step 2 finishes cleanly: `shutil.move` workspace dir to new path. If that
   fails → reverse-rename all branches, raise.
4. Rewrite manifest in the new location (`Manifest.rename` + `save`) and patch the first
   `# <old>` header in `context.md`. These are local + atomic.

Accepted residual failure: a crash between step 2 and step 4 leaves branches as `new`
but the workspace+manifest as `old`. Recovery: re-run `fleet task rename old new`. Step
2 detects the missing old branch in every canonical and warns; steps 3 + 4 then complete
the job. Documented in `--help` text.

**edit.** Single field change + atomic save. `context.md` rewrite is best-effort
(suppressed); manifest is the source of truth.

## 5. Edge cases to test

- `add-repo` for a repo already in the task → `FleetError`, no mutation.
- `add-repo` whose new leaf collides with an existing or sibling-new worktree dir.
- `add-repo` where the task branch already exists on origin for that canonical.
- `add-repo` with duplicate tokens in one `--repos` → dedupe (note + ignore).
- `remove-repo` of the last repo → allowed, warn "task now has no repos".
- `remove-repo` of a repo whose worktree dir is already gone → clean drop, rc 0.
- `remove-repo` of a repo not in the task → `FleetError` with member-name hint.
- `remove-repo` dirty without `--force` → refuse rc 1; `--force` → warn + remove.
- `rename` to the same name → no-op rc 0 (friendlier than error).
- `rename` where `<new>` collides with an existing workspace → `FleetError`.
- `rename` while a worktree is dirty → allowed (rename doesn't touch working tree).
- `rename` where one canonical lost the old branch → warn and continue.
- `rename` with an invalid `<new>` → `validate_task_name` rejects.
- `edit` with both `--description` and `--description-file` → argparse rejects.
- `edit --description-file -` → reads stdin.
- Concurrency: no per-task lock today. Out of v1 scope (see Open Questions).

## 6. Test plan

### unit (`tests/unit`)
- `test_tasks_manifest.py`: `add_repos` round-trip via save→load; `remove_repos` returns
  dropped entries + removes them; `rename` recomputes name/branch/`worktree_path`.
  Assert `Manifest.load(ws).branch == "task/<fleet>/<new>"`.
- `test_tasks_validation.py`: `require_repo_in_task` raises with member-name hint when
  the token isn't a member.
- `test_completion.py`: monkeypatch `Manifest.load` + `discover_repos`; assert
  `add-repo <t> --repos <tab>` excludes members and returns `DIRECTIVE_NOSPACE`;
  `remove-repo <t> --repos <tab>` returns exactly the members.

### integration (`tests/integration`, real bare remotes)
- `test_task_edit.py` (new):
  - `add-repo` happy: assert `(ws/"gamma").is_dir()`; branch `task/<fleet>/<t>` checked
    out; manifest grows by one.
  - `add-repo` duplicate → `pytest.raises(FleetError)`; manifest unchanged.
  - `add-repo` rollback: monkeypatch `git_ops.run_git` to fail on 2nd `worktree add`;
    assert first added worktree removed, branch `-D`'d, manifest at original count.
  - `add-repo` `--dry-run`: no fs change; plan printed.
  - `remove-repo` happy: worktree dir gone, canonical pruned, manifest shrinks.
  - `remove-repo` dirty refusal + `--force` path.
  - `remove-repo` last repo → warn, manifest.repos == [].
  - `remove-repo` missing worktree dir → clean drop.
  - `rename` happy: new ws exists, old gone, each canonical's branch is
    `task/<fleet>/<new>`, manifest paths repointed, context.md header rewritten.
  - `rename` branch-failure rollback: monkeypatch run_git to fail on 2nd `branch -m`;
    first canonical's branch renamed BACK to old; ws not moved.
  - `rename` new-exists → `FleetError`.
  - `rename` same-name → no-op rc 0.
  - `rename` canonical missing old branch → warn, others rename, manifest still updates.
  - `rename` cross-fleet correctness (fleet `demo` → `task/demo/<new>`).
  - `edit`: TEXT and stdin paths; assert manifest.description + context.md updated.

### e2e (`tests/e2e`, subprocess `python -m fleet`)
- One happy path each for the four new commands; `--dry-run` for add/remove.

### completion subprocess (`tests/e2e/test_completion_subprocess.py`)
- `fleet __complete -- task add-repo <t> --repos` returns `:4` and excludes members.
- `fleet __complete -- task remove-repo <t> --repos` returns only members.

## 7. Backward compatibility & migration

- **Manifest schema**: NO new fields. Old `task.json` loads unchanged.
- **Shell wrappers**: no new shell-mutating command. `rename` does NOT cd the parent
  shell — if the user is inside the old workspace when rename runs, their `$PWD` goes
  stale. The handler prints a hint line; auto-cd is left to a possible future wrapper
  enhancement.
- **Completion script regen**: NONE. Shell scripts shell out to `fleet __complete`; new
  subcommands are auto-discovered via argparse introspection.

## 8. Implementation order

Repo stays green at each step.

1. Extract `prepare_canonical` + `add_worktree` into `tasks/worktree.py`; update
   `lifecycle.py` to import them; run the suite.
2. `Manifest.add_repos/remove_repos/rename` + `require_repo_in_task` + unit tests.
3. `git_ops.rename_branch`.
4. Ship `add-repo`: handler, argparse wiring, integration tests.
5. Ship `remove-repo`.
6. Ship `rename`.
7. Ship `edit`.
8. Completion providers + `POS_PROVIDERS`/`OPT_PROVIDERS` wiring + completion tests.
9. README updates.

## 9. Open questions

1. `rename` to the same name: no-op (rc 0, recommended) vs error?
2. Add a per-task lock in v1 (mirroring `_config_lock`)? Recommend defer.
3. `context.md` rewrite for `edit` / `add-repo` / `rename`: best-effort section replace
   is acceptable, or manifest-only? Recommend best-effort with suppressed failures.
4. `remove-repo` of the last repo: warn-and-allow (recommended) vs require `--force` vs
   block?
5. `--dry-run` scope: add-repo + remove-repo yes; rename defer; edit no.
6. Stretch goals (`task move`, `task replace-repo`, `$EDITOR` edit) confirmed out of v1.

## Stretch goals (not in v1)

- `task move <task> --to <fleet>` — would validate canonicals in the target fleet,
  rename branch to `task/<new-fleet>/<task>`, move the workspace under the new fleet's
  `tasks_root`. Higher risk; defer.
- `task replace-repo <task> <old> <new>` — sugar = remove + add. Trivial once both
  exist; defer to keep v1 tight.
- `task edit` opening `task.json` in `$EDITOR` with schema-validation on save — needs a
  re-validation round-trip; defer.

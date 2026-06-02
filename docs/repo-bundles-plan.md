# Repo bundles — implementation plan

Add named, ordered repo aliases ("bundles") so users stop retyping
`--repos foo,bar,baz/qux`. A bundle is referenced with an `@name` sigil
anywhere `--repos` is accepted. Bundles are **per-fleet**, stored in a
sibling `bundles.json` next to `fleet.json`. Pre-1.0: no migration code,
no shims.

## 1. Background & current state

`--repos` is consumed by exactly three handlers, all of which split on
commas then resolve each token through `resolve_repo`
(`src/fleet/tasks/validation.py`):

- `cmd_new` (`src/fleet/tasks/lifecycle.py`) —
  `repo_tokens = [t.strip() for t in args.repos.split(",") if t.strip()]`,
  then `discover_repos()` → `resolve_repo` loop → leaf-collision check →
  `workspace.mkdir`.
- `cmd_add_repo` (`src/fleet/tasks/edit.py`) — `_split_repo_tokens` →
  `resolve_repo` loop.
- `cmd_remove_repo` (`src/fleet/tasks/edit.py`) — `_split_repo_tokens` →
  `require_repo_in_task` loop.

A token is whatever `resolve_repo` accepts: bare leaf `alpha` or
`group/path/name`. Tokens never start with `@` today (`RepoInfo` leaves
can't), which is what makes `@` a safe sigil.

Patterns to mirror:

- **Per-fleet config file**: `FleetsConfig`
  (`src/fleet/fleets_config.py`) — dataclass + `load()`/`save()` (atomic
  tmp + `os.replace`) + `_config_lock` cross-process advisory lock.
  `Manifest.save()` is the other atomic-write template.
- **Per-fleet derived path**: `registry_path()` = `find_repos_root() /
  REGISTRY_FILENAME`. Bundles get the analogous `bundles_path()`.
- **Command module + `register()` hook**: `fleets_commands.py`, wired in
  `build_parser()`.
- **Completion providers**: `POS_PROVIDERS` / `OPT_PROVIDERS`, the
  comma-aware `--repos` branch in `_complete_option_value`, and the
  `_task_name_after` / `_manifest_repo_names` anchor pattern.

Invariants preserved: git only from `git_ops.py` (bundles never touch
git → trivially satisfied); atomic persistent writes; exit-code
contract 0/1/2; zero runtime deps; no docstrings/comments on untouched
code.

## 2. Command surface & UX

New top-level group `fleet bundles`, all subparsers `parents=[fleet_arg]`.

### `bundles add <name> --repos a,b,grp/c [--force]`

```text
PS> fleet bundles add core --repos alpha,group/gamma
✓ Bundle 'core' created (2 repo(s)).

PS> fleet bundles add core --repos beta
ERROR: Bundle 'core' already exists. Use --force to overwrite.   # rc 1
```

- Validates name; rejects any member token starting with `@` (no
  nesting). Stores tokens verbatim, ordered, deduped. Best-effort
  resolves each token against `discover_repos()`; **non-fatal warning**
  for tokens that don't currently resolve (still saves, rc 0) — supports
  pre-cloning workflows.
- rc 0 success; rc 1 on dup-without-`--force`, invalid name, `@`-token,
  empty `--repos`.

### `bundles list`

```text
Configured bundles (fleet 'main'):
  core       2 repo(s)
  frontend   1 repo(s)

Bundles file: C:\src\bundles.json
```

Empty → "No bundles configured" hint. rc 0.

### `bundles show <name>`

```text
Bundle 'core' (fleet 'main'):
  alpha
  group/gamma   [missing]      # token no longer resolves
```

rc 0; rc 1 unknown bundle.

### `bundles remove <name>`

rc 0; rc 1 unknown bundle. (Pure config delete — no git side effects.)

### `bundles edit <name> [--add a,b] [--remove c]`

```text
PS> fleet bundles edit core --add beta --remove group/gamma
✓ Bundle 'core' updated (2 repo(s)).
```

- At least one of `--add` / `--remove` required (rc 1 if neither).
- `--add` appends (dedup, reject `@`); `--remove` drops by exact token
  match (warn if a token isn't a member).
- Same best-effort resolve warning as `add`.

### Consumption (the seam)

```text
PS> fleet task new bug-42 --repos @core,extra-repo
PS> fleet task add-repo bug-42 --repos @frontend
PS> fleet task remove-repo bug-42 --repos @core
```

- `@name` expands in place to the bundle's ordered tokens; mixing
  `@bundle` with raw tokens is allowed.
- Unknown `@bundle` → `FleetError` listing available bundles (rc 1),
  raised **before any filesystem mutation**.
- Duplicates introduced by expansion are absorbed by the existing
  `seen`-set "note: ignoring duplicate" logic.

### Completion

- `bundles show|remove|edit <Tab>` → bundle names. `bundles add <Tab>` →
  free text.
- `bundles edit <name> --add <Tab>` → repo names **not** already in the
  bundle; `--remove <Tab>` → tokens **currently in** the bundle.
  Comma-aware, `DIRECTIVE_NOSPACE`.
- `task new|add-repo|remove-repo --repos <Tab>` → existing repo
  candidates **plus** `@bundle` names. `DIRECTIVE_NOSPACE` preserved.

## 3. Module-by-module changes

- **`paths.py`** — add `BUNDLES_FILENAME = "bundles.json"`.
- **`state.py`** — add `bundles_path() -> Path` = `find_repos_root() /
  BUNDLES_FILENAME`.
- **`bundles_config.py` (new)**:
  - `_BUNDLE_NAME_RE`, `validate_bundle_name(name)`.
  - `@dataclass BundlesConfig` with `bundles: dict[str, list[str]]`;
    `load()` (empty on missing; `FleetError` on malformed); `save()`
    (atomic tmp + `os.replace`); `add(name, tokens, force)`,
    `remove(name)`, `edit(name, add, remove)`, `get(name)`,
    `names() -> list[str]`.
  - `expand_bundle_tokens(tokens: list[str]) -> list[str]` — the single
    expansion seam; loads `BundlesConfig` lazily, splices `@name`
    members in order, `FleetError` on unknown.
  - All writes wrap `fleets_config._config_lock(bundles_path())`.
- **`bundles_commands.py` (new)** — `cmd_bundles_add/list/show/remove/edit`
  + `register(subparsers, fleet_arg)`. `show` cross-checks tokens
  against `discover_repos()` for the `[missing]` marker.
- **`cli.py`** — call `bundles_commands.register(...)`. `bundles`
  requires an active fleet (do **not** exclude it from
  `_needs_active_fleet`).
- **`tasks/lifecycle.py`** — in `cmd_new`, after the comma split:
  `repo_tokens = expand_bundle_tokens(repo_tokens)`.
- **`tasks/edit.py`** — expand after `_split_repo_tokens` in both
  `cmd_add_repo` and `cmd_remove_repo`.
- **`completion.py`** — add bundle providers + `POS_PROVIDERS` entries
  for `("bundles", "show"|"remove"|"edit")`; extend the comma-aware
  branch to fire for `bundles edit --add`/`--remove`; append `@bundle`
  candidates to `--repos` for new/add/remove.
- **`README.md`** — command-reference rows, "Bundles" concept section,
  Layout entries, config cheatsheet (`<fleet-root>/bundles.json`).

`git_ops.py` is **not** touched.

## 4. Failure modes, rollback & atomicity

- **`bundles.json` write** — atomic (tmp + `os.replace`) under
  `_config_lock(bundles_path())`. Concurrent `bundles add` serialize
  like `fleets add`.
- **Malformed `bundles.json`** — `BundlesConfig.load` raises `FleetError`
  once (mirrors `FleetsConfig` / `Manifest`), surfaced as rc 1.
- **Bundle name ↔ repo name collision** — resolved by the `@` sigil:
  repo `alpha` is `alpha`, bundle `alpha` is `@alpha`. Justification for
  the sigil over `--bundle NAME`: a sigil interleaves with raw tokens in
  one ordered list and needs no extra flag at every consumption site.
- **Bundle referring to a now-missing/disabled repo** — stored
  verbatim, so allowed at rest. `bundles show` flags `[missing]`;
  `bundles add/edit` warns non-fatally. At **consumption**, `resolve_repo`
  raises its existing clear "Unknown repo / disabled in fleet.json"
  error (rc 1). For `cmd_new`, expansion + resolution both happen
  **before** `workspace.mkdir`, so a bad member never leaves a half-built
  workspace. For `add-repo`, the existing `BaseException` rollback
  covers anything after expansion.
- **Circular references / bundles-of-bundles** — structurally
  impossible: `add` / `edit` reject any token starting with `@`. No
  cycle detection needed. Keeps the mental model "bundle = list of
  repos" — total, one-level expansion.
- **Unknown `@bundle` at consumption** — `expand_bundle_tokens` raises
  `FleetError` ("Unknown bundle '@x'. Known: …") before any mutation.
- **Empty bundle / empty expansion** — falls through to the existing
  "--repos requires at least one repo name" guard (rc 1).

## 5. Edge cases to test

- `bundles add` with a token starting with `@` → rejected.
- `bundles add` duplicate name without / with `--force`.
- `bundles add --repos alpha,alpha` → dedup (note printed).
- `bundles add` with a token that doesn't currently resolve → saved,
  warning printed, rc 0.
- `bundles edit` neither `--add` nor `--remove` supplied → rc 1.
- `bundles edit --remove` of a token not in the bundle → warn, no error.
- `bundles edit` resulting in empty bundle → allowed; later
  `task new @empty` → "requires at least one repo".
- `bundles show` of a bundle with a missing token → `[missing]` marker.
- `bundles remove` unknown name → rc 1.
- Per-fleet isolation: same bundle name in fleet A and fleet B
  independent.
- `task new --repos @core` happy → worktrees match ordered tokens;
  `task.json` records expanded entries (no bundle ref).
- `task new --repos @core,alpha` where `core` already contains `alpha`
  → dedup, one worktree.
- `task new --repos @nope` → `FleetError`, no workspace dir created.
- `task new --repos @core` where one member is disabled → `resolve_repo`
  error, no workspace dir.
- `add-repo --repos @frontend`, `remove-repo --repos @core` happy paths.
- `remove-repo --repos @core` where a member isn't in the task →
  `require_repo_in_task` error.
- Token ordering preserved across expansion.

## 6. Test plan (must end fully green)

### unit (`tests/unit/test_bundles_config.py`, `test_completion.py`)

- `BundlesConfig`: add/edit/remove/get round-trip via save→load; atomic
  write leaves no `.tmp`; `force` semantics; `@`-token rejection;
  malformed file → `FleetError`; missing file → empty config.
- `expand_bundle_tokens`: `@core` → ordered members; mixed `@core,raw`;
  unknown `@x` → `FleetError`; non-`@` tokens passthrough.
- `validate_bundle_name`: accept/reject table.
- completion (monkeypatch `BundlesConfig.load` + `discover_repos`):
  `bundles show <Tab>` → bundle names; `bundles edit <b> --add <Tab>`
  excludes current members + `DIRECTIVE_NOSPACE`; `--remove <Tab>` only
  members; `task new --repos <Tab>` includes `@core`.

### integration (`tests/integration/test_bundles.py`)

- Full CRUD against a live fleet: add → `bundles.json` on disk has
  ordered tokens; list; show with a `[missing]` after a repo is
  disabled; edit add/remove; remove.
- Per-fleet isolation with two fleets.
- Consumption: `cmd_new(--repos @core)` → assert worktree dirs exist for
  each member, `Manifest.load(ws).repos` names == expanded order.
- `cmd_new(--repos @core,alpha)` dedup.
- `cmd_new(--repos @nope)` → `FleetError`; workspace not created.
- `cmd_new` with a disabled member → `FleetError`, no workspace.
- `cmd_add_repo(--repos @frontend)` and `cmd_remove_repo(--repos @core)`
  happy paths.

### e2e (`tests/e2e/test_subprocess.py`)

One subprocess happy path each: `bundles add/list/show/edit/remove`;
plus `task new --repos @core`.

### completion subprocess (`tests/e2e/test_completion_subprocess.py`)

- `fleet __complete -- bundles show ` → bundle names, `:0`.
- `fleet __complete -- bundles edit <b> --add ` → `:4`, excludes
  members.
- `fleet __complete -- task new bug --repos ` → includes `@core`, `:4`.

**Success criterion:** a full `pytest` run is fully green (only
platform-skips allowed).

## 7. Compat / cleanup

Pre-1.0 — no migration, no shims, no fallback readers. `task.json`
schema is unchanged (bundles are input sugar only). Nothing currently in
the repo is subsumed or needs removal. No completion-script regen
(shell glue shells out to `fleet __complete`).

## 8. Implementation order (repo green at each step)

1. `paths.BUNDLES_FILENAME` + `state.bundles_path()`.
2. `bundles_config.py` + unit tests.
3. `bundles_commands.py` + `cli.py` wiring + integration CRUD tests.
4. Expansion seam in `cmd_new` / `cmd_add_repo` / `cmd_remove_repo` +
   consumption integration tests.
5. Completion providers + unit + subprocess completion tests.
6. README updates.
7. Full `pytest`; manual smoke (happy path, missing-repo,
   unknown-bundle).

## 9. Open questions

1. `bundles add` validation strictness — recommend store-verbatim +
   non-fatal warning. Alt: hard-resolve at add time.
2. `bundles edit` requiring at least one of `--add` / `--remove` —
   recommend yes (rc 1 if neither).
3. `remove-repo --repos @bundle` symmetry — recommend yes.
4. Per-bundle concurrency — reuse `_config_lock` (no finer-grained lock).

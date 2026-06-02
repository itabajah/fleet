# fleet

> A small, fast, dependency-free CLI for keeping a directory full of git
> repos in sync — and for spinning up disposable, multi-repo "task"
> workspaces using `git worktree`.

`fleet` is for anyone whose `~/code/` (or `D:\src\`, or…) has more than a
handful of git checkouts, and who is tired of:

- writing yet another `for d in *; do (cd "$d" && git pull); done` one-liner,
- losing track of which repos exist on disk vs. which they actually want to keep up to date,
- juggling ad-hoc feature branches across N repos at once,
- hand-stitching VS Code workspaces every time a new task starts.

It is one self-contained Python package (`fleet/`) and one thin PowerShell
module (`Fleet.psm1`) that glues it into your shell. **Zero runtime
dependencies** — Python stdlib + `git` is the entire stack.

---

## Highlights

- **Parallel `git pull --ff-only`** across every enabled repo, with
  per-host credential probing, retries, and a clean summary.
- **Disk-driven config**: `fleet scan` walks the tree and writes
  `fleet.json`. Manual `sync: false` and `exclude` entries survive
  re-scans; single-child folder chains collapse for compact output.
- **Task workspaces**: `fleet task new bug-123 --repos foo,bar` creates
  one `git worktree` per repo on a shared `task/<fleet>/bug-123` branch,
  with a manifest, scratch dir, and one-shot teardown.
- **Multiple fleets**: register any number of named fleets (each is a
  root directory + its own `fleet.json`), with a default and per-command
  `-F NAME` override.
- **PowerShell-first**, but the Python CLI is fully usable on its own
  (Linux, macOS, WSL, Git Bash, etc.).

---

## Install

```powershell
# 1. Clone wherever you like
git clone https://github.com/itabajah/fleet.git $HOME\src\fleet

# 2. Wire the PowerShell module into your $PROFILE
'Import-Module $HOME\src\fleet\Fleet.psm1' | Add-Content -Path $PROFILE

# 3. Restart PowerShell, then
fleet --help
```

That's it. The module sets `PYTHONPATH` to the package's `src/` for you,
so no `pip install` is needed. (You can `pip install -e .` if you'd
rather have `fleet` on `PATH` without any shell wrapper — see
[Shell integration on Linux/macOS](#shell-integration-on-linuxmacos)
below for the bash/zsh equivalent.)

### Requirements

| Tool          | Version                                                           |
| ------------- | ----------------------------------------------------------------- |
| Python        | ≥ 3.10                                                            |
| `git`         | any modern version (must support `git worktree`)                  |
| shell wrapper | optional — `Fleet.psm1` (PowerShell) or `fleet.sh` (bash/zsh), only needed for `fleet open` |
| `code`        | optional — used by `fleet open` to launch VS Code on the workspace |

---

## First-run walkthrough

```powershell
# Tell fleet about a directory full of repos
PS> cd C:\src                       # contains acme/, foo-cli/, internal/baz/, ...
PS> fleet fleets add main           # registers C:\src as fleet "main" (default)
✓ Registered fleet 'main' at C:\src
  (set as default)
  Tip: run `fleet scan -F main` to populate fleet.json

# Walk the tree, build the registry
PS> fleet scan
✓ Scan complete.
  Total repositories found: 23
  New repositories found:   23
  Repositories enabled:     23
Configuration saved to: C:\src\fleet.json

# Pull everything in parallel
PS> fleet sync
✓ All operations completed successfully!
  Total processed:      23
  Successfully updated: 23

# Start a multi-repo task
PS> fleet task new payments-bug --repos acme,foo-cli -d "investigate 500s"
PS> fleet open payments-bug         # cds + launches VS Code
```

That's the entire happy path.

---

## Command reference

| Command                                      | What it does                                                             |
| -------------------------------------------- | ------------------------------------------------------------------------ |
| `fleet sync`                                 | Parallel `git pull --ff-only` across every enabled repo                   |
| `fleet sync --dry-run`                       | Preview what sync would do                                                |
| `fleet sync --workers N`                     | Worker count (default 10, `0` = auto: one per repo, capped at 32)        |
| `fleet sync --no-auth-check`                 | Skip the per-host credential probe                                        |
| `fleet scan`                                 | Walk disk, rewrite `fleet.json`, preserve manual settings                 |
| `fleet repos`                                | List every git repo on disk; mark disabled / not-in-registry              |
| `fleet task new <name> --repos a,b[,grp/c]`  | Create a task workspace with worktrees                                    |
| `fleet task new ... --no-pull`               | Skip fetch + pull on each canonical repo (offline-safe; local refs only)  |
| `fleet task new ... --dry-run`               | Validate inputs and print the plan without creating anything              |
| `fleet task add-repo <name> --repos a,b`     | Add one or more repos (new worktrees) to an existing task                 |
| `fleet task remove-repo <name> --repos a,b [--force]` | Remove repos and tear down their worktrees (`--force` over dirty/unpushed) |
| `fleet task rename <old> <new>`              | Rename a task: rename branch in every canonical + move workspace dir       |
| `fleet task edit <name> --description TEXT`  | Update the task's description in `task.json` (and `context.md`)            |
| `fleet task list [--quick]`                  | List active task workspaces (`--quick` skips dirty/unpushed checks)       |
| `fleet task list --json`                     | Emit one JSON object per line on stdout (implies `--quick`)               |
| `fleet task info <name>`                     | Detailed status of one task                                               |
| `fleet task sync <name>`                     | Fetch + ff-pull each worktree on its task branch                          |
| `fleet task end <name> [--force]`            | Archive `task.json`/`context.md`/`scratch/`, tear down worktrees          |
| `fleet task path <name>`                     | Print the absolute workspace path (used by `fleet open` internally)       |
| `fleet open <name>`                          | `cd` into a task workspace and launch VS Code (PowerShell or `fleet.sh`)  |
| `fleet fleets list`                          | Show every configured fleet, mark default                                 |
| `fleet fleets add <name> [--root PATH] [--force]` | Register a fleet (defaults to current directory; `--force` overwrites)    |
| `fleet fleets default <name>`                | Switch the default fleet                                                  |
| `fleet fleets remove <name>`                 | Unregister a fleet (no file deletion)                                     |

**Per-command override.** Add `-F NAME` (or `--fleet NAME`) to any
fleet-aware command to use a non-default fleet for that invocation only:

```powershell
fleet sync -F work
fleet task new bug-123 --repos foo,bar -F work
fleet open bug-123 -F work
```

---

## Concepts

### Fleet

A *fleet* is a named pair of `(root directory, fleet.json registry)`.
The first fleet you register becomes the default; later commands use
that default unless you pass `-F NAME` or change the default with
`fleet fleets default NAME`.

The named-fleets index lives at `%LOCALAPPDATA%\fleet\fleets.json`
on Windows, `~/.config/fleet/fleets.json` on Linux/macOS (override
either with the `FLEET_CONFIG_PATH` env var):

```json
{
  "default": "main",
  "fleets": {
    "main": { "root": "C:\\src" },
    "work": { "root": "D:\\work" }
  }
}
```

### Registry: `fleet.json`

Lives at the active fleet's repos root (`<fleet-root>/fleet.json`).
Hand-edit `sync: false` on a folder to skip it (and everything under
it); add a name to a folder's `exclude` list to skip just that one
repo. `fleet scan` preserves both across re-scans.

```json
{
  "root": ".",
  "infra": {
    "sync": true,
    "repos": ["k8s-clusters"],
    "exclude": ["pipeline-templates"],
    "subfolders": {
      "k8s-clusters/temp": {
        "sync": true,
        "repos": ["scratch-experiment"]
      }
    }
  },
  "vendored": { "sync": false }
}
```

`subfolders` keys can collapse single-child chains (`"a/b/c"`); the
scan handles both forms on read and emits the collapsed form on write.

### Tasks

Task workspaces live at `<TASKS_ROOT>/<fleet>/<task>/`. Default
`TASKS_ROOT` is `%LOCALAPPDATA%\fleet-tasks` on Windows and
`~/fleet-tasks` elsewhere — both are always user-writable. Override at
any time with the `FLEET_TASKS_ROOT` environment variable (e.g. point it
at a fast scratch disk).

Each task gets:

- one `git worktree` per chosen repo (checked out on `task/<fleet>/<name>`,
  branched from the canonical repo's default branch),
- a `task.json` manifest recording branch, repos, creation time, and
  per-repo worktree paths,
- a `context.md` scratchpad (use it however you like — Copilot prompts,
  scratch notes, links),
- a `scratch/` directory for throwaway files.

`fleet task end` zips the manifest + `context.md` + `scratch/` into
`<TASKS_ROOT>/<fleet>/_archive/<task>-<timestamp>.zip`, then removes
every worktree and the workspace folder. Use `--force` to tear down
through dirty worktrees.

Tasks can also be edited in place without ending and recreating them:

- `fleet task add-repo <name> --repos foo` adds a fresh worktree on the
  existing `task/<fleet>/<name>` branch and appends it to `task.json`.
- `fleet task remove-repo <name> --repos foo` tears down that repo's
  worktree (refuses on dirty/unpushed unless `--force`) and drops it
  from the manifest.
- `fleet task rename <old> <new>` renames the git branch in every
  canonical (`git branch -m`), moves the workspace directory, and
  rewrites `task.json` + the `context.md` header. If you were `cd`'d
  inside the old workspace, `cd` to the new path afterwards.
- `fleet task edit <name> --description TEXT` (or
  `--description-file PATH`, `-` for stdin) updates the manifest's
  description and the `## Description` section of `context.md`.

### `fleet open` (shell integration)

`fleet open <task>` resolves the path via `fleet task path <task>`,
sets the current directory, and launches VS Code (`code` on `PATH`).
Python can't change the parent shell's cwd, which is why this command
lives in a shell wrapper rather than the CLI itself:

- **PowerShell** (Windows, or pwsh 7 on Linux/macOS): `Fleet.psm1`.
- **bash/zsh** (Linux/macOS/WSL/Git Bash): `fleet.sh` — `source` it from
  your shell rc (see [Shell integration on Linux/macOS](#shell-integration-on-linuxmacos)).

To open a task in a non-default fleet: `fleet open <task> -F NAME`.

---

## Repos in multiple fleets

Fully supported — fleets are completely isolated namespaces, even when
they share state on disk:

1. **Different physical clones of the same upstream.** Fleet A's root
   has its own `org-foo/`; fleet B's root has another. They are
   completely independent — separate `.git`, separate branches, separate
   worktrees.
2. **Same physical clone shared by both fleets** (e.g. fleet A is
   `C:\src` and fleet B is `C:\src\subset`):
   - **Tasks are namespaced by fleet on disk** (`<TASKS_ROOT>/<fleet>/<task>/`)
     and **branches are namespaced by fleet in git** (`task/<fleet>/<name>`).
     Two fleets can each have a task called `bug-123` against the
     same shared repo without colliding.
   - `fleet sync` from either fleet pulls the same canonical `.git`.
     Safe but redundant — the second pull is essentially a no-op.

All task-management commands (`task new` / `task sync` / `task end` /
`task path` / `open`) stay scoped to whichever fleet is active (or
whichever one you pass via `-F`). There is no global task list.
---

## Layout

```
fleet/
├── Fleet.psm1                 # PowerShell entry point
├── fleet.sh                   # bash/zsh entry point (Linux/macOS/WSL)
├── README.md                  # this file
├── pyproject.toml             # package metadata (no runtime deps)
└── src/fleet/
    ├── __init__.py
    ├── __main__.py            # `python -m fleet`
    ├── cli.py                 # argparse top-level + dispatch
    ├── console.py             # ANSI color helpers
    ├── discovery.py           # walks disk + applies fleet.json rules
    ├── errors.py              # FleetError (carries exit code)
    ├── fleets_commands.py     # `fleet fleets ...` handlers
    ├── fleets_config.py       # named-fleet registry (LOCALAPPDATA / XDG)
    ├── git_ops.py             # unified git wrappers (run_git, fetch, pull, ...)
    ├── paths.py               # filesystem constants (PRUNE_DIRS, defaults)
    ├── registry_tree.py       # fleet.json normalization + traversal
    ├── repos_command.py       # `fleet repos`
    ├── scan.py                # `fleet scan` (write path)
    ├── state.py               # active-fleet state + derived paths
    ├── sync.py                # `fleet sync` (parallel runner)
    ├── walker.py              # shared disk walker
    └── tasks/                 # `fleet task ...` (manifest, lifecycle, inspect, validation)
```

---

## Shell integration on Linux/macOS

The Python CLI is fully cross-platform; only `fleet open` needs a shell
wrapper (it has to change the parent shell's cwd, which no child process
can do). Source `fleet.sh` from your `~/.bashrc` / `~/.zshrc`:

```bash
echo 'source "$HOME/src/fleet/fleet.sh"' >> ~/.bashrc   # or ~/.zshrc
exec $SHELL          # reload
fleet --help
```

That gives you the full command set — including `fleet open <name>`
(and `fleet open <name> -F work`) — with the same behavior as the
PowerShell module. It sets `PYTHONPATH` to the package's `src/` for you,
so no `pip install` is needed.

### Without any shell wrapper

If you'd rather not source anything (or you installed via
`pip install -e <path-to-clone>` so `fleet` is already on `PATH`), every
command works the same — the only thing you lose is `fleet open`, which
you replace with:

```bash
cd "$(fleet task path <name>)" && code .
# Non-default fleet:
cd "$(fleet task path <name> -F work)" && code .
```

The Python CLI never touches your shell state.

---

## Tab completion

Pressing `<Tab>` after `fleet` (and its subcommands and flags) suggests
valid completions, including dynamic values: fleet names for `-F`, task
names for `fleet task info|sync|end|path|open`, and repo names for
`fleet task new --repos`.

If you source the shell wrapper, completion is **already wired up** — no
extra step needed:

- PowerShell: `Import-Module <install-dir>\Fleet.psm1`
- bash/zsh:   `source <install-dir>/fleet.sh`

If you installed via `pip install -e .` and prefer not to source the
wrapper, register completion directly with your shell:

```bash
# bash (~/.bashrc)
source <(fleet completion bash)

# zsh (~/.zshrc, after `autoload -Uz compinit && compinit`)
source <(fleet completion zsh)
```

```powershell
# PowerShell ($PROFILE)
Invoke-Expression (& fleet completion powershell | Out-String)
```

---

## Configuration cheatsheet

| Variable                | Purpose                                                       | Default                                          |
| ----------------------- | ------------------------------------------------------------- | ------------------------------------------------ |
| `FLEET_CONFIG_PATH`     | Override the global fleets index location                     | `%LOCALAPPDATA%\fleet\fleets.json` / `~/.config/fleet/fleets.json` |
| `FLEET_TASKS_ROOT`      | Override the parent of `<fleet>/<task>/`                       | `%LOCALAPPDATA%\fleet-tasks` (Windows) / `~/fleet-tasks` (other) |
| `FLEET_REPOS_ROOT`      | Escape hatch used only when no fleet is active (tests / scripts); ignored once a fleet has been resolved | (unset)                                          |

| File                                     | What                                                |
| ---------------------------------------- | --------------------------------------------------- |
| `<fleet-root>/fleet.json`                | Per-fleet enable/exclude registry                   |
| `<FLEET_CONFIG_PATH>`                    | Global named-fleets index                           |
| `<FLEET_TASKS_ROOT>/<fleet>/<task>/`     | Active task workspace                               |
| `<FLEET_TASKS_ROOT>/<fleet>/_archive/`   | Archived tasks (one zip per `task end`)             |

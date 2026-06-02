# fleet.sh — bash/zsh shell integration for the `fleet` CLI.
#
# Most commands forward straight to `python -m fleet`. The exception is
# `fleet open <task> [-F NAME]`, which has to change the parent shell's
# working directory — a child process can't do that. So this function
# calls `python -m fleet task path <task> [-F NAME]` to resolve the
# workspace path (Python knows about fleets, default vs override, etc.),
# then does the cd + `code` invocation in-shell.
#
# This is the bash/zsh equivalent of Fleet.psm1. PowerShell 7 runs on
# Linux/macOS too, so you can use either; this is here so you don't need
# pwsh just to get `fleet open`.
#
# One-time setup: source this file from your shell rc, replacing
# <install-dir> with the path where you cloned this repo:
#
#   source <install-dir>/fleet.sh
#
# (e.g. source "$HOME/src/fleet/fleet.sh")
#
# It sets PYTHONPATH to the package's src/ for the duration of each call,
# so no `pip install` is needed. If you installed via `pip install -e .`
# the `fleet` entry point already exists and you only need the `open`
# helper below — but sourcing this file is harmless either way.

# Resolve the directory this script lives in (bash and zsh differ here).
if [ -n "${BASH_SOURCE:-}" ]; then
    _fleet_self="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _fleet_self="${(%):-%N}"
else
    _fleet_self="$0"
fi
_FLEET_SRC="$(cd "$(dirname "$_fleet_self")/src" 2>/dev/null && pwd)"
unset _fleet_self

# Pick a Python launcher once. Prefers `python3` then `python`.
_fleet_python() {
    if command -v python3 >/dev/null 2>&1; then
        printf 'python3'
    elif command -v python >/dev/null 2>&1; then
        printf 'python'
    else
        return 1
    fi
}

# Run `python -m fleet ...` with src/ prepended to PYTHONPATH, then
# restored. Component-aware so e.g. `.../src-experimental` doesn't
# false-match `.../src`.
_fleet_run() {
    local py
    py="$(_fleet_python)" || {
        printf 'fleet: no Python interpreter found on PATH (tried python3, python). Install Python >= 3.10.\n' >&2
        return 1
    }
    local old_pp="${PYTHONPATH:-}"
    local sep=':'
    # Prepend src/ unless it's already a discrete entry. The case glob
    # wraps both sides in separators so `.../src` can't false-match
    # `.../src-experimental`, and avoids touching IFS.
    case "${sep}${old_pp}${sep}" in
        *"${sep}${_FLEET_SRC}${sep}"*)
            : ;;  # already present (or _FLEET_SRC empty) — leave PYTHONPATH alone
        *)
            if [ -n "$_FLEET_SRC" ]; then
                if [ -n "$old_pp" ]; then
                    export PYTHONPATH="${_FLEET_SRC}${sep}${old_pp}"
                else
                    export PYTHONPATH="$_FLEET_SRC"
                fi
            fi
            ;;
    esac
    "$py" -m fleet "$@"
    local rc=$?
    if [ -n "$old_pp" ]; then
        export PYTHONPATH="$old_pp"
    else
        unset PYTHONPATH
    fi
    return $rc
}

fleet() {
    if [ "$#" -eq 0 ]; then
        _fleet_run --help
        return
    fi

    # `open` and `task open` need to mutate the parent shell — handle locally.
    if [ "$1" = "open" ]; then
        shift
        _fleet_open "$@"
        return
    fi
    if [ "$1" = "task" ] && [ "${2:-}" = "open" ]; then
        shift 2
        _fleet_open "$@"
        return
    fi

    _fleet_run "$@"
}

_fleet_open() {
    if [ "$#" -eq 0 ]; then
        printf 'Usage: fleet open <task-name> [-F <fleet>]\n' >&2
        return 2
    fi

    # Pass the entire remainder through to `task path` so -F / --fleet,
    # quoted args, etc. are handled by argparse exactly once. `task path`
    # writes only the workspace path to stdout; diagnostics go to stderr.
    local workspace
    workspace="$(_fleet_run task path "$@")"
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        return "$rc"
    fi

    # Trim to the first non-empty line.
    workspace="$(printf '%s\n' "$workspace" | sed -n '/[^[:space:]]/{p;q;}')"
    if [ -z "$workspace" ]; then
        printf 'fleet task path produced no output\n' >&2
        return 1
    fi
    if [ ! -d "$workspace" ]; then
        printf 'Resolved workspace does not exist: %s\n' "$workspace" >&2
        return 1
    fi

    cd "$workspace" || return 1

    if command -v code >/dev/null 2>&1; then
        code "$workspace"
    else
        printf 'VS Code (code) not on PATH; skipping launch.\n' >&2
    fi
}

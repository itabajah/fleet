# bash completion for the `fleet` CLI.
#
# Sourced automatically by fleet.sh. For pip-installed users:
#   source <(fleet completion bash)

_fleet_complete() {
    local cur words out directive body line
    cur="${COMP_WORDS[COMP_CWORD]}"
    # Pass every word the user typed after `fleet`, ending with the current
    # partial (which may be empty when the cursor is on a fresh word).
    words=("${COMP_WORDS[@]:1:COMP_CWORD}")
    if [ "${#words[@]}" -eq 0 ] || [ "${words[${#words[@]}-1]}" != "$cur" ]; then
        words+=("$cur")
    fi

    out="$(fleet __complete -- "${words[@]}" 2>/dev/null)" || return 0
    [ -z "$out" ] && return 0

    directive="${out%%$'\n'*}"
    directive="${directive#:}"
    body="${out#*$'\n'}"
    [ "$body" = "$out" ] && body=""

    COMPREPLY=()
    while IFS= read -r line; do
        [ -n "$line" ] && COMPREPLY+=("$line")
    done <<< "$body"

    if [ -n "$directive" ] && [ "$(( directive & 4 ))" -ne 0 ]; then
        compopt -o nospace 2>/dev/null
    fi
}

complete -F _fleet_complete fleet

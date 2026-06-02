#compdef fleet
# zsh completion for the `fleet` CLI.
#
# Sourced automatically by fleet.sh. For pip-installed users:
#   source <(fleet completion zsh)

_fleet() {
    local -a payload cands
    local cur out directive body suffix

    # $words is the full command line (1-indexed); $words[1] is "fleet".
    # Pass tokens from $words[2] up to $words[CURRENT], ensuring the final
    # element is the current partial (empty when the cursor is on a fresh word).
    cur="${words[CURRENT]:-}"
    if (( CURRENT > 1 )); then
        payload=("${words[@]:1:CURRENT-1}")
    else
        payload=()
    fi
    if (( ${#payload[@]} == 0 )) || [[ "${payload[-1]}" != "$cur" ]]; then
        payload+=("$cur")
    fi

    out=$(fleet __complete -- "${payload[@]}" 2>/dev/null) || return 0
    [[ -z "$out" ]] && return 0

    directive="${out%%$'\n'*}"
    directive="${directive#:}"
    body="${out#*$'\n'}"
    [[ "$body" == "$out" ]] && body=""

    cands=("${(@f)body}")
    cands=("${(@)cands:#}")
    (( ${#cands[@]} == 0 )) && return 0

    if [[ -n "$directive" ]] && (( directive & 4 )); then
        suffix=""
    else
        suffix=" "
    fi
    compadd -S "$suffix" -- "${cands[@]}"
}

compdef _fleet fleet

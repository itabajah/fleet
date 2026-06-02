# PowerShell argument completer for the `fleet` CLI.
#
# Dot-sourced automatically by Fleet.psm1. For pip-installed users:
#   Invoke-Expression (& fleet completion powershell | Out-String)

Register-ArgumentCompleter -Native -CommandName fleet -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    # Extract every token after `fleet`. CommandElements[0] is the command
    # itself (the `fleet` function); the rest are user-typed words.
    $elements = @($commandAst.CommandElements)
    if ($elements.Count -ge 1) {
        $tokens = @($elements[1..($elements.Count - 1)] | ForEach-Object { $_.Extent.Text })
    } else {
        $tokens = @()
    }

    # Ensure the last token IS the current partial — when the cursor is on a
    # fresh word (after a space), $wordToComplete is '' and is not in $tokens.
    if ($tokens.Count -eq 0 -or $tokens[-1] -ne $wordToComplete) {
        $tokens = $tokens + $wordToComplete
    }

    $payload = @('__complete', '--') + $tokens
    $raw = & fleet @payload 2>$null
    if (-not $raw) { return }

    $lines = @($raw | ForEach-Object { $_.ToString() })
    if ($lines.Count -lt 1) { return }

    $directive = 0
    if ($lines[0] -match '^:(\d+)$') { $directive = [int]$Matches[1] }
    if ($lines.Count -lt 2) { return }
    $cands = $lines[1..($lines.Count - 1)] | Where-Object { $_ }

    # nospace == ParameterName (no trailing space in PSReadLine);
    # otherwise ParameterValue (PSReadLine appends a space).
    if (($directive -band 4) -ne 0) {
        $rt = [System.Management.Automation.CompletionResultType]::ParameterName
    } else {
        $rt = [System.Management.Automation.CompletionResultType]::ParameterValue
    }

    foreach ($c in $cands) {
        [System.Management.Automation.CompletionResult]::new($c, $c, $rt, $c)
    }
}

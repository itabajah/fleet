# PowerShell argument completer for the `fleet` CLI.
#
# Dot-sourced automatically by Fleet.psm1. For pip-installed users:
#   Invoke-Expression (& fleet completion powershell | Out-String)

Register-ArgumentCompleter -Native -CommandName fleet -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    # Reconstruct the literal source typed after `fleet ` up to the cursor.
    # We deliberately do NOT use $commandAst.CommandElements: PowerShell's
    # parser eagerly turns `a,b,c` into an ArrayLiteralExpression, which
    # would destroy the comma-separated value of `--repos`. Extent.Text
    # preserves the exact characters the user typed, commas included.
    $extent = $commandAst.Extent
    $maxLen = $extent.Text.Length
    $rel = [Math]::Max(0, [Math]::Min($cursorPosition - $extent.StartOffset, $maxLen))
    $line = $extent.Text.Substring(0, $rel)

    # Strip the leading command name (`fleet`) plus its trailing whitespace.
    $firstSpace = $line.IndexOf(' ')
    if ($firstSpace -lt 0) {
        $payload = ''
    } else {
        $payload = $line.Substring($firstSpace + 1)
    }

    # Tokenize on whitespace runs. Commas stay inside tokens — which is
    # exactly what we want for `--repos a,b,c<TAB>`.
    if ([string]::IsNullOrEmpty($payload)) {
        $tokens = @()
    } else {
        $tokens = @($payload -split '\s+' | Where-Object { $_ -ne '' })
    }

    # If the line ends with whitespace (or is empty), the cursor is on a
    # fresh word — make sure the engine sees an empty trailing token.
    if ([string]::IsNullOrEmpty($payload) -or $payload[$payload.Length - 1] -match '\s') {
        $tokens = @($tokens) + ''
    }

    $invokeArgs = @('__complete', '--') + $tokens
    $raw = & fleet @invokeArgs 2>$null
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

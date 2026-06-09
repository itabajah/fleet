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

    # The engine bakes the whole comma-head into each `--repos` candidate
    # (e.g. `alpha,beta,gamma`) because bash/zsh replace the entire
    # whitespace-delimited word. PowerShell is different: it parses
    # `alpha,beta,` as an ArrayLiteralExpression and sets the completion
    # replacement span to ONLY the element after the last comma. Splicing
    # the full candidate there duplicates the head
    # (`alpha,beta,` + `alpha,beta,gamma` = `alpha,beta,alpha,beta,gamma`).
    # So strip everything up to and including the last comma of the current
    # token; PowerShell then fills in just the trailing element. With no
    # comma in the current token the head is empty and candidates pass
    # through unchanged.
    if ($tokens.Count -gt 0) {
        $currentToken = [string]$tokens[$tokens.Count - 1]
    } else {
        $currentToken = ''
    }
    $commaIdx = $currentToken.LastIndexOf(',')
    if ($commaIdx -ge 0) {
        $head = $currentToken.Substring(0, $commaIdx + 1)
    } else {
        $head = ''
    }

    foreach ($c in $cands) {
        if ($head -and $c.StartsWith($head)) {
            $text = $c.Substring($head.Length)
        } else {
            $text = $c
        }
        [System.Management.Automation.CompletionResult]::new($text, $text, $rt, $c)
    }
}

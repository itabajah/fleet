#requires -Version 7.0

# Fleet.psm1 — PowerShell entry point for the `fleet` CLI.
#
# Most commands forward straight to `python -m fleet`. The exception is
# `fleet open <task> [-F NAME]`, which has to change the parent shell's
# working directory — Python can't do that. So Fleet.psm1 calls
# `python -m fleet task path <task> [-F NAME]` to resolve the workspace
# path (Python knows about fleets, default vs override, etc.), then does
# the cd + `code` invocation locally.
#
# One-time setup: add the following to your $PROFILE, replacing
# <install-dir> with the path where you cloned this repo:
#
#   Import-Module <install-dir>\Fleet.psm1
#
# (e.g. Import-Module $HOME\src\fleet\Fleet.psm1)

$script:FleetRoot = $PSScriptRoot
$script:FleetSrc  = Join-Path $PSScriptRoot 'src'

function fleet {
    [CmdletBinding(PositionalBinding = $false)]
    param(
        [Parameter(Position = 0)]
        [string]$Command,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Rest
    )

    if ([string]::IsNullOrWhiteSpace($Command)) {
        Invoke-FleetPython @('--help')
        return
    }

    # `open` and `task open` need to mutate the parent shell — handle locally.
    if ($Command -eq 'open') {
        return Invoke-FleetOpen -Rest $Rest
    }
    if ($Command -eq 'task' -and $Rest -and $Rest.Count -ge 1 -and $Rest[0] -eq 'open') {
        return Invoke-FleetOpen -Rest ($Rest | Select-Object -Skip 1)
    }

    $argList = @($Command) + (@($Rest) | Where-Object { $_ -ne $null })
    Invoke-FleetPython $argList
}

function Invoke-FleetPython {
    param([string[]]$ArgList)

    $oldPyPath = $env:PYTHONPATH
    try {
        if ([string]::IsNullOrEmpty($oldPyPath)) {
            $env:PYTHONPATH = $script:FleetSrc
        } elseif ($oldPyPath -notlike "*$($script:FleetSrc)*") {
            $env:PYTHONPATH = "$($script:FleetSrc)$([System.IO.Path]::PathSeparator)$oldPyPath"
        }

        & python -m fleet @ArgList
        $global:LASTEXITCODE = $LASTEXITCODE
    }
    finally {
        $env:PYTHONPATH = $oldPyPath
    }
}

function Invoke-FleetPython-Capture {
    # Like Invoke-FleetPython but captures stdout (returns it as an array of
    # strings) so callers can read structured output. Stderr passes through
    # to the host. Used by `fleet open` to read the workspace path without
    # the dim banner contaminating it.
    param([string[]]$ArgList)

    $oldPyPath = $env:PYTHONPATH
    try {
        if ([string]::IsNullOrEmpty($oldPyPath)) {
            $env:PYTHONPATH = $script:FleetSrc
        } elseif ($oldPyPath -notlike "*$($script:FleetSrc)*") {
            $env:PYTHONPATH = "$($script:FleetSrc)$([System.IO.Path]::PathSeparator)$oldPyPath"
        }

        $stdout = & python -m fleet @ArgList
        $exitCode = $LASTEXITCODE
    }
    finally {
        $env:PYTHONPATH = $oldPyPath
    }

    return [PSCustomObject]@{
        Stdout   = $stdout
        ExitCode = $exitCode
    }
}

function Invoke-FleetOpen {
    param([string[]]$Rest)

    if (-not $Rest -or $Rest.Count -eq 0) {
        Write-Error 'Usage: fleet open <task-name> [-F <fleet>]'
        $global:LASTEXITCODE = 2
        return
    }

    # Pass the entire remainder through to `task path` so -F / --fleet,
    # quoted args, etc. are handled by argparse exactly once.
    $pathArgs = @('task', 'path') + $Rest
    $result = Invoke-FleetPython-Capture $pathArgs

    if ($result.ExitCode -ne 0) {
        $global:LASTEXITCODE = $result.ExitCode
        return
    }

    # Output may include the dim "[fleet: ...]" banner when -F is used; the
    # actual workspace path is the last non-empty line.
    $lines = @($result.Stdout) | Where-Object { $_ -and $_.ToString().Trim() }
    if (-not $lines) {
        Write-Error 'fleet task path produced no output'
        $global:LASTEXITCODE = 1
        return
    }
    $workspace = $lines[-1].ToString().Trim()

    if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
        Write-Error "Resolved workspace does not exist: $workspace"
        $global:LASTEXITCODE = 1
        return
    }

    Set-Location -LiteralPath $workspace

    if (Get-Command code -ErrorAction SilentlyContinue) {
        & code $workspace
    } else {
        Write-Warning 'VS Code (code) not on PATH; skipping launch.'
    }
    $global:LASTEXITCODE = 0
}

Export-ModuleMember -Function fleet

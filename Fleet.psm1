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

$script:FleetSrc = Join-Path $PSScriptRoot 'src'
$script:FleetPython = $null

function Resolve-FleetPython {
    # Resolve the Python launcher to use, once per session. Prefers `python`
    # then `python3` then the Windows `py -3` launcher. Cached so we don't
    # re-probe on every command.
    if ($script:FleetPython) { return $script:FleetPython }
    foreach ($exe in @('python', 'python3')) {
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if ($cmd) {
            $script:FleetPython = @($cmd.Source)
            return $script:FleetPython
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $script:FleetPython = @($py.Source, '-3')
        return $script:FleetPython
    }
    throw 'fleet: no Python interpreter found on PATH (tried `python`, `python3`, `py -3`). Install Python >= 3.10.'
}

function Add-FleetSrcToPyPath {
    # Prepend $script:FleetSrc to PYTHONPATH unless it's already there as a
    # discrete entry. Component-aware so e.g. `…\src-experimental` doesn't
    # false-match `…\src`.
    $sep = [System.IO.Path]::PathSeparator
    $existing = $env:PYTHONPATH
    if ([string]::IsNullOrEmpty($existing)) {
        $env:PYTHONPATH = $script:FleetSrc
        return
    }
    foreach ($p in $existing.Split($sep)) {
        if ($p -eq $script:FleetSrc) { return }
    }
    $env:PYTHONPATH = "$($script:FleetSrc)$sep$existing"
}

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
        Add-FleetSrcToPyPath
        $py = Resolve-FleetPython
        & $py[0] @($py | Select-Object -Skip 1) -m fleet @ArgList
        $global:LASTEXITCODE = $LASTEXITCODE
    }
    finally {
        $env:PYTHONPATH = $oldPyPath
    }
}

function Invoke-FleetPythonCapture {
    # Like Invoke-FleetPython but captures stdout (returns it as an array of
    # strings) so callers can read structured output. Stderr passes through
    # to the host. Used by `fleet open` to read the workspace path.
    param([string[]]$ArgList)

    $oldPyPath = $env:PYTHONPATH
    try {
        Add-FleetSrcToPyPath
        $py = Resolve-FleetPython
        $stdout = & $py[0] @($py | Select-Object -Skip 1) -m fleet @ArgList
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
    $result = Invoke-FleetPythonCapture $pathArgs

    if ($result.ExitCode -ne 0) {
        $global:LASTEXITCODE = $result.ExitCode
        return
    }

    # `task path` writes only the workspace path to stdout; banners and
    # other diagnostics go to stderr. Trim and use the first non-empty line.
    $lines = @($result.Stdout) | Where-Object { $_ -and $_.ToString().Trim() }
    if (-not $lines) {
        Write-Error 'fleet task path produced no output'
        $global:LASTEXITCODE = 1
        return
    }
    $workspace = $lines[0].ToString().Trim()

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

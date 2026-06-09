"""End-to-end completion: spawn real `python -m fleet __complete ...`.

Verifies stdout discipline (only `:directive` + candidates), exit code 0
even on misconfigured environments, and that `fleet completion <shell>`
emits the expected glue script.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001
    for item in items:
        item.add_marker(pytest.mark.e2e)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(
        [sys.executable, "-m", "fleet", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


# ---------------------------------------------------------------------------
# `fleet __complete` subprocess
# ---------------------------------------------------------------------------

def test_complete_help_is_suppressed() -> None:
    r = _run("--help")
    assert r.returncode == 0
    # The hidden subcommand must not appear in help output.
    assert "__complete" not in r.stdout


def test_complete_top_level(fleet_env_sandbox: Path) -> None:
    r = _run("__complete", "--", "")
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    lines = r.stdout.splitlines()
    assert lines[0] == ":0"
    assert "sync" in lines
    assert "task" in lines
    assert "completion" in lines


def test_complete_misconfigured_env_is_silent(
    fleet_env_sandbox: Path,
) -> None:
    # No fleets configured -> dynamic providers must return [] with no
    # stderr noise and exit 0.
    r = _run("__complete", "--", "task", "info", "")
    assert r.returncode == 0
    assert r.stderr == ""
    assert r.stdout.startswith(":0\n")


def test_complete_subcommand_options(fleet_env_sandbox: Path) -> None:
    r = _run("__complete", "--", "sync", "--")
    assert r.returncode == 0
    lines = r.stdout.splitlines()
    assert "--dry-run" in lines
    assert "--workers" in lines


# ---------------------------------------------------------------------------
# `fleet completion <shell>`
# ---------------------------------------------------------------------------

def test_completion_bash_script(fleet_env_sandbox: Path) -> None:
    r = _run("completion", "bash")
    assert r.returncode == 0, r.stderr
    assert "_fleet_complete" in r.stdout
    assert "complete -F _fleet_complete fleet" in r.stdout


def test_completion_zsh_script(fleet_env_sandbox: Path) -> None:
    r = _run("completion", "zsh")
    assert r.returncode == 0
    assert "#compdef fleet" in r.stdout
    assert "compdef _fleet fleet" in r.stdout


def test_completion_powershell_script(fleet_env_sandbox: Path) -> None:
    r = _run("completion", "powershell")
    assert r.returncode == 0
    assert "Register-ArgumentCompleter" in r.stdout


def test_completion_rejects_unknown_shell(fleet_env_sandbox: Path) -> None:
    r = _run("completion", "fish")
    assert r.returncode != 0
    assert "invalid choice" in r.stderr.lower()


# ---------------------------------------------------------------------------
# Bundles completion (subprocess)
# ---------------------------------------------------------------------------

def test_complete_bundles_subcommands(fleet_env_sandbox: Path) -> None:
    r = _run("__complete", "--", "bundles", "")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == ":0"
    for n in ("add", "list", "show", "remove", "edit"):
        assert n in lines


def test_complete_bundles_show_lists_bundles(
    fleet_env_sandbox: Path, tmp_path: Path,
) -> None:
    """End-to-end: register a fleet, scan, add a bundle, complete `bundles show`."""
    repos_root = tmp_path / "src_complete"
    repos_root.mkdir()
    for name in ("alpha", "beta"):
        d = repos_root / name
        d.mkdir()
        (d / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")
    assert _run("fleets", "add", "compcomp",
                "--root", str(repos_root)).returncode == 0
    assert _run("scan", "-F", "compcomp").returncode == 0
    assert _run("bundles", "add", "core", "--repos", "alpha,beta",
                "-F", "compcomp").returncode == 0

    r = _run("__complete", "--", "bundles", "show", "")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == ":0"
    assert "core" in lines

    # `task new --repos <TAB>` exposes both raw repo names and @bundle refs.
    r2 = _run("__complete", "--", "task", "new", "t",
              "-F", "compcomp", "--repos", "")
    assert r2.returncode == 0, r2.stderr
    lines2 = r2.stdout.splitlines()
    assert lines2[0] == ":4"  # DIRECTIVE_NOSPACE
    assert "@core" in lines2

    # `bundles edit core --add` excludes current members.
    r3 = _run("__complete", "--", "bundles", "edit", "core",
              "-F", "compcomp", "--add", "")
    assert r3.returncode == 0, r3.stderr
    lines3 = r3.stdout.splitlines()
    assert lines3[0] == ":4"
    assert "alpha" not in lines3
    assert "beta" not in lines3

    _run("fleets", "remove", "compcomp")


# ---------------------------------------------------------------------------
# Rendered scripts pass `bash -n` (syntax check)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="bash not installed or running on Windows (path translation breaks bash -n)",
)
def test_rendered_bash_script_parses(
    fleet_env_sandbox: Path, tmp_path: Path,
) -> None:
    r = _run("completion", "bash")
    assert r.returncode == 0
    script = tmp_path / "fleet.bash"
    script.write_text(r.stdout, encoding="utf-8")
    check = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True, text=True,
    )
    assert check.returncode == 0, check.stderr


@pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="bash not installed or running on Windows (chmod/shim semantics differ)",
)
def test_bash_completion_function_round_trip(
    fleet_env_sandbox: Path, tmp_path: Path,
) -> None:
    """Source the bash glue, invoke its function with a crafted COMP_WORDS,
    and assert COMPREPLY contains real subcommands."""
    # Render the script to disk so we can source it from a sub-bash.
    r = _run("completion", "bash")
    script = tmp_path / "fleet.bash"
    script.write_text(r.stdout, encoding="utf-8")

    # `fleet` must be on PATH for the glue to call back. The console-script
    # entry isn't guaranteed in CI, so define a thin shell shim that proxies
    # to `python -m fleet`.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "fleet"
    shim.write_text(
        f'#!/usr/bin/env bash\nexec "{sys.executable}" -m fleet "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    harness = textwrap.dedent(f"""
        export PATH="{bin_dir}:$PATH"
        source "{script}"
        COMP_WORDS=(fleet "")
        COMP_CWORD=1
        _fleet_complete
        printf '%s\\n' "${{COMPREPLY[@]}}"
    """).strip()

    env = os.environ.copy()
    out = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    cands = set(out.stdout.split())
    # Should contain at least the user-facing top-level commands.
    assert {"sync", "task", "fleets"}.issubset(cands)


# ---------------------------------------------------------------------------
# PowerShell comma-list round trip (regression for --repos a,b,<TAB>)
# ---------------------------------------------------------------------------

# PowerShell parses `a,b,c` as an ArrayLiteralExpression and sets the
# completion replacement span to ONLY the element after the last comma. The
# engine bakes the whole comma-head into each candidate (correct for
# bash/zsh, which replace the entire word). fleet.ps1 must strip that head so
# splicing extends the list instead of duplicating it
# (`alpha,beta,` + `alpha,beta,gamma` = `alpha,beta,alpha,beta,gamma`).
_PS_ROUND_TRIP = r"""
$ErrorActionPreference = 'Stop'
Import-Module '__PSM1__' -Force
$line = '__LINE__'
$res = [System.Management.Automation.CommandCompletion]::CompleteInput(
    $line, $line.Length, $null)
$texts = @($res.CompletionMatches | ForEach-Object { $_.CompletionText })
$first = if ($texts.Count -gt 0) { $texts[0] } else { '' }
$spliced = $line.Substring(0, $res.ReplacementIndex) + $first +
    $line.Substring($res.ReplacementIndex + $res.ReplacementLength)
Write-Output ("TEXTS=" + ($texts -join '|'))
Write-Output ("SPLICED=" + $spliced)
"""


@pytest.mark.skipif(
    shutil.which("pwsh") is None,
    reason="pwsh (PowerShell 7+) not installed",
)
def test_powershell_repos_comma_no_duplication(
    fleet_env_sandbox: Path, tmp_path: Path,
) -> None:
    """Drive the real Fleet.psm1 completer through TabExpansion2 and assert
    a comma-separated --repos value extends rather than duplicates its head."""
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    for name in ("alpha", "beta", "gamma"):
        d = repos_root / name
        d.mkdir()
        (d / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")
    assert _run("fleets", "add", "pscomma",
                "--root", str(repos_root)).returncode == 0
    assert _run("scan", "-F", "pscomma").returncode == 0

    psm1 = Path(__file__).resolve().parents[2] / "Fleet.psm1"
    assert psm1.is_file(), psm1
    line = "fleet task new t -F pscomma --repos alpha,beta,"
    script = (
        _PS_ROUND_TRIP
        .replace("__PSM1__", psm1.as_posix())
        .replace("__LINE__", line)
    )

    env = os.environ.copy()
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    _run("fleets", "remove", "pscomma")

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Candidate texts are tails only — no baked-in comma head.
    assert "TEXTS=gamma" in out, out
    # Splicing the first match extends the list; the head is not duplicated.
    assert (
        "SPLICED=fleet task new t -F pscomma --repos alpha,beta,gamma" in out
    ), out
    assert "alpha,beta,alpha" not in out


# ---------------------------------------------------------------------------
# Import-cost guard: build_parser runs on every <Tab> and must stay cheap
# ---------------------------------------------------------------------------

# The completion engine introspects the argparse tree built by
# ``fleet.cli.build_parser`` on every keystroke. Describing the CLI must NOT
# drag in the heavy command implementations — git_ops, the disk walker +
# discovery, the tasks worktree/manifest subtree, concurrent.futures, zipfile.
# Those load lazily on actual dispatch. If any of them creeps back onto a
# module top-level import, this guard fails before it can slow down <Tab>.
_FORBIDDEN_ON_PARSER_BUILD = [
    "fleet.discovery",
    "fleet.git_ops",
    "fleet.walker",
    "fleet.registry_tree",
    "fleet.tasks.manifest",
    "fleet.tasks.validation",
    "fleet.tasks.status",
    "fleet.tasks.worktree",
    "fleet.tasks.lifecycle",
    "fleet.tasks.edit",
    "fleet.tasks.inspect",
    "concurrent.futures",
    "zipfile",
]


def test_build_parser_stays_lean() -> None:
    """Guard the completion hot path: build_parser imports nothing heavy."""
    script = (
        "import sys, json\n"
        "import fleet.cli\n"
        "fleet.cli.build_parser()\n"
        f"forbidden = {_FORBIDDEN_ON_PARSER_BUILD!r}\n"
        "print(json.dumps([m for m in forbidden if m in sys.modules]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    leaked = json.loads(proc.stdout.strip().splitlines()[-1])
    assert leaked == [], (
        "build_parser pulled heavy modules onto the completion hot path: "
        f"{leaked}. Keep their imports inside handler bodies."
    )

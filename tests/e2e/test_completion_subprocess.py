"""End-to-end completion: spawn real `python -m fleet __complete ...`.

Verifies stdout discipline (only `:directive` + candidates), exit code 0
even on misconfigured environments, and that `fleet completion <shell>`
emits the expected glue script.
"""

from __future__ import annotations

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

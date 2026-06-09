"""Smoke tests: real ``python -m fleet`` subprocess.

These verify the end-to-end pipeline (CLI dispatch, env-var sandbox,
exit codes, stdout/stderr separation) without relying on direct calls
to internal handlers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleet import __version__


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001
    for item in items:
        item.add_marker(pytest.mark.e2e)


def _run(*args: str, env_extra: dict[str, str] | None = None,
         cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Subprocess `python -m fleet <args>` with the test sandbox env."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "fleet", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
        env=env,
    )


@pytest.fixture
def sandbox(fleet_env_sandbox: Path) -> Path:
    """E2E sandbox: reuses the autouse `fleet_env_sandbox` from tests/conftest.

    The global autouse fixture has already created the sandbox dir and set
    every relevant env var via monkeypatch — those env vars are inherited
    by the subprocesses we spawn here through ``os.environ.copy()``.
    """
    return fleet_env_sandbox


def test_help_exits_zero(sandbox) -> None:
    r = _run("--help")
    assert r.returncode == 0
    assert "Multi-repo workspace tool" in r.stdout


def test_version_exits_zero(sandbox) -> None:
    r = _run("--version")
    assert r.returncode == 0
    assert __version__ in r.stdout


def test_no_command_exits_nonzero(sandbox) -> None:
    r = _run()
    assert r.returncode != 0
    # argparse writes the usage error to stderr.
    assert "command" in r.stderr.lower() or "usage" in r.stderr.lower()


def test_unknown_command_exits_nonzero(sandbox) -> None:
    r = _run("totally-unknown-command")
    assert r.returncode != 0
    assert "invalid choice" in r.stderr.lower()


def test_fleets_list_empty(sandbox) -> None:
    r = _run("fleets", "list")
    assert r.returncode == 0
    assert "No fleets configured" in r.stdout


def test_open_command_errors_with_helpful_message(sandbox) -> None:
    r = _run("open", "ghost")
    assert r.returncode == 2
    # Either the PowerShell hint (Windows) or the bash hint (POSIX) is
    # printed to stderr — assert the universal "ERROR" prefix.
    assert "ERROR" in r.stderr


def test_no_fleets_yields_clear_error(sandbox) -> None:
    """Commands that need a fleet should error cleanly when none are configured."""
    r = _run("repos")
    assert r.returncode != 0
    assert "No fleets configured" in r.stderr


def test_full_lifecycle_end_to_end(sandbox, tmp_path) -> None:
    """Add fleet -> scan -> repos -> task new --dry-run -> fleets remove."""
    repos_root = tmp_path / "src"
    repos_root.mkdir()
    # Drop two fake repos via marker files; no real git needed for this test.
    for name in ("alpha", "beta"):
        d = repos_root / name
        d.mkdir()
        (d / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")

    r1 = _run("fleets", "add", "smoke", "--root", str(repos_root))
    assert r1.returncode == 0, r1.stderr

    r2 = _run("scan", "-F", "smoke")
    assert r2.returncode == 0, r2.stderr
    assert (repos_root / "fleet.json").is_file()

    r3 = _run("repos", "-F", "smoke")
    assert r3.returncode == 0, r3.stderr
    assert "alpha" in r3.stdout
    assert "beta" in r3.stdout

    # Note: real `task new` would invoke git on the marker repos and fail;
    # use --dry-run to validate parser + handler wiring without git work.
    r4 = _run("task", "new", "t1", "--repos", "alpha,beta",
              "-F", "smoke", "--dry-run", "--no-pull")
    assert r4.returncode == 0, r4.stderr

    r5 = _run("fleets", "remove", "smoke")
    assert r5.returncode == 0, r5.stderr


def test_bundles_crud_end_to_end(sandbox, tmp_path) -> None:
    """Register fleet -> bundles add/list/show/edit/remove + task new @bundle."""
    repos_root = tmp_path / "src2"
    repos_root.mkdir()
    for name in ("alpha", "beta"):
        d = repos_root / name
        d.mkdir()
        (d / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")

    assert _run("fleets", "add", "bsmoke",
                "--root", str(repos_root)).returncode == 0
    assert _run("scan", "-F", "bsmoke").returncode == 0

    r_empty = _run("bundles", "list", "-F", "bsmoke")
    assert r_empty.returncode == 0
    assert "No bundles configured" in r_empty.stdout

    r_add = _run("bundles", "add", "core",
                 "--repos", "alpha,beta", "-F", "bsmoke")
    assert r_add.returncode == 0, r_add.stderr
    assert (repos_root / "bundles.json").is_file()

    r_list = _run("bundles", "list", "-F", "bsmoke")
    assert r_list.returncode == 0
    assert "core" in r_list.stdout and "2 repo(s)" in r_list.stdout

    r_show = _run("bundles", "show", "core", "-F", "bsmoke")
    assert r_show.returncode == 0
    assert "alpha" in r_show.stdout and "beta" in r_show.stdout

    r_dup = _run("bundles", "add", "core",
                 "--repos", "alpha", "-F", "bsmoke")
    assert r_dup.returncode == 1
    assert "already exists" in r_dup.stderr

    r_edit = _run("bundles", "edit", "core",
                  "--remove", "beta", "-F", "bsmoke")
    assert r_edit.returncode == 0

    # task new --repos @core should expand via the bundle.
    r_new = _run("task", "new", "t-bundle", "--repos", "@core",
                 "-F", "bsmoke", "--dry-run", "--no-pull")
    assert r_new.returncode == 0, r_new.stderr
    # After the edit, only alpha remains in the bundle.
    assert "alpha" in r_new.stdout
    assert "beta" not in r_new.stdout

    r_unknown = _run("task", "new", "t-x", "--repos", "@nope",
                     "-F", "bsmoke", "--dry-run", "--no-pull")
    assert r_unknown.returncode == 1
    assert "Unknown bundle" in r_unknown.stderr

    r_rm = _run("bundles", "remove", "core", "-F", "bsmoke")
    assert r_rm.returncode == 0
    assert _run("bundles", "list", "-F", "bsmoke").stdout.count(
        "No bundles configured"
    ) == 1

    assert _run("fleets", "remove", "bsmoke").returncode == 0


def test_fleets_rename_end_to_end(sandbox, tmp_path) -> None:
    repos_root = tmp_path / "ren"
    repos_root.mkdir()
    assert _run("fleets", "add", "old",
                "--root", str(repos_root)).returncode == 0
    r = _run("fleets", "rename", "old", "fresh")
    assert r.returncode == 0, r.stderr
    listed = _run("fleets", "list")
    assert "fresh" in listed.stdout
    assert "old" not in listed.stdout.replace("fresh", "")
    assert _run("fleets", "remove", "fresh").returncode == 0


def test_bundles_rename_end_to_end(sandbox, tmp_path) -> None:
    repos_root = tmp_path / "bren"
    repos_root.mkdir()
    for name in ("alpha", "beta"):
        d = repos_root / name
        d.mkdir()
        (d / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")
    assert _run("fleets", "add", "brn", "--root", str(repos_root)).returncode == 0
    assert _run("scan", "-F", "brn").returncode == 0
    assert _run("bundles", "add", "core", "--repos", "alpha,beta",
                "-F", "brn").returncode == 0
    r = _run("bundles", "rename", "core", "renamed", "-F", "brn")
    assert r.returncode == 0, r.stderr
    listed = _run("bundles", "list", "-F", "brn")
    assert "renamed" in listed.stdout
    assert _run("fleets", "remove", "brn").returncode == 0


def test_repos_json_and_filter(sandbox, tmp_path) -> None:
    repos_root = tmp_path / "rj"
    repos_root.mkdir()
    for name in ("alpha-cli", "beta"):
        d = repos_root / name
        d.mkdir()
        (d / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")
    assert _run("fleets", "add", "rj", "--root", str(repos_root)).returncode == 0
    assert _run("scan", "-F", "rj").returncode == 0

    r_json = _run("repos", "--json", "-F", "rj")
    assert r_json.returncode == 0
    import json as _json
    lines = [ln for ln in r_json.stdout.splitlines() if ln.strip()]
    names = {_json.loads(ln)["name"] for ln in lines}
    assert names == {"alpha-cli", "beta"}

    r_filter = _run("repos", "--filter", "*-cli", "-F", "rj")
    assert r_filter.returncode == 0
    assert "alpha-cli" in r_filter.stdout
    assert "beta" not in r_filter.stdout
    assert _run("fleets", "remove", "rj").returncode == 0


def test_doctor_reports_problems(sandbox, tmp_path) -> None:
    repos_root = tmp_path / "doc"
    repos_root.mkdir()
    assert _run("fleets", "add", "doc", "--root", str(repos_root)).returncode == 0
    # Clean config (no fleet.json yet is a notice, not a problem) -> exit 0.
    r_ok = _run("doctor")
    assert r_ok.returncode == 0, r_ok.stderr
    assert "doc" in r_ok.stdout

    # Now break it: point a second fleet at a path, then delete the path.
    gone = tmp_path / "gone"
    gone.mkdir()
    assert _run("fleets", "add", "broken", "--root", str(gone)).returncode == 0
    import shutil as _shutil
    _shutil.rmtree(gone)
    r_bad = _run("doctor")
    assert r_bad.returncode == 2, r_bad.stdout
    assert "does not exist" in r_bad.stdout
    assert _run("fleets", "remove", "doc").returncode == 0
    assert _run("fleets", "remove", "broken").returncode == 0


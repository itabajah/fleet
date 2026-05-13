"""Sync's auth-probe failure path (unit-level: mock ls-remote)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from fleet import git_ops
from fleet.sync import cmd_sync


def _args(**overrides) -> argparse.Namespace:
    base = {"dry_run": False, "workers": 1, "no_auth_check": False, "fleet": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _result(ok: bool = True, stderr: str = "fatal: auth denied") -> git_ops.GitResult:
    return git_ops.GitResult(returncode=0 if ok else 128, stdout="", stderr=stderr)


def test_auth_probe_failure_culls_repos(populated_fleet, capsys,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed auth probe skips affected repos but still runs the rest."""
    # Stub ls-remote to fail for ALL hosts: there's only one host (the bare
    # remote dir on disk), so every probe fails and every repo is culled.
    monkeypatch.setattr(git_ops, "ls_remote_head",
                        lambda _p: _result(ok=False))
    rc = cmd_sync(_args())
    assert rc == 1
    out = capsys.readouterr().out
    assert "Authentication failed" in out
    assert "Every enabled repo is on a failed-auth host" in out


def test_auth_probe_disabled_skips_check(populated_fleet, capsys,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-auth-check skips the probe entirely."""
    called = {"n": 0}

    def fail(_p: Path) -> git_ops.GitResult:
        called["n"] += 1
        return _result(ok=False)

    monkeypatch.setattr(git_ops, "ls_remote_head", fail)
    rc = cmd_sync(_args(no_auth_check=True))
    assert rc == 0
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "Authentication Check" not in out

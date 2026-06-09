"""``fleet doctor`` config-load resilience + branch-convention reporting."""

from __future__ import annotations

import argparse
from pathlib import Path

from fleet.doctor import cmd_doctor
from fleet.errors import EXIT_OK, EXIT_PARTIAL
from fleet.fleets_config import FleetsConfig, config_path
from fleet.state import BranchConfig


def _write_config(text: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_doctor_reports_malformed_branch_as_problem(capsys) -> None:
    """A bad ``branch`` convention must surface as a doctor problem (exit 2),
    not crash the whole check with an uncaught FleetError."""
    _write_config('{"fleets": {}, "branch": {"prefix": "bad prefix"}}')
    rc = cmd_doctor(argparse.Namespace(fleet=None))
    assert rc == EXIT_PARTIAL
    out = capsys.readouterr().out
    assert "problem(s) found" in out
    assert "branch convention" in out.lower()


def test_doctor_shows_active_branch_convention(tmp_path: Path, capsys) -> None:
    """A valid custom convention is echoed in the Fleets section."""
    root = tmp_path / "repos"
    root.mkdir()
    cfg = FleetsConfig()
    cfg.add("demo", root)
    cfg.branch = BranchConfig(prefix="wip", scoped=False)
    cfg.save()
    rc = cmd_doctor(argparse.Namespace(fleet=None))
    # No fleet.json yet is a notice, not a problem.
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "branch convention: wip/<task>" in out

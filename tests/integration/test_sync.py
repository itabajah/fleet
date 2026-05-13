"""``fleet sync`` against real local bare remotes (no network)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from fleet.sync import cmd_sync
from helpers.git import _git, commit_file, make_dirty


def _sync_args(**overrides) -> argparse.Namespace:
    base = {
        "dry_run": False,
        "workers": 1,
        "no_auth_check": True,
        "fleet": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_sync_happy_path(populated_fleet,
                         capsys: pytest.CaptureFixture[str]) -> None:
    rc = cmd_sync(_sync_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "All operations completed successfully" in out
    assert "Successfully updated: 3" in out


def test_sync_dry_run_makes_no_changes(populated_fleet,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    # Touch the working tree so we can verify the dry-run doesn't mutate state.
    repo = populated_fleet.repos["alpha"]
    rc = cmd_sync(_sync_args(dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Would fetch and pull" in out
    # Working dir untouched.
    assert (repo / "README.md").is_file()


def test_sync_skips_dirty(populated_fleet,
                         capsys: pytest.CaptureFixture[str]) -> None:
    make_dirty(populated_fleet.repos["alpha"])
    rc = cmd_sync(_sync_args())
    assert rc == 0  # skip-only is still success overall
    out = capsys.readouterr().out
    assert "Uncommitted changes" in out
    assert "Skipped:              1" in out


def test_sync_skips_detached_head(populated_fleet,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    repo = populated_fleet.repos["alpha"]
    _git("checkout", "--detach", "HEAD", cwd=repo)
    rc = cmd_sync(_sync_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Detached HEAD" in out


def test_sync_pulls_in_new_commits(populated_fleet,
                                   capsys: pytest.CaptureFixture[str],
                                   tmp_path: Path) -> None:
    """Push a commit via a side-clone, then `fleet sync` should pull it."""
    repo = populated_fleet.repos["alpha"]
    # Clone the side from the BARE remote (clones of a working repo can't
    # push back to it because the destination's checked-out branch refuses).
    bare_url = _git("remote", "get-url", "origin", cwd=repo).stdout.strip()
    side = tmp_path / "_side"
    _git("clone", bare_url, str(side), cwd=tmp_path)
    _git("config", "user.email", "side@example.invalid", cwd=side)
    _git("config", "user.name", "side", cwd=side)
    commit_file(side, "from-side.txt")
    _git("push", "origin", "main", cwd=side)
    # `fleet sync` will fetch from the bare and FF-pull into `repo`.
    rc = cmd_sync(_sync_args())
    assert rc == 0
    capsys.readouterr()  # discard output
    assert (repo / "from-side.txt").is_file()


def test_sync_no_repos_to_sync_when_disabled(populated_fleet,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """Disable every repo via fleet.json -> sync returns success but warns."""
    import json
    (populated_fleet.root / "fleet.json").write_text(
        json.dumps({"root": ".", ".": {"sync": False}}) + "\n",
        encoding="utf-8",
    )
    rc = cmd_sync(_sync_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "No repositories enabled to sync" in out


def test_sync_workers_zero_caps_at_repos_count(populated_fleet,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    rc = cmd_sync(_sync_args(workers=0))
    assert rc == 0
    out = capsys.readouterr().out
    # 3 repos, --workers 0 -> 3 workers.
    assert "Processing 3 repositories with 3 worker(s)" in out


def test_sync_workers_negative_clamped(populated_fleet,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    rc = cmd_sync(_sync_args(workers=-99))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Processing 3 repositories with 1 worker(s)" in out

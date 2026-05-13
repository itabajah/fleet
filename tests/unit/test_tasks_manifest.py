"""Manifest dataclass: load / try_load / save round-trip + schema validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.errors import FleetError
from fleet.tasks.manifest import Manifest, RepoEntry, now_iso


def _good_manifest_dict(workspace: Path) -> dict:
    return {
        "name": "task-1",
        "branch": "task/demo/task-1",
        "created_at": now_iso(),
        "description": "test",
        "repos": [
            {
                "name": "alpha",
                "group": None,
                "canonical_path": str(workspace / "_canon"),
                "worktree_path": str(workspace / "alpha"),
            },
        ],
    }


def test_now_iso_is_valid_isoformat() -> None:
    s = now_iso()
    # Should round-trip through datetime parsing without error.
    from datetime import datetime
    datetime.fromisoformat(s)


def test_load_round_trip(tmp_path: Path) -> None:
    payload = _good_manifest_dict(tmp_path)
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    m = Manifest.load(tmp_path)
    assert m.name == "task-1"
    assert m.branch == "task/demo/task-1"
    assert len(m.repos) == 1
    assert m.repos[0].name == "alpha"
    assert m.repos[0].display_name == "alpha"


def test_load_with_group(tmp_path: Path) -> None:
    payload = _good_manifest_dict(tmp_path)
    payload["repos"][0]["group"] = "g/sub"
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    m = Manifest.load(tmp_path)
    assert m.repos[0].group == "g/sub"
    assert m.repos[0].display_name == "g/sub/alpha"


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FleetError, match="missing or unreadable"):
        Manifest.load(tmp_path)


def test_load_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "task.json").write_text("not json {", encoding="utf-8")
    with pytest.raises(FleetError, match="Malformed task.json"):
        Manifest.load(tmp_path)


def test_load_top_level_not_dict(tmp_path: Path) -> None:
    (tmp_path / "task.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(FleetError, match="not an object"):
        Manifest.load(tmp_path)


@pytest.mark.parametrize("missing", ["name", "branch"])
def test_load_missing_required(tmp_path: Path, missing: str) -> None:
    payload = _good_manifest_dict(tmp_path)
    del payload[missing]
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FleetError, match=missing):
        Manifest.load(tmp_path)


def test_load_blank_branch_rejected(tmp_path: Path) -> None:
    payload = _good_manifest_dict(tmp_path)
    payload["branch"] = ""
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FleetError, match="missing the required 'branch'"):
        Manifest.load(tmp_path)


def test_load_repos_must_be_list(tmp_path: Path) -> None:
    payload = _good_manifest_dict(tmp_path)
    payload["repos"] = {"alpha": {}}
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FleetError, match="'repos' must be a list"):
        Manifest.load(tmp_path)


def test_load_repo_entry_must_be_object(tmp_path: Path) -> None:
    payload = _good_manifest_dict(tmp_path)
    payload["repos"] = ["not-an-object"]
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FleetError, match="not an object"):
        Manifest.load(tmp_path)


def test_load_repo_entry_missing_paths(tmp_path: Path) -> None:
    payload = _good_manifest_dict(tmp_path)
    del payload["repos"][0]["canonical_path"]
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FleetError, match="missing 'canonical_path'"):
        Manifest.load(tmp_path)


def test_try_load_returns_none_on_failure(tmp_path: Path) -> None:
    assert Manifest.try_load(tmp_path) is None


def test_save_then_load(tmp_path: Path) -> None:
    m = Manifest(
        name="t1",
        branch="task/demo/t1",
        created_at=now_iso(),
        description="hello",
        repos=[RepoEntry(name="alpha", group=None,
                         canonical_path=tmp_path / "c",
                         worktree_path=tmp_path / "alpha")],
    )
    m.save(tmp_path)
    loaded = Manifest.load(tmp_path)
    assert loaded.name == m.name
    assert loaded.branch == m.branch
    assert loaded.description == "hello"
    assert loaded.repos[0].canonical_path == tmp_path / "c"


def test_save_atomic_no_tmp_left(tmp_path: Path) -> None:
    m = Manifest(name="t", branch="b", created_at="x", description="",
                 repos=[])
    m.save(tmp_path)
    assert (tmp_path / "task.json").is_file()
    # No `.tmp` should be left behind.
    assert list(tmp_path.glob("*.tmp")) == []


def test_repo_entry_display_name() -> None:
    e = RepoEntry(name="alpha", group=None,
                  canonical_path=Path("/a"), worktree_path=Path("/b"))
    assert e.display_name == "alpha"
    e2 = RepoEntry(name="alpha", group="g/sub",
                   canonical_path=Path("/a"), worktree_path=Path("/b"))
    assert e2.display_name == "g/sub/alpha"

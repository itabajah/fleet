"""Focused tests for ``fleet.sync`` internals (probe + retry semantics)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fleet import git_ops, sync
from fleet.discovery import RepoInfo


def _ri(name: str, path: Path) -> RepoInfo:
    return RepoInfo(name=name, group_path="", path=path, enabled=True,
                    in_registry=True)


def _ok() -> git_ops.GitResult:
    return git_ops.GitResult(returncode=0, stdout="", stderr="")


def _transient() -> git_ops.GitResult:
    return git_ops.GitResult(returncode=1, stdout="", stderr="503 Service Unavailable")


def _fatal() -> git_ops.GitResult:
    return git_ops.GitResult(returncode=128, stdout="", stderr="fatal: bad")


def _warning() -> git_ops.GitResult:
    return git_ops.GitResult(returncode=1, stdout="",
                             stderr="warning: case-insensitive filesystem")


# ---------------------------------------------------------------------------
# _gather_host_info
# ---------------------------------------------------------------------------

def test_gather_host_info_classifies_states(tmp_path: Path) -> None:
    repo = _ri("a", tmp_path / "a")
    not_repo = _ri("b", tmp_path / "b")
    no_origin = _ri("c", tmp_path / "c")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".git").mkdir()
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / ".git").mkdir()

    with patch.object(git_ops, "origin_url", side_effect=lambda p: (
            "https://example.com/x.git" if p == repo.path else None
        )):
        info = sync._gather_host_info([repo, not_repo, no_origin])

    assert info[id(repo)].is_repo and info[id(repo)].host == "example.com"
    assert info[id(not_repo)].is_repo is False
    assert info[id(no_origin)].is_repo is True
    assert info[id(no_origin)].has_origin is False


# ---------------------------------------------------------------------------
# _probe_host
# ---------------------------------------------------------------------------

def test_probe_host_succeeds_first_try(tmp_path: Path) -> None:
    repo = _ri("a", tmp_path)
    with patch.object(git_ops, "ls_remote_head", return_value=_ok()):
        ok, err = sync._probe_host(repo)
    assert ok and err == ""


def test_probe_host_treats_warning_as_success(tmp_path: Path) -> None:
    repo = _ri("a", tmp_path)
    with patch.object(git_ops, "ls_remote_head", return_value=_warning()):
        ok, _ = sync._probe_host(repo)
    assert ok is True


def test_probe_host_retries_transient_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _ri("a", tmp_path)
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)
    seq = [_transient(), _ok()]
    with patch.object(git_ops, "ls_remote_head", side_effect=seq):
        ok, _ = sync._probe_host(repo)
    assert ok is True


def test_probe_host_fails_on_non_transient(tmp_path: Path) -> None:
    repo = _ri("a", tmp_path)
    with patch.object(git_ops, "ls_remote_head", return_value=_fatal()):
        ok, err = sync._probe_host(repo)
    assert ok is False
    assert "fatal" in err


def test_probe_host_exhausts_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _ri("a", tmp_path)
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)
    with patch.object(git_ops, "ls_remote_head", return_value=_transient()) as m:
        ok, _ = sync._probe_host(repo)
    assert ok is False
    assert m.call_count == sync._MAX_RETRIES


# ---------------------------------------------------------------------------
# _retry_call
# ---------------------------------------------------------------------------

def test_retry_call_first_try_success() -> None:
    calls = {"n": 0}

    def fn() -> git_ops.GitResult:
        calls["n"] += 1
        return _ok()

    result = sync._retry_call("Op", fn, output=[])
    assert result.ok
    assert calls["n"] == 1


def test_retry_call_warning_only_returns_first(monkeypatch) -> None:
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fn() -> git_ops.GitResult:
        calls["n"] += 1
        return _warning()

    out: list = []
    sync._retry_call("Op", fn, out)
    assert calls["n"] == 1
    assert any("safe to ignore" in line.text for line in out)


def test_retry_call_transient_then_success(monkeypatch) -> None:
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)
    seq = iter([_transient(), _ok()])
    out: list = []
    result = sync._retry_call("Op", lambda: next(seq), out)
    assert result.ok
    assert any("retrying" in line.text for line in out)


def test_retry_call_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)
    out: list = []
    result = sync._retry_call("Op", lambda: _transient(), out)
    assert result.ok is False


# ---------------------------------------------------------------------------
# _print_lines + _color_for round-trip
# ---------------------------------------------------------------------------

def test_color_for_unknown_is_identity() -> None:
    fn = sync._color_for("nope")
    assert fn("x") == "x"


def test_print_lines_emits_each_line(capsys) -> None:
    out = [sync._Line("hello", "green"), sync._Line("world", "")]
    sync._print_lines(out)
    captured = capsys.readouterr().out
    assert "hello" in captured
    assert "world" in captured

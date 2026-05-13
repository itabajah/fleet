"""Pure helpers from git_ops: regex classifiers + URL parsing."""

from __future__ import annotations

import pytest

from fleet.git_ops import (
    GitResult,
    is_transient_error,
    is_warning_only,
    origin_host,
)


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/owner/repo.git", "github.com"),
    ("https://GitHub.com/owner/repo", "github.com"),
    ("https://user@dev.azure.com/org/proj/_git/repo",
     "dev.azure.com"),
    ("git@github.com:owner/repo.git", "github.com"),
    ("ssh://git@gitlab.example.com/owner/repo.git", "gitlab.example.com"),
    ("ssh://git@gitlab.example.com:2222/owner/repo.git", "gitlab.example.com"),
])
def test_origin_host_known_shapes(url: str, expected: str) -> None:
    assert origin_host(url) == expected


def test_origin_host_falls_back_to_lowercased_url() -> None:
    weird = "totally-bizarre-string"
    assert origin_host(weird) == weird


def _result(stderr: str = "", returncode: int = 1) -> GitResult:
    return GitResult(returncode=returncode, stdout="", stderr=stderr)


@pytest.mark.parametrize("text", [
    "warning: case-insensitive filesystem detected",
    "warning: reftable: degraded performance",
    "WARNING: warning detected",
])
def test_is_warning_only_matches(text: str) -> None:
    assert is_warning_only(_result(stderr=text)) is True


@pytest.mark.parametrize("text", [
    "fatal: not a git repository",
    "error: pathspec did not match",
    "remote: Permission to repo denied",
    "warning: but also fatal: now we have an error",
])
def test_is_warning_only_rejects_real_errors(text: str) -> None:
    assert is_warning_only(_result(stderr=text)) is False


def test_is_warning_only_rejects_when_no_warning() -> None:
    assert is_warning_only(_result(stderr="just a benign note")) is False


@pytest.mark.parametrize("text", [
    "fatal: unable to access 'https://x': failed to connect to host",
    "Couldn't connect: 503 Service Unavailable",
    "remote: 502 Bad Gateway",
    "fatal: unable to access: timeout reached",
    "Connection refused",
    "504 Gateway Time-out",
    "temporarily unavailable",
])
def test_is_transient_error_matches(text: str) -> None:
    assert is_transient_error(_result(stderr=text)) is True


@pytest.mark.parametrize("text", [
    "fatal: not a git repository",
    "error: branch already exists",
    "ok",
])
def test_is_transient_error_rejects(text: str) -> None:
    assert is_transient_error(_result(stderr=text)) is False

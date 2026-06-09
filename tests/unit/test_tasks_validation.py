"""Task name validation, ``--repos`` token resolution, branch builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.discovery import RepoInfo
from fleet.errors import FleetError
from fleet.state import set_active_fleet
from fleet.tasks.validation import (
    resolve_repo,
    task_branch,
    validate_branch,
    validate_task_name,
)

# ---------------------------------------------------------------------------
# validate_task_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", [
    "a", "Bug123", "x_y-z.1", "x" * 64,
])
def test_valid_names(good: str) -> None:
    validate_task_name(good)


@pytest.mark.parametrize("bad", [
    "", " spaces ", "x" * 65,
    "_leading", "-leading", ".leading",
    "trailing.", "trailing.lock",
    "a..b", "a@{b",
    "weird@", "with/slash",
])
def test_invalid_names(bad: str) -> None:
    with pytest.raises(FleetError):
        validate_task_name(bad)


@pytest.mark.parametrize("reserved", [
    "CON", "con", "PRN", "AUX", "NUL",
    "COM1", "LPT1", "com9", "lpt9",
    "CON.txt", "nul.log",
])
def test_rejects_windows_reserved_names(reserved: str) -> None:
    with pytest.raises(FleetError, match="reserved device name"):
        validate_task_name(reserved)



# ---------------------------------------------------------------------------
# task_branch
# ---------------------------------------------------------------------------

def test_task_branch_uses_active_fleet(tmp_path: Path) -> None:
    set_active_fleet("alpha", tmp_path)
    assert task_branch("bug-1") == "task/alpha/bug-1"


def test_task_branch_raises_without_active_fleet() -> None:
    with pytest.raises(FleetError):
        task_branch("bug-1")


# ---------------------------------------------------------------------------
# resolve_repo
# ---------------------------------------------------------------------------

def _r(name: str, group: str = "", *, enabled: bool = True,
       in_registry: bool = True) -> RepoInfo:
    return RepoInfo(name=name, group_path=group, path=Path("/x"),
                    enabled=enabled, in_registry=in_registry)


def test_resolve_exact_match() -> None:
    repos = [_r("alpha"), _r("beta")]
    assert resolve_repo("alpha", repos).name == "alpha"


def test_resolve_unknown_with_suggestion() -> None:
    repos = [_r("alpha"), _r("beta")]
    with pytest.raises(FleetError, match="Did you mean: alpha"):
        resolve_repo("alphz", repos)


def test_resolve_unknown_without_suggestion() -> None:
    repos = [_r("alpha")]
    with pytest.raises(FleetError, match=r"^Unknown repo 'zzzqqq'\.$"):
        resolve_repo("zzzqqq", repos)


def test_resolve_ambiguous_lists_groups() -> None:
    repos = [_r("alpha", "group-a"), _r("alpha", "group-b")]
    with pytest.raises(FleetError, match="Ambiguous repo name 'alpha'"):
        resolve_repo("alpha", repos)


def test_resolve_disambiguate_with_group() -> None:
    repos = [_r("alpha", "group-a"), _r("alpha", "group-b")]
    chosen = resolve_repo("group-a/alpha", repos)
    assert chosen.group_path == "group-a"


def test_resolve_grouped_unknown() -> None:
    repos = [_r("alpha", "group-a")]
    with pytest.raises(FleetError, match="Unknown repo 'group-x/alpha'"):
        resolve_repo("group-x/alpha", repos)


def test_resolve_disabled_rejected() -> None:
    repos = [_r("alpha", enabled=False)]
    with pytest.raises(FleetError, match="disabled in fleet.json"):
        resolve_repo("alpha", repos)


def test_resolve_normalises_backslashes() -> None:
    repos = [_r("alpha", "group/sub")]
    chosen = resolve_repo("group\\sub\\alpha", repos)
    assert chosen.group_path == "group/sub"


def test_resolve_strips_leading_trailing_slashes() -> None:
    repos = [_r("alpha")]
    chosen = resolve_repo("/alpha/", repos)
    assert chosen.name == "alpha"


# ---------------------------------------------------------------------------
# validate_branch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", [
    "main", "task/alpha/bug-1", "feature/x.y", "release/1.2.3",
])
def test_validate_branch_accepts_valid(good: str) -> None:
    validate_branch(good)


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "-flag-like",             # CLI-flag hazard
    "has space",              # whitespace
    "tilde~name", "caret^x",  # git-special chars
    "colon:bad", "star*bad", "bracket[bad",
    "back\\slash",
    ".leading", "trailing.", "trailing.lock",
    "double..dot", "with@{ref",
    "/leading-slash", "trailing-slash/", "double//slash",
])
def test_validate_branch_rejects_invalid(bad: str) -> None:
    with pytest.raises(FleetError):
        validate_branch(bad)

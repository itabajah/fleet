"""FleetError carries a message and an exit code."""

from __future__ import annotations

import pytest

from fleet.errors import FleetError


def test_default_exit_code_is_one() -> None:
    err = FleetError("boom")
    assert str(err) == "boom"
    assert err.exit_code == 1


def test_custom_exit_code_preserved() -> None:
    err = FleetError("partial", exit_code=2)
    assert err.exit_code == 2


def test_is_an_exception() -> None:
    with pytest.raises(FleetError) as excinfo:
        raise FleetError("raise me", exit_code=3)
    assert excinfo.value.exit_code == 3

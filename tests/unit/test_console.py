"""Color helpers honour NO_COLOR / FORCE_COLOR / TERM=dumb / isatty."""

from __future__ import annotations

import sys
from io import StringIO

import pytest

from fleet import console


@pytest.fixture
def force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force-enable color via the well-known env var (works under pytest capture)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")


def test_no_color_env_disables_color(force_color: None,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert console.use_color() is False
    assert console.red("x") == "x"


def test_term_dumb_disables_color(force_color: None,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert console.use_color() is False


def test_force_color_overrides_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    # No fake TTY: stdout is whatever pytest captured; isatty() returns False.
    assert console.use_color() is True
    assert console.red("x") == "\x1b[31mx\x1b[0m"


def test_isatty_true_enables_color(monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # Pytest captures stdout by default; disable capture so our patched
    # `sys.stdout` is what `use_color()` sees.
    with capsys.disabled():
        class _TTYIO(StringIO):
            def isatty(self) -> bool:
                return True
        monkeypatch.setattr(sys, "stdout", _TTYIO())
        assert console.use_color() is True


def test_no_isatty_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # Pytest's capture object doesn't have isatty()=True, so no color.
    assert console.use_color() is False


def test_no_color_beats_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """The NO_COLOR convention takes precedence over FORCE_COLOR."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert console.use_color() is False


@pytest.mark.parametrize("fn,code", [
    (console.red, "31"),
    (console.green, "32"),
    (console.yellow, "33"),
    (console.magenta, "35"),
    (console.cyan, "36"),
    (console.gray, "90"),
    (console.dim, "2"),
])
def test_color_codes_when_enabled(force_color: None, fn, code: str) -> None:
    assert fn("x") == f"\x1b[{code}mx\x1b[0m"

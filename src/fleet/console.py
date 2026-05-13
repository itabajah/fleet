"""Tiny ANSI color helpers.

Color is decided per call (not at import time) so tests and downstream
processes can flip ``NO_COLOR`` / ``FORCE_COLOR`` mid-run and have it take
effect. Resolution order matches the de-facto convention:

  1. ``NO_COLOR`` set (any value)        -> never colorize.
  2. ``TERM=dumb``                        -> never colorize.
  3. ``FORCE_COLOR`` set (any value)      -> always colorize.
  4. Otherwise                            -> colorize iff stdout is a TTY.
"""

from __future__ import annotations

import os
import sys


def use_color() -> bool:
    """Return True when ANSI color codes should be emitted."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _c(text: str, code: str) -> str:
    if not use_color():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def red(s: str) -> str: return _c(s, "31")
def green(s: str) -> str: return _c(s, "32")
def yellow(s: str) -> str: return _c(s, "33")
def magenta(s: str) -> str: return _c(s, "35")
def cyan(s: str) -> str: return _c(s, "36")
def gray(s: str) -> str: return _c(s, "90")
def dim(s: str) -> str: return _c(s, "2")

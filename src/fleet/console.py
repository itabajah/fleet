"""Tiny ANSI color helpers.

Color is decided per call (not at import time) so tests and downstream
processes can flip ``NO_COLOR`` / ``FORCE_COLOR`` mid-run and have it take
effect. Resolution order matches the de-facto convention:

  1. ``NO_COLOR`` set (any value)        -> never colorize.
  2. ``TERM=dumb``                        -> never colorize.
  3. ``FORCE_COLOR`` set (any value)      -> always colorize.
  4. Otherwise                            -> colorize iff the target stream
                                             is a TTY.

The default target stream is ``stdout``; pass ``stream=sys.stderr`` (or use
:func:`eprint`) for error output so messages stay colored when stdout is
piped but stderr is still a terminal (``fleet sync | tee log``).
"""

from __future__ import annotations

import os
import sys
from typing import TextIO


def use_color(stream: TextIO | None = None) -> bool:
    """Return True when ANSI color codes should be emitted to ``stream``."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    target = stream if stream is not None else sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


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


def eprint(text: str, *, color: str = "") -> None:
    """Print to stderr, coloring based on whether *stderr* is a TTY.

    ``color`` is an ANSI SGR code (e.g. ``"31"`` for red); empty means no
    color. Using stderr's own TTY state keeps error messages readable when
    stdout is redirected but the terminal still shows stderr.
    """
    if color and use_color(sys.stderr):
        text = f"\x1b[{color}m{text}\x1b[0m"
    print(text, file=sys.stderr)

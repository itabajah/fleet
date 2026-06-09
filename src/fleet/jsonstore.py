"""Shared JSON persistence helpers: decode-with-clear-errors + atomic write.

Centralises two patterns every on-disk config in the package performs
identically (``fleets.json``, ``bundles.json``, ``task.json``, ``fleet.json``):

  * :func:`read_json` — ``json.loads`` tolerating a UTF-8 BOM, wrapping a
    :class:`~json.JSONDecodeError` in a :class:`FleetError` with a
    caller-supplied label.
  * :func:`write_json_atomic` — temp-file + :func:`os.replace` so a crash
    mid-write can't truncate an existing file.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fleet.errors import FleetError


def read_json(path: Path, *, what: str) -> Any:
    """Read and parse JSON from ``path`` (tolerating a UTF-8 BOM).

    ``what`` names the source for the error message, e.g.
    ``"fleets config at /…/fleets.json"``. A malformed file raises
    :class:`FleetError`; the caller checks existence first.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise FleetError(f"Malformed {what}: {e}") from e


def write_json_atomic(path: Path, payload: object) -> None:
    """Write ``payload`` as pretty JSON to ``path`` via a temp file + rename.

    Creates the parent directory, writes ``<path>.tmp``, then
    :func:`os.replace`-s it into place. On any :class:`OSError` the temp file
    is cleaned up and the error re-raised for the caller to translate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# Cross-process config lock
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05


@contextlib.contextmanager
def config_lock(path: Path) -> Iterator[None]:
    """Cross-process advisory lock around a JSON config file.

    Uses an exclusive-create sidecar (``<path>.lock``) so concurrent writers
    (e.g. ``fleet fleets add`` / ``fleet bundles add``) serialize their
    read-modify-write rather than losing updates. Best-effort: gives up after
    ``_LOCK_TIMEOUT_SECONDS`` and proceeds anyway, so a stale lock from a
    crashed process never permanently wedges the CLI.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Stale lock fallback: proceed without locking. Two writers
                # can still collide here, but better than hanging forever.
                # Surface the situation so the user can clean up a stale
                # lock from a crashed peer process.
                print(
                    f"WARN: acquired {path.name} without lock after "
                    f"{_LOCK_TIMEOUT_SECONDS:g}s waiting on {lock_path}; "
                    f"concurrent writers may race. "
                    f"Delete the lock file if no other fleet process is running.",
                    file=sys.stderr,
                )
                fd = None
                break
            time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()

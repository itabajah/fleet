"""User-visible error type carrying a process exit code."""

from __future__ import annotations


class FleetError(Exception):
    """Raised for anything the user is meant to see and the CLI should exit on.

    ``exit_code`` is what ``cli.main()`` returns to the OS. Default 1 covers
    "generic operational failure"; specific call sites may pick 2 to mark
    "command ran but produced partial-failure" results (mirrors what some
    ``fleet task ...`` paths do).
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code

"""User-visible error type carrying a process exit code.

Exit-code convention used throughout the CLI:

  * :data:`EXIT_OK` (0)        — success.
  * :data:`EXIT_ERROR` (1)     — a hard error; nothing usable happened.
  * :data:`EXIT_PARTIAL` (2)   — the command ran but some units failed or were
                                 skipped (e.g. ``fleet sync`` with a failed repo,
                                 ``fleet task end`` that couldn't remove a
                                 worktree, ``fleet doctor`` finding problems).
  * :data:`EXIT_INTERRUPT` (130) — interrupted by Ctrl-C (128 + SIGINT).
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_INTERRUPT = 130


class FleetError(Exception):
    """Raised for anything the user is meant to see and the CLI should exit on.

    ``exit_code`` is what ``cli.main()`` returns to the OS. Default
    :data:`EXIT_ERROR` covers "generic operational failure"; specific call
    sites may pick :data:`EXIT_PARTIAL` to mark "command ran but produced
    partial-failure" results (mirrors what some ``fleet task ...`` paths do).
    """

    def __init__(self, message: str, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code

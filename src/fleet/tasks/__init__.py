"""Task workspace commands: ``new``, ``list``, ``info``, ``sync``, ``end``, ``path``.

A *task* is a folder under ``<TASKS_ROOT>/<fleet>/`` containing:
  - one git worktree per chosen repo (each on a fresh
    ``task/<fleet>/<name>`` branch),
  - ``context.md`` (free-form notes seed),
  - ``scratch/`` (free-form scratch dir),
  - ``task.json`` (manifest read by all task commands).

This package splits the implementation across:
  - :mod:`fleet.tasks.manifest`    — Manifest dataclass + atomic load/save
  - :mod:`fleet.tasks.validation`  — name validation + ``--repos`` resolution
  - :mod:`fleet.tasks.lifecycle`   — ``new`` / ``end`` (mutating)
  - :mod:`fleet.tasks.inspect`     — ``list`` / ``info`` / ``sync`` / ``path``
"""

from __future__ import annotations

import argparse

from fleet.tasks.edit import cmd_add_repo, cmd_edit, cmd_remove_repo, cmd_rename
from fleet.tasks.inspect import cmd_info, cmd_list, cmd_path, cmd_sync
from fleet.tasks.lifecycle import cmd_end, cmd_new

__all__ = [
    "cmd_new", "cmd_list", "cmd_info", "cmd_sync", "cmd_end", "cmd_path",
    "cmd_add_repo", "cmd_remove_repo", "cmd_rename", "cmd_edit",
]


def register(subparsers: argparse._SubParsersAction,
             fleet_arg: argparse.ArgumentParser) -> argparse._SubParsersAction:
    """Register the ``fleet task`` command group.

    Returns the inner sub-action so the caller (``cli.build_parser``) can
    attach additional ``task <foo>`` stubs without reaching into argparse
    private attributes.
    """
    p = subparsers.add_parser("task", help="manage task workspaces")
    sub = p.add_subparsers(dest="task_cmd", required=True, metavar="<command>")

    # task new
    s_new = sub.add_parser(
        "new", parents=[fleet_arg],
        help="create a new task workspace with worktrees per repo",
    )
    s_new.add_argument("name", help="task name (folder + branch suffix)")
    s_new.add_argument("--repos", required=True,
                       help="comma-separated repo names "
                            "(use group/path/name to disambiguate)")
    s_new.add_argument("--description", "-d", default="",
                       help="seed text for context.md")
    s_new.add_argument("--no-pull", action="store_true",
                       help="skip fetch + pull on each canonical repo "
                            "(uses local refs only; offline-safe)")
    s_new.add_argument("--dry-run", action="store_true",
                       help="validate inputs and print the plan without "
                            "creating anything")
    s_new.set_defaults(func=cmd_new)

    # task list
    s_list = sub.add_parser(
        "list", parents=[fleet_arg],
        help="list active task workspaces in the active fleet",
    )
    s_list.add_argument("--quick", action="store_true",
                        help="skip dirty/unpushed status checks")
    s_list.add_argument("--json", dest="as_json", action="store_true",
                        help="emit one JSON object per line on stdout "
                             "(implies --quick)")
    s_list.set_defaults(func=cmd_list)

    # task info
    s_info = sub.add_parser(
        "info", parents=[fleet_arg],
        help="show detailed status of one task",
    )
    s_info.add_argument("name", help="task name")
    s_info.set_defaults(func=cmd_info)

    # task sync
    s_sync = sub.add_parser(
        "sync", parents=[fleet_arg],
        help="fetch + ff-pull each worktree on its task branch",
    )
    s_sync.add_argument("name", help="task name")
    s_sync.set_defaults(func=cmd_sync)

    # task end
    s_end = sub.add_parser(
        "end", parents=[fleet_arg],
        help="archive and tear down a task workspace",
    )
    s_end.add_argument("name", help="task name")
    s_end.add_argument("--force", action="store_true",
                       help="proceed even if a worktree is dirty")
    s_end.set_defaults(func=cmd_end)

    # task add-repo
    s_add = sub.add_parser(
        "add-repo", parents=[fleet_arg],
        help="add one or more repos (new worktrees) to an existing task",
    )
    s_add.add_argument("name", help="task name")
    s_add.add_argument("--repos", required=True,
                       help="comma-separated repo names "
                            "(use group/path/name to disambiguate)")
    s_add.add_argument("--no-pull", action="store_true",
                       help="skip fetch + pull on each canonical repo")
    s_add.add_argument("--dry-run", action="store_true",
                       help="validate inputs and print the plan without "
                            "creating anything")
    s_add.set_defaults(func=cmd_add_repo)

    # task remove-repo
    s_rm = sub.add_parser(
        "remove-repo", parents=[fleet_arg],
        help="remove one or more repos (tear down worktrees) from a task",
    )
    s_rm.add_argument("name", help="task name")
    s_rm.add_argument("--repos", required=True,
                     help="comma-separated repo names currently in the task")
    s_rm.add_argument("--force", action="store_true",
                     help="proceed even if a worktree is dirty / unpushed")
    s_rm.add_argument("--dry-run", action="store_true",
                     help="print the plan without removing anything")
    s_rm.set_defaults(func=cmd_remove_repo)

    # task rename
    s_ren = sub.add_parser(
        "rename", parents=[fleet_arg],
        help="rename a task: move workspace + rename branch in every canonical",
    )
    s_ren.add_argument("old", help="current task name")
    s_ren.add_argument("new", help="new task name")
    s_ren.set_defaults(func=cmd_rename)

    # task edit
    s_edit = sub.add_parser(
        "edit", parents=[fleet_arg],
        help="update a task's description in task.json (and context.md)",
    )
    s_edit.add_argument("name", help="task name")
    grp = s_edit.add_mutually_exclusive_group()
    grp.add_argument("--description", "-d", default=None,
                     help="new description text")
    grp.add_argument("--description-file", default=None, metavar="PATH",
                     help="read description from PATH (use '-' for stdin)")
    s_edit.set_defaults(func=cmd_edit)

    # task path  (prints absolute workspace path; used by `fleet open`)
    s_path = sub.add_parser(
        "path", parents=[fleet_arg],
        help="print the absolute path to a task workspace",
    )
    s_path.add_argument("name", help="task name")
    s_path.set_defaults(func=cmd_path)

    return sub

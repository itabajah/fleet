"""Tab-completion engine for the ``fleet`` CLI.

Single source of truth for shell completion across PowerShell, bash, and
zsh. Thin shell glue (in :mod:`fleet.completions`) shells out to
``fleet __complete -- <words...>`` and renders the candidates.

Output protocol (cobra-compatible subset)::

    :<directive>\n
    candidate1\n
    candidate2\n
    ...

The first line is always ``:<int>`` where ``<int>`` is a bitmask. Today
only bit 2 (value 4) is meaningful: ``DIRECTIVE_NOSPACE`` tells the shell
not to append a trailing space (used for ``--repos a,b,<Tab>`` so the user
can keep typing comma-separated segments).

Structure (subcommands + flags) is discovered by **introspecting** the
argparse tree built by :func:`fleet.cli.build_parser`, so new commands
get completion for free. Dynamic value domains (fleet names, task names,
repo names) are hand-wired in :data:`POS_PROVIDERS` / :data:`OPT_PROVIDERS`
— there are only three of them and they touch state the parser doesn't
know about.

Hard rule: this module must NEVER raise from :func:`render` and must
NEVER write to stderr. A misconfigured environment yields an empty
candidate list. Every dynamic provider already swallows its own errors.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import threading
from collections.abc import Callable, Iterable, Sequence

DIRECTIVE_DEFAULT = 0
DIRECTIVE_NOSPACE = 4

# Hard ceiling on how long dynamic completion may run before we give up and
# return nothing. A slow disk walk or a hung network mount must never freeze
# the user's shell waiting on <Tab>.
_COMPLETION_TIMEOUT_SECONDS = 1.5

# ---------------------------------------------------------------------------
# Parser introspection helpers
# ---------------------------------------------------------------------------

def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _option_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions if a.option_strings]


def _positional_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [
        a for a in parser._actions
        if not a.option_strings
        and not isinstance(a, argparse._SubParsersAction)
        and a.dest != argparse.SUPPRESS
    ]


def _option_takes_value(action: argparse.Action) -> bool:
    """True if this option consumes a following positional value."""
    if isinstance(action, (
        argparse._StoreTrueAction,
        argparse._StoreFalseAction,
        argparse._HelpAction,
        argparse._VersionAction,
        argparse._CountAction,
    )):
        return False
    # nargs=0 means it's a flag with no value (defensive — store_true et al.
    # are already caught above).
    return action.nargs != 0


def _find_option(
    parser: argparse.ArgumentParser, token: str,
) -> argparse.Action | None:
    for a in _option_actions(parser):
        if token in a.option_strings:
            return a
    return None


# ---------------------------------------------------------------------------
# Dynamic value providers — each returns sorted list[str], never raises
# ---------------------------------------------------------------------------

def _fleet_names(_words: Sequence[str]) -> list[str]:
    try:
        from fleet.fleets_config import FleetsConfig
        return sorted(FleetsConfig.load().fleets)
    except Exception:
        return []


def _activate_fleet_from_words(words: Sequence[str]) -> bool:
    """Resolve ``-F``/``--fleet`` (or default) and pin it. Returns success."""
    override: str | None = None
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("-F", "--fleet") and i + 1 < len(words):
            override = words[i + 1]
            i += 2
            continue
        if w.startswith("--fleet="):
            override = w.split("=", 1)[1]
        i += 1
    try:
        from fleet.fleets_config import FleetsConfig
        from fleet.state import set_active_fleet
        cfg = FleetsConfig.load()
        entry = cfg.resolve(override)
        set_active_fleet(entry.name, entry.root, cfg.branch)
        return True
    except Exception:
        return False


def _task_names(words: Sequence[str]) -> list[str]:
    if not _activate_fleet_from_words(words):
        return []
    try:
        from fleet.state import tasks_root
        root = tasks_root()
        if not root.is_dir():
            return []
        return sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and not p.name.startswith("_")
        )
    except Exception:
        return []


def _repo_names(words: Sequence[str]) -> list[str]:
    if not _activate_fleet_from_words(words):
        return []
    try:
        from fleet.discovery import discover_repos
        repos = discover_repos()
        names: set[str] = set()
        for r in repos:
            if not r.enabled:
                continue
            names.add(r.name)
            if r.group_path:
                names.add(r.display_name)
        return sorted(names)
    except Exception:
        return []


def _task_name_after(words: Sequence[str], anchor: str) -> str | None:
    """Find the first non-flag token after ``anchor`` (e.g. 'add-repo')."""
    try:
        i = words.index(anchor)
    except ValueError:
        return None
    j = i + 1
    while j < len(words) - 1:  # last is the current partial
        w = words[j]
        if w.startswith("-"):
            # Skip the flag, and its value if it takes one.
            if "=" in w:
                j += 1
                continue
            if w in ("-F", "--fleet", "--repos", "--description",
                     "--description-file", "-d", "--add", "--remove"):
                j += 2
                continue
            j += 1
            continue
        return w
    return None


def _manifest_repo_names(words: Sequence[str], anchor: str) -> set[str]:
    """Repo names listed in the named task's manifest."""
    task = _task_name_after(words, anchor)
    if not task:
        return set()
    if not _activate_fleet_from_words(words):
        return set()
    try:
        from fleet.state import tasks_root
        from fleet.tasks.manifest import Manifest
        ws = tasks_root() / task
        if not ws.is_dir():
            return set()
        m = Manifest.try_load(ws)
        if m is None:
            return set()
        out: set[str] = set()
        for r in m.repos:
            out.add(r.name)
            if r.group:
                out.add(f"{r.group}/{r.name}")
        return out
    except Exception:
        return set()


def _repo_names_not_in_task(words: Sequence[str]) -> list[str]:
    members = _manifest_repo_names(words, "add-repo")
    return [r for r in _repo_names(words) if r not in members]


def _repo_names_in_task(words: Sequence[str]) -> list[str]:
    return sorted(_manifest_repo_names(words, "remove-repo"))


def _bundle_names(words: Sequence[str]) -> list[str]:
    if not _activate_fleet_from_words(words):
        return []
    try:
        from fleet.bundles_config import BundlesConfig
        return BundlesConfig.load().names()
    except Exception:
        return []


def _bundle_refs(words: Sequence[str]) -> list[str]:
    """Bundle names prefixed with ``@`` for inclusion in --repos."""
    return [f"@{n}" for n in _bundle_names(words)]


def _bundle_members(words: Sequence[str], anchor: str) -> list[str]:
    """Members of the bundle named after ``anchor`` in ``words``."""
    name = _task_name_after(words, anchor)
    if not name:
        return []
    if not _activate_fleet_from_words(words):
        return []
    try:
        from fleet.bundles_config import BundlesConfig
        return BundlesConfig.load().bundles.get(name, [])
    except Exception:
        return []


def _bundle_repos_not_in_bundle(words: Sequence[str]) -> list[str]:
    members = set(_bundle_members(words, "edit"))
    return [r for r in _repo_names(words) if r not in members]


def _bundle_repos_in_bundle(words: Sequence[str]) -> list[str]:
    return sorted(_bundle_members(words, "edit"))


ValueProvider = Callable[[Sequence[str]], list[str]]

# Positional value providers keyed by command-path tuple (subcommand chain).
# Lambdas (instead of direct refs) give us late binding so tests can
# monkey-patch the underlying provider functions on this module.
POS_PROVIDERS: dict[tuple[str, ...], ValueProvider] = {
    ("task", "info"): lambda w: _task_names(w),
    ("task", "sync"): lambda w: _task_names(w),
    ("task", "end"): lambda w: _task_names(w),
    ("task", "path"): lambda w: _task_names(w),
    ("task", "open"): lambda w: _task_names(w),
    ("task", "add-repo"): lambda w: _task_names(w),
    ("task", "remove-repo"): lambda w: _task_names(w),
    ("task", "rename"): lambda w: _task_names(w),
    ("task", "edit"): lambda w: _task_names(w),
    ("open",): lambda w: _task_names(w),
    ("fleets", "default"): lambda w: _fleet_names(w),
    ("fleets", "remove"): lambda w: _fleet_names(w),
    ("fleets", "rename"): lambda w: _fleet_names(w),
    ("bundles", "show"): lambda w: _bundle_names(w),
    ("bundles", "remove"): lambda w: _bundle_names(w),
    ("bundles", "edit"): lambda w: _bundle_names(w),
    ("bundles", "rename"): lambda w: _bundle_names(w),
}

# Option value providers keyed by option string. Applies on any subcommand
# that exposes the option (e.g. ``-F`` lives on every fleet-aware subparser).
OPT_PROVIDERS: dict[str, ValueProvider] = {
    "--fleet": lambda w: _fleet_names(w),
    "-F": lambda w: _fleet_names(w),
    "--repos": lambda w: _repo_names(w),
}

# Subcommand choices we never want to surface to users.
_HIDDEN_CHOICES = frozenset({"__complete"})


# ---------------------------------------------------------------------------
# Core completion
# ---------------------------------------------------------------------------

def _walk(
    parser: argparse.ArgumentParser,
    prev: Sequence[str],
) -> tuple[argparse.ArgumentParser, tuple[str, ...], int]:
    """Walk ``prev`` to find the deepest parser + how many positionals were
    consumed at that level."""
    cur = parser
    cmd_path: list[str] = []
    consumed_positional = 0
    skip_next = False
    for tok in prev:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("--") and "=" in tok:
            continue
        opt = _find_option(cur, tok)
        if opt is not None:
            if _option_takes_value(opt):
                skip_next = True
            continue
        sub = _subparsers_action(cur)
        if sub is not None and tok in sub.choices:
            cur = sub.choices[tok]
            cmd_path.append(tok)
            consumed_positional = 0
            continue
        # Bare positional value consumed on the current parser.
        consumed_positional += 1
    return cur, tuple(cmd_path), consumed_positional


def _filter_prefix(cands: Iterable[str], prefix: str) -> list[str]:
    return [c for c in cands if c.startswith(prefix)]


def _complete_option_value(
    action: argparse.Action, current: str, words: Sequence[str],
    cmd_path: tuple[str, ...] = (),
) -> tuple[int, list[str]]:
    if action.choices:
        cands = sorted(str(c) for c in action.choices)
        return DIRECTIVE_DEFAULT, _filter_prefix(cands, current)

    provider: ValueProvider | None = None

    # ``bundles edit --add/--remove`` are comma-separated repo-token lists
    # scoped to the named bundle. Gate on the resolved subcommand chain
    # (``cmd_path``) rather than a loose ``"edit" in words`` substring, so a
    # value that merely contains the word "edit" (or a future unrelated
    # subcommand reusing ``--add``) can't trigger bundle-scoped completion.
    if (
        cmd_path == ("bundles", "edit")
        and any(s in ("--add", "--remove") for s in action.option_strings)
    ):
        if "--add" in action.option_strings:
            provider = _bundle_repos_not_in_bundle
        else:
            provider = _bundle_repos_in_bundle
        head, sep, tail = current.rpartition(",")
        prefix = f"{head}{sep}" if sep else ""
        already = set(filter(None, head.split(","))) if sep else set()
        filtered = [r for r in provider(words)
                    if r not in already and r.startswith(tail)]
        return DIRECTIVE_NOSPACE, [f"{prefix}{r}" for r in filtered]

    for s in action.option_strings:
        if s in OPT_PROVIDERS:
            provider = OPT_PROVIDERS[s]
            break
    if provider is None:
        return DIRECTIVE_DEFAULT, []

    # --repos is comma-separated: complete the trailing segment, preserve the
    # head, request nospace so the user can keep adding commas. Subcommand
    # context picks the right candidate set: add-repo excludes current
    # members, remove-repo restricts to current members. ``@bundle`` refs
    # are always offered alongside raw repo names so users discover
    # bundles inline.
    if "--repos" in action.option_strings:
        if cmd_path == ("task", "add-repo"):
            provider = _repo_names_not_in_task
        elif cmd_path == ("task", "remove-repo"):
            provider = _repo_names_in_task
        head, sep, tail = current.rpartition(",")
        prefix = f"{head}{sep}" if sep else ""
        already = set(filter(None, head.split(","))) if sep else set()
        all_cands = list(provider(words)) + _bundle_refs(words)
        filtered = [r for r in all_cands
                    if r not in already and r.startswith(tail)]
        return DIRECTIVE_NOSPACE, [f"{prefix}{r}" for r in filtered]

    cands = sorted(set(provider(words)))
    return DIRECTIVE_DEFAULT, _filter_prefix(cands, current)


def complete(words: Sequence[str]) -> tuple[int, list[str]]:
    """Compute completion candidates for ``words``.

    ``words`` is the tokens typed after ``fleet`` including the current
    partial as the last element (empty string when the cursor is on a
    fresh word). Returns ``(directive, candidates)``.
    """
    from fleet.cli import build_parser

    if not words:
        words = [""]
    prev = list(words[:-1])
    current = words[-1]

    parser = build_parser()
    cur_parser, cmd_path, consumed_positional = _walk(parser, prev)

    # If the immediately preceding token is a value-taking option, complete
    # its value (`-F <TAB>`, `--repos <TAB>`).
    if prev:
        last = prev[-1]
        last_opt = _find_option(cur_parser, last)
        if last_opt is not None and _option_takes_value(last_opt):
            return _complete_option_value(last_opt, current, words, cmd_path)

    # `--opt=val<TAB>` form on the current token.
    if current.startswith("--") and "=" in current:
        name, _, val = current.partition("=")
        opt = _find_option(cur_parser, name)
        if opt is not None and _option_takes_value(opt):
            directive, cands = _complete_option_value(opt, val, words, cmd_path)
            return directive, [f"{name}={c}" for c in cands]

    # Completing an option (current starts with `-`).
    if current.startswith("-"):
        opts: list[str] = []
        for a in _option_actions(cur_parser):
            opts.extend(a.option_strings)
        return DIRECTIVE_DEFAULT, sorted(set(_filter_prefix(opts, current)))

    # Otherwise: subcommand choices + next positional's provider.
    out: list[str] = []
    sub = _subparsers_action(cur_parser)
    if sub is not None:
        out.extend(c for c in sub.choices if c not in _HIDDEN_CHOICES)

    positionals = _positional_actions(cur_parser)
    if consumed_positional < len(positionals):
        pos = positionals[consumed_positional]
        provider = POS_PROVIDERS.get(cmd_path)
        if provider is not None:
            out.extend(provider(words))
        elif pos.choices:
            out.extend(str(c) for c in pos.choices)

    return DIRECTIVE_DEFAULT, sorted(set(_filter_prefix(out, current)))


# ---------------------------------------------------------------------------
# Entry points used by cli.py
# ---------------------------------------------------------------------------

def _complete_within_timeout(
    words: Sequence[str],
) -> tuple[int, list[str]]:
    """Run :func:`complete` on a daemon thread with a hard timeout.

    A misconfigured environment, a slow network mount, or a huge repo tree
    must never hang the shell. If completion doesn't finish within
    :data:`_COMPLETION_TIMEOUT_SECONDS` we abandon the worker (it dies with
    the one-shot ``fleet __complete`` process) and return no candidates.
    """
    box: list[tuple[int, list[str]]] = [(DIRECTIVE_DEFAULT, [])]

    def _run() -> None:
        with contextlib.suppress(Exception):
            box[0] = complete(list(words))

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(_COMPLETION_TIMEOUT_SECONDS)
    return box[0]


def _sanitize(candidate: str) -> str:
    """Collapse anything that would corrupt the one-per-line wire protocol.

    A candidate containing a newline would be read by the shell as two
    separate completions; a stray carriage return mangles the line. Strip
    both (and surrounding whitespace) so each candidate stays a single line.
    """
    return candidate.replace("\r", "").replace("\n", " ").strip()


def render(words: Sequence[str]) -> None:
    """Print ``:directive`` + candidates to stdout. Never raises."""
    directive = DIRECTIVE_DEFAULT
    cands: list[str] = []
    with contextlib.suppress(Exception):
        directive, cands = _complete_within_timeout(list(words))
    out = sys.stdout
    out.write(f":{directive}\n")
    for c in cands:
        clean = _sanitize(c)
        if not clean:
            continue
        out.write(clean)
        out.write("\n")


def get_script(shell: str) -> str:
    """Return the shell completion script as a string."""
    from importlib.resources import files
    name = {
        "bash": "fleet.bash",
        "zsh": "fleet.zsh",
        "powershell": "fleet.ps1",
    }[shell]
    return (files("fleet.completions") / name).read_text(encoding="utf-8")

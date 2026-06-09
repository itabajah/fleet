"""Git operations: one implementation of every git command we need.

Replaces the three near-identical fetch/pull paths that used to live in
sync.ps1, task.py:prepare_canonical, and task.py:cmd_sync.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fleet.errors import FleetError

# Patterns from sync.ps1's $invokeWithRetry — used to decide whether a git
# invocation actually failed or just emitted noise (Windows filesystem
# warnings, reftable advisories, etc.). Word boundaries keep a fatal message
# that merely *mentions* "reftable"/"case-insensitive" from being downgraded
# to a warning, and stop substrings (e.g. "cannot" inside another word) from
# tripping the error gate.
_ERROR_RE = re.compile(
    r"\bfatal\b|error:|\bdenied\b|\bnot found\b|\bcould not\b|\bcannot\b|"
    r"\brejected\b|authentication failed",
    re.IGNORECASE,
)
_WARNING_RE = re.compile(r"\b(?:case-insensitive|reftable)\b|warning:", re.IGNORECASE)
_TRANSIENT_RE = re.compile(
    r"\b(?:503|502|504)\b|\btimeout\b|temporarily unavailable|connection refused|"
    r"failed to connect",
    re.IGNORECASE,
)

# Network git ops (fetch/pull/ls-remote/set-head) can hang indefinitely on a
# dead remote or stuck connection, so they get a bounded timeout with an env
# override (a big-but-healthy fetch shouldn't be killed, yet a truly hung
# connection mustn't block the whole run forever). Local ops are left
# UNBOUNDED on purpose: `git worktree add` / `checkout` legitimately take a
# long time when checking out a large working tree, and a fixed cap there
# would turn a slow-but-fine task creation into a spurious failure.
_DEFAULT_NETWORK_TIMEOUT = 120.0


def _network_timeout() -> float:
    """Network git timeout (seconds), overridable via ``FLEET_GIT_TIMEOUT``."""
    raw = os.environ.get("FLEET_GIT_TIMEOUT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _DEFAULT_NETWORK_TIMEOUT
        if value > 0:
            return value
    return _DEFAULT_NETWORK_TIMEOUT


@dataclass
class GitResult:
    """Outcome of a `run_git` call."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


def run_git(*args: str, cwd: Path | None = None, check: bool = True,
            capture: bool = True,
            timeout: float | None = None) -> GitResult:
    """Invoke git. Raises FleetError on non-zero exit when check=True.

    Raises FleetError (not Python's FileNotFoundError) when git isn't on PATH,
    so callers don't have to special-case the install-git case. ``timeout``
    defaults to ``None`` (unbounded) so local operations that are slow but
    healthy — a large ``worktree add`` / ``checkout`` — aren't killed; network
    wrappers (fetch/pull/ls-remote) pass an explicit timeout so a dead remote
    can't hang the whole run.
    """
    cmd = ["git", *args]
    # Pre-check cwd so a missing worktree directory doesn't get reported as
    # "git not on PATH" — subprocess.run raises FileNotFoundError for either.
    if cwd is not None and not Path(cwd).is_dir():
        raise FleetError(
            f"Working directory does not exist: {cwd}"
        )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise FleetError(
            "git not found on PATH. Install Git for Windows and re-open the "
            f"shell. (underlying error: {e})"
        ) from None
    except subprocess.TimeoutExpired:
        cwd_part = f" (in {cwd})" if cwd else ""
        timeout_part = f"{timeout:g}" if timeout is not None else "?"
        raise FleetError(
            f"git {' '.join(args)} timed out after {timeout_part}s{cwd_part}. "
            f"Check the network/remote, or raise the FLEET_GIT_TIMEOUT env var."
        ) from None

    result = GitResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if check and result.returncode != 0:
        cwd_part = f" (in {cwd})" if cwd else ""
        raise FleetError(
            f"git {' '.join(args)} failed{cwd_part}:\n  "
            f"{result.stderr.strip()}"
        )
    return result


def is_warning_only(result: GitResult) -> bool:
    """True if a non-zero git result looks like warning noise, not a real error.

    Some Windows git installs emit reftable / case-insensitive advisories and
    exit non-zero on otherwise-successful operations. We treat the operation
    as successful when the output matches a warning pattern AND none of the
    error patterns. Mirrors the gate in sync.ps1's $invokeWithRetry.
    """
    text = result.combined
    if _ERROR_RE.search(text):
        return False
    return bool(_WARNING_RE.search(text))


def is_transient_error(result: GitResult) -> bool:
    """True if the failure looks like a network blip worth retrying."""
    return bool(_TRANSIENT_RE.search(result.combined))


# ---------------------------------------------------------------------------
# Repository inspection
# ---------------------------------------------------------------------------

def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def is_dirty(repo_path: Path) -> bool:
    """True if the working tree has uncommitted changes.

    A vanished ``repo_path`` is treated as "not dirty" (it's the expected
    race during teardown). A genuine git failure — git missing from PATH, a
    timeout, a corrupted repo — is *not* swallowed here; it propagates as a
    FleetError so it can't masquerade as "clean". Aggregate callers that must
    survive one broken worktree (e.g. ``fleet task list``) guard the call.
    """
    if not repo_path.is_dir():
        return False
    r = run_git("status", "--porcelain", cwd=repo_path, check=False)
    if r.returncode != 0:
        return False
    return bool(r.stdout.strip())


def current_branch(repo_path: Path) -> str | None:
    """Return current branch name, or None on detached HEAD / failure."""
    r = run_git("branch", "--show-current", cwd=repo_path, check=False)
    if r.returncode != 0:
        return None
    name = r.stdout.strip()
    return name or None


def origin_url(repo_path: Path) -> str | None:
    """Return the origin remote URL, or None if no origin is configured.

    A vanished ``repo_path`` (or a repo with no ``origin``) yields None. A
    genuine git failure (missing binary, timeout) propagates rather than
    being silently reported as "no origin".
    """
    if not repo_path.is_dir():
        return None
    r = run_git("remote", "get-url", "origin", cwd=repo_path, check=False)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def origin_host(remote_url: str) -> str:
    """Extract the host portion (lowercased) from an origin URL.

    Handles https://host/..., git@host:..., and ssh://git@host/... formats.
    Falls back to the raw URL when nothing matches (so callers still get a
    deduplication key).
    """
    m = re.match(r"^[a-z][a-z0-9+.\-]*://(?:[^/@]+@)?([^/:]+)", remote_url)
    if m:
        return m.group(1).lower()
    m = re.match(r"^[^@]+@([^:]+):", remote_url)
    if m:
        return m.group(1).lower()
    return remote_url.lower()


def detect_default_branch(repo_path: Path, *, offline: bool = False,
                          allow_set_head: bool = False) -> str:
    """Return origin's default branch (e.g. 'main').

    Resolution order:
      1. ``git symbolic-ref refs/remotes/origin/HEAD`` (no network).
      2. Probe local refs for ``main`` then ``master`` (no network).
      3. If ``allow_set_head=True`` AND ``offline=False``, refresh
         ``origin/HEAD`` via ``git remote set-head origin --auto`` and
         retry step 1. Skipped by default because it both round-trips to
         the network and mutates a local ref.

    Raises :class:`FleetError` if nothing works. Pass ``offline=True`` from
    code paths that promised the user no network activity (e.g. ``--no-pull``).
    """
    r = run_git("symbolic-ref", "--quiet", "--short",
                "refs/remotes/origin/HEAD", cwd=repo_path, check=False)
    if r.returncode == 0:
        ref = r.stdout.strip()
        if ref.startswith("origin/"):
            return ref[len("origin/"):]

    for candidate in ("main", "master"):
        probe = run_git("show-ref", "--verify", "--quiet",
                        f"refs/remotes/origin/{candidate}",
                        cwd=repo_path, check=False)
        if probe.returncode == 0:
            return candidate

    if allow_set_head and not offline:
        refresh = run_git("remote", "set-head", "origin", "--auto",
                          cwd=repo_path, check=False,
                          timeout=_network_timeout())
        if refresh.returncode == 0:
            r = run_git("symbolic-ref", "--quiet", "--short",
                        "refs/remotes/origin/HEAD", cwd=repo_path, check=False)
            if r.returncode == 0:
                ref = r.stdout.strip()
                if ref.startswith("origin/"):
                    return ref[len("origin/"):]

    if offline:
        raise FleetError(
            f"Could not determine default branch for {repo_path} from local "
            "refs. Drop --no-pull (or run `git fetch && git remote set-head "
            "origin --auto` in that repo) and try again."
        )
    raise FleetError(
        f"Could not determine default branch for {repo_path}. "
        "Try `git remote set-head origin --auto` in that repo."
    )


def unpushed_count(worktree_path: Path, branch: str) -> int | None:
    """Commits on `branch` not yet on the local origin/<branch> ref.

    Returns None when no local origin/<branch> ref exists (branch never
    pushed, or never fetched after a push from elsewhere). Uses only local
    refs, so safe to call from offline status displays.
    """
    rp = run_git("rev-parse", "--verify", "--quiet",
                 f"refs/remotes/origin/{branch}",
                 cwd=worktree_path, check=False)
    if rp.returncode != 0 or not rp.stdout.strip():
        return None
    rev = run_git("rev-list", "--count", f"origin/{branch}..HEAD",
                  cwd=worktree_path, check=False)
    if rev.returncode != 0:
        return None
    try:
        return int(rev.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def fetch_prune(repo_path: Path, *, no_tags: bool = True) -> GitResult:
    """`git fetch --prune` (no-tags by default for sync speed)."""
    args = ["fetch", "--prune"]
    if no_tags:
        args.append("--no-tags")
    args.append("origin")
    return run_git(*args, cwd=repo_path, check=False,
                   timeout=_network_timeout())


def pull_ff_only(repo_path: Path, branch: str, *, no_tags: bool = True) -> GitResult:
    """`git pull --ff-only origin <branch>`."""
    args = ["pull", "--ff-only"]
    if no_tags:
        args.append("--no-tags")
    args.extend(["origin", branch])
    return run_git(*args, cwd=repo_path, check=False,
                   timeout=_network_timeout())


def fetch_branch_ff(repo_path: Path, branch: str) -> GitResult:
    """`git fetch origin <branch>:<branch>` — fast-forward a non-checked-out branch.

    Used to refresh the canonical's local ``<default>`` ref from origin when
    the canonical isn't currently on that branch (so a plain pull can't run).
    Network op, so it carries the network timeout.
    """
    return run_git("fetch", "origin", f"{branch}:{branch}",
                   cwd=repo_path, check=False, timeout=_network_timeout())


def checkout(repo_path: Path, branch: str) -> GitResult:
    return run_git("checkout", branch, cwd=repo_path, check=False)


def rename_branch(repo_path: Path, old: str, new: str) -> GitResult:
    """``git branch -m <old> <new>`` in ``repo_path``. Never raises.

    Used by ``fleet task rename`` to retarget the per-fleet task branch in
    each canonical repo. Caller inspects ``.ok``/``stderr`` to decide
    whether to roll back (a missing old branch usually means a prior
    rename completed past this canonical — see ``task rename`` recovery).
    """
    return run_git("branch", "-m", old, new, cwd=repo_path, check=False)


def worktree_repair(canonical: Path, worktree_path: Path) -> GitResult:
    """``git worktree repair <worktree_path>`` from the canonical.

    Required after manually moving a worktree directory: it updates the
    canonical's ``.git/worktrees/<leaf>/gitdir`` so ``git worktree
    remove`` / ``prune`` / ``list`` resolve the new location.
    """
    return run_git("worktree", "repair", str(worktree_path),
                   cwd=canonical, check=False)


def ls_remote_head(repo_path: Path) -> GitResult:
    """`git ls-remote --exit-code origin HEAD` — used as the auth probe."""
    return run_git("ls-remote", "--exit-code", "origin", "HEAD",
                   cwd=repo_path, check=False, timeout=_network_timeout())

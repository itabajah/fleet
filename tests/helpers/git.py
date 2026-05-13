"""Tiny real-git helpers for integration tests.

Every helper invokes the system ``git`` binary against an isolated
temp-dir bare remote — no network, no shared state, no user config
contamination (we set per-repo ``user.email`` / ``user.name``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git with text output captured, raising if it fails."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def init_bare_remote(path: Path, default_branch: str = "main") -> Path:
    """Initialise an empty bare remote at ``path`` and seed an initial commit.

    Bare repos can't have a working tree, so we initialise a temporary
    ``seed`` clone, commit a README, and push back. Returns ``path``.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", "--initial-branch", default_branch, str(path), cwd=path.parent)

    seed = path.parent / f"_seed_{path.name}"
    seed.mkdir()
    _git("init", "--initial-branch", default_branch, ".", cwd=seed)
    _git("config", "user.email", "fleet-test@example.invalid", cwd=seed)
    _git("config", "user.name", "fleet test", cwd=seed)
    _git("config", "commit.gpgsign", "false", cwd=seed)
    (seed / "README.md").write_text("# seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "initial", cwd=seed)
    _git("remote", "add", "origin", str(path), cwd=seed)
    _git("push", "-u", "origin", default_branch, cwd=seed)

    # Set HEAD on the bare so ls-remote and clones know the default.
    _git("symbolic-ref", "HEAD", f"refs/heads/{default_branch}", cwd=path)
    return path


def clone_into(remote: Path, dest: Path) -> Path:
    """Clone ``remote`` into ``dest``, configuring user identity. Returns dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", str(remote), str(dest), cwd=dest.parent)
    _git("config", "user.email", "fleet-test@example.invalid", cwd=dest)
    _git("config", "user.name", "fleet test", cwd=dest)
    _git("config", "commit.gpgsign", "false", cwd=dest)
    # Make origin/HEAD explicit so detect_default_branch's fast path works.
    _git("remote", "set-head", "origin", "--auto", cwd=dest)
    return dest


def commit_file(repo: Path, name: str, content: str = "x\n",
                message: str | None = None) -> None:
    """Write ``name`` with ``content``, then add+commit it."""
    (repo / name).write_text(content, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("commit", "-m", message or f"add {name}", cwd=repo)


def make_dirty(repo: Path, name: str = "DIRTY", content: str = "x\n") -> None:
    """Drop an unstaged file so ``git status --porcelain`` is non-empty."""
    (repo / name).write_text(content, encoding="utf-8")


def push_branch(repo: Path, branch: str) -> None:
    """Push ``branch`` to origin (creating the upstream if missing)."""
    _git("push", "-u", "origin", branch, cwd=repo)


def write_marker_repo(path: Path) -> Path:
    """Create a directory containing only a ``.git`` marker (file).

    Used by walker / discovery unit tests: avoids spawning git, but the
    walker still classifies the directory as a "repo".
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: ./fake\n", encoding="utf-8")
    return path

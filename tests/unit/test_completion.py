"""Unit tests for the completion engine.

Pure-Python. No subprocess, no real git. Dynamic providers are
monkey-patched where appropriate so tests don't depend on the disk
layout under `tasks_root()` / the configured fleets root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet import completion
from fleet.fleets_config import FleetsConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _complete(*words: str) -> tuple[int, list[str]]:
    """Run the engine with the given tokens (last token = current partial)."""
    return completion.complete(list(words))


def _candidates(*words: str) -> list[str]:
    return _complete(*words)[1]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

class TestTopLevel:
    def test_empty_lists_all_user_facing_commands(self) -> None:
        cands = _candidates("")
        assert "sync" in cands
        assert "scan" in cands
        assert "repos" in cands
        assert "task" in cands
        assert "fleets" in cands
        assert "open" in cands
        assert "completion" in cands

    def test_hidden_complete_subcommand_is_filtered(self) -> None:
        assert "__complete" not in _candidates("")

    def test_prefix_filters(self) -> None:
        assert set(_candidates("s")) == {"sync", "scan"}

    def test_dash_lists_top_level_options(self) -> None:
        cands = _candidates("-")
        assert "--help" in cands
        assert "--version" in cands


class TestTaskGroup:
    def test_task_subcommands(self) -> None:
        cands = _candidates("task", "")
        for name in ("new", "list", "info", "sync", "end", "path", "open"):
            assert name in cands

    def test_task_prefix(self) -> None:
        assert set(_candidates("task", "n")) == {"new"}


class TestFleetsGroup:
    def test_fleets_subcommands(self) -> None:
        assert set(_candidates("fleets", "")) == {
            "list", "add", "default", "remove", "rename",
        }


class TestFlags:
    def test_sync_flags(self) -> None:
        cands = _candidates("sync", "--")
        assert "--dry-run" in cands
        assert "--workers" in cands
        assert "--no-auth-check" in cands
        assert "--fleet" in cands  # from fleet_arg parent

    def test_task_new_requires_repos(self) -> None:
        cands = _candidates("task", "new", "--")
        assert "--repos" in cands
        assert "--description" in cands
        assert "--no-pull" in cands
        assert "--dry-run" in cands

    def test_task_list_flags(self) -> None:
        cands = _candidates("task", "list", "--")
        assert "--quick" in cands
        assert "--json" in cands

    def test_short_options(self) -> None:
        assert "-F" in _candidates("sync", "-")
        assert "-d" in _candidates("task", "new", "-")


# ---------------------------------------------------------------------------
# Dynamic value providers
# ---------------------------------------------------------------------------

class TestFleetNameCompletion:
    def test_lists_configured_fleet_names(self, tmp_path: Path) -> None:
        (tmp_path / "main").mkdir()
        (tmp_path / "work").mkdir()
        cfg = FleetsConfig.load()
        cfg.add("main", tmp_path / "main")
        cfg.add("work", tmp_path / "work")
        cfg.save()

        directive, cands = _complete("sync", "-F", "")
        assert directive == completion.DIRECTIVE_DEFAULT
        assert cands == ["main", "work"]

    def test_prefix_filters_fleet_names(self, tmp_path: Path) -> None:
        (tmp_path / "main").mkdir()
        (tmp_path / "work").mkdir()
        cfg = FleetsConfig.load()
        cfg.add("main", tmp_path / "main")
        cfg.add("work", tmp_path / "work")
        cfg.save()

        assert _candidates("scan", "--fleet", "w") == ["work"]

    def test_fleets_default_completes_fleet_names(self, tmp_path: Path) -> None:
        (tmp_path / "main").mkdir()
        cfg = FleetsConfig.load()
        cfg.add("main", tmp_path / "main")
        cfg.save()
        assert "main" in _candidates("fleets", "default", "")
        assert "main" in _candidates("fleets", "remove", "")

    def test_no_fleets_yields_empty_list(self) -> None:
        # No fleets configured, no exception.
        directive, cands = _complete("sync", "-F", "")
        assert directive == completion.DIRECTIVE_DEFAULT
        assert cands == []


class TestTaskNameCompletion:
    def _setup(self, tmp_path: Path) -> None:
        (tmp_path / "repos").mkdir()
        cfg = FleetsConfig.load()
        cfg.add("demo", tmp_path / "repos")
        cfg.save()

    def test_lists_task_dirs_under_active_fleet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup(tmp_path)
        tasks = tmp_path / "tasks-root" / "demo"
        tasks.mkdir(parents=True)
        (tasks / "feature-a").mkdir()
        (tasks / "bug-123").mkdir()
        (tasks / "_archive").mkdir()  # skip underscore-prefixed
        monkeypatch.setenv("FLEET_TASKS_ROOT", str(tmp_path / "tasks-root"))

        for cmd in ("info", "sync", "end", "path", "open"):
            assert set(_candidates("task", cmd, "")) == {"feature-a", "bug-123"}

    def test_top_level_open_completes_task_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup(tmp_path)
        tasks = tmp_path / "tasks-root" / "demo"
        tasks.mkdir(parents=True)
        (tasks / "alpha").mkdir()
        monkeypatch.setenv("FLEET_TASKS_ROOT", str(tmp_path / "tasks-root"))

        assert _candidates("open", "") == ["alpha"]

    def test_respects_F_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = FleetsConfig.load()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        cfg.add("a", tmp_path / "a")
        cfg.add("b", tmp_path / "b")
        cfg.set_default("a")
        cfg.save()
        tasks_a = tmp_path / "tr" / "a"
        tasks_b = tmp_path / "tr" / "b"
        tasks_a.mkdir(parents=True)
        tasks_b.mkdir(parents=True)
        (tasks_a / "in-a").mkdir()
        (tasks_b / "in-b").mkdir()
        monkeypatch.setenv("FLEET_TASKS_ROOT", str(tmp_path / "tr"))

        assert _candidates("task", "info", "") == ["in-a"]
        assert _candidates("task", "info", "-F", "b", "") == ["in-b"]

    def test_missing_tasks_root_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._setup(tmp_path)
        monkeypatch.setenv("FLEET_TASKS_ROOT", str(tmp_path / "nonexistent"))
        assert _candidates("task", "info", "") == []


class TestRepoNameCompletion:
    def test_lists_repos_for_active_fleet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Fake the dynamic provider so we don't need a real disk walk.
        monkeypatch.setattr(
            completion, "_repo_names", lambda _w: ["alpha", "beta", "gamma"],
        )

        directive, cands = _complete("task", "new", "foo", "--repos", "")
        assert directive == completion.DIRECTIVE_NOSPACE
        assert cands == ["alpha", "beta", "gamma"]

    def test_csv_completes_next_segment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names", lambda _w: ["alpha", "beta", "gamma"],
        )

        directive, cands = _complete(
            "task", "new", "foo", "--repos", "alpha,b",
        )
        assert directive == completion.DIRECTIVE_NOSPACE
        # Head preserved; only "beta" matches prefix; alpha excluded (already used).
        assert cands == ["alpha,beta"]

    def test_csv_excludes_already_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names", lambda _w: ["alpha", "beta", "gamma"],
        )

        _, cands = _complete("task", "new", "foo", "--repos", "alpha,beta,")
        assert cands == ["alpha,beta,gamma"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_misconfigured_environment_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Force the provider to blow up — render() must still produce a clean
        # ":0\n" with no stderr output.
        def boom(_w):
            raise RuntimeError("simulated breakage")

        monkeypatch.setattr(completion, "_task_names", boom)
        # complete() will hit boom() via _task_names; ensure NO exception
        # bubbles out of render() and stderr stays empty.
        completion.render(["task", "info", ""])
        out = capsys.readouterr()
        assert out.err == ""
        # First line is the directive; body may be empty.
        assert out.out.startswith(":0\n")

    def test_unknown_subcommand_returns_no_candidates(self) -> None:
        # We walk past unknown tokens; if we end up at the root with no
        # subcommand prefix, we still return top-level commands.
        _, cands = _complete("totally-unknown", "")
        # consumed as a positional value, so we get the top-level cmds again.
        assert "sync" in cands

    def test_no_words_lists_everything(self) -> None:
        cands = _candidates()
        assert "sync" in cands

    def test_render_emits_directive_line(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        completion.render(["sync", "--"])
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith(":")
        assert "--dry-run" in out

    def test_render_nospace_directive_for_repos(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names", lambda _w: ["alpha"],
        )
        completion.render(["task", "new", "foo", "--repos", ""])
        out = capsys.readouterr().out.splitlines()
        assert out[0] == f":{completion.DIRECTIVE_NOSPACE}"
        assert "alpha" in out


class TestEditSubcommandRepoFiltering:
    """add-repo / remove-repo filter --repos against the task's manifest."""

    def test_add_repo_excludes_current_members(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names",
            lambda _w: ["alpha", "beta", "gamma"],
        )
        monkeypatch.setattr(
            completion, "_manifest_repo_names",
            lambda _w, _anchor: {"alpha"},
        )
        directive, cands = _complete(
            "task", "add-repo", "t", "--repos", "",
        )
        assert directive == completion.DIRECTIVE_NOSPACE
        assert "alpha" not in cands
        assert "beta" in cands
        assert "gamma" in cands

    def test_remove_repo_offers_only_members(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names",
            lambda _w: ["alpha", "beta", "gamma"],
        )
        monkeypatch.setattr(
            completion, "_manifest_repo_names",
            lambda _w, _anchor: {"alpha", "beta"},
        )
        directive, cands = _complete(
            "task", "remove-repo", "t", "--repos", "",
        )
        assert directive == completion.DIRECTIVE_NOSPACE
        assert set(cands) == {"alpha", "beta"}

    def test_edit_subcommands_offer_task_names(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_task_names", lambda _w: ["t1", "t2"],
        )
        for sub in ("add-repo", "remove-repo", "rename", "edit"):
            assert _candidates("task", sub, "") == ["t1", "t2"], sub


class TestBundlesCompletion:
    """Bundle name positional + @bundle refs in --repos + bundles edit CSV."""

    def test_bundles_subcommands(self) -> None:
        cands = _candidates("bundles", "")
        for n in ("add", "list", "show", "remove", "edit"):
            assert n in cands

    def test_bundles_show_positional_lists_bundle_names(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_bundle_names", lambda _w: ["core", "frontend"],
        )
        for sub in ("show", "remove", "edit"):
            assert _candidates("bundles", sub, "") == ["core", "frontend"]

    def test_repos_includes_at_bundle_refs(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names", lambda _w: ["alpha", "beta"],
        )
        monkeypatch.setattr(
            completion, "_bundle_names", lambda _w: ["core"],
        )
        directive, cands = _complete("task", "new", "t", "--repos", "")
        assert directive == completion.DIRECTIVE_NOSPACE
        assert "alpha" in cands
        assert "@core" in cands

    def test_repos_prefix_at_filters_to_bundles(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names", lambda _w: ["alpha"],
        )
        monkeypatch.setattr(
            completion, "_bundle_names", lambda _w: ["core", "frontend"],
        )
        _, cands = _complete("task", "new", "t", "--repos", "@")
        assert set(cands) == {"@core", "@frontend"}

    def test_bundles_edit_add_excludes_current_members(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_repo_names",
            lambda _w: ["alpha", "beta", "gamma"],
        )
        monkeypatch.setattr(
            completion, "_bundle_members",
            lambda _w, _anchor: ["alpha"],
        )
        directive, cands = _complete(
            "bundles", "edit", "core", "--add", "",
        )
        assert directive == completion.DIRECTIVE_NOSPACE
        assert "alpha" not in cands
        assert "beta" in cands and "gamma" in cands

    def test_bundles_edit_remove_offers_only_members(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            completion, "_bundle_members",
            lambda _w, _anchor: ["alpha", "beta"],
        )
        directive, cands = _complete(
            "bundles", "edit", "core", "--remove", "",
        )
        assert directive == completion.DIRECTIVE_NOSPACE
        assert set(cands) == {"alpha", "beta"}


class TestRender:
    def test_sanitizes_newlines_in_candidates(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A candidate containing a newline would corrupt the one-per-line
        # wire protocol; render() must strip it.
        monkeypatch.setattr(
            completion, "complete",
            lambda _w: (completion.DIRECTIVE_DEFAULT, ["good", "ba\nd", ""]),
        )
        completion.render(["task"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f":{completion.DIRECTIVE_DEFAULT}"
        # The empty candidate is dropped; the newline one is collapsed.
        assert "good" in lines
        assert "ba d" in lines
        assert "" not in lines[1:]
        assert all("\n" not in line for line in lines)

    def test_render_never_raises_on_provider_error(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _boom(_w: object) -> tuple[int, list[str]]:
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(completion, "complete", _boom)
        # Must not raise, and must still emit a valid directive line.
        completion.render(["task"])
        out = capsys.readouterr().out
        assert out.startswith(f":{completion.DIRECTIVE_DEFAULT}")

    def test_render_times_out_slow_provider(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import time

        def _slow(_w: object) -> tuple[int, list[str]]:
            time.sleep(5)
            return completion.DIRECTIVE_DEFAULT, ["late"]

        monkeypatch.setattr(completion, "complete", _slow)
        monkeypatch.setattr(completion, "_COMPLETION_TIMEOUT_SECONDS", 0.1)
        start = time.monotonic()
        completion.render(["task"])
        elapsed = time.monotonic() - start
        # Should bail out near the timeout, not wait the full 5s.
        assert elapsed < 2.0
        out = capsys.readouterr().out
        assert out.startswith(f":{completion.DIRECTIVE_DEFAULT}")
        assert "late" not in out



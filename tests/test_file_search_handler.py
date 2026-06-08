"""Tests for :class:`app.core.tasks.handlers.file_search.FileSearchHandler`.

The handler is the first reference :class:`TaskHandler` shipped in
phase 1 of the brain-orchestration refactor. These tests exercise it
directly (no orchestrator needed) so we can verify:

* Argument parsing: empty / non-string queries are rejected.
* Search semantics: substring match works, ``case_sensitive`` flag
  flips behaviour, skip directories are pruned, ``max_results``
  truncates with a flag.
* Lifecycle: progress events fire periodically, completion carries
  the right result shape, missing roots emit ``TaskFailed``.
* Cancellation / resume / on_input entry points all behave (the
  former is a no-op, the latter two emit ``TaskFailed``).

End-to-end orchestrator integration lives in
``tests/test_task_orchestration_mixin.py`` (chunks 5-6) — these
tests focus on the pure handler contract.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.core.tasks.handlers.file_search import FileSearchHandler
from app.core.tasks.sandbox import FileTaskRoot
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskFailed,
    TaskOutcome,
    TaskProgress,
)


class _CollectingEmit:
    """Captures every emit so tests can assert the full sequence."""

    def __init__(self) -> None:
        self.outcomes: list[TaskOutcome] = []

    def __call__(self, outcome: TaskOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def terminal(self) -> TaskOutcome | None:
        for o in reversed(self.outcomes):
            if isinstance(o, (TaskCompleted, TaskFailed)):
                return o
        return None


def _make_tree(base: Path) -> None:
    """Create a small test tree under ``base``::

        base/
          Docs/
            a.md
            beta.txt
            gamma.md
            sub/
              deep_alpha.md
              .git/                  # skipped
                config
              node_modules/          # skipped
                pkg.json
        base/
          Notes/
            beta.md
            random.txt
    """
    docs = base / "Docs"
    notes = base / "Notes"
    docs.mkdir(parents=True)
    notes.mkdir(parents=True)
    (docs / "a.md").write_text("doc a")
    (docs / "beta.txt").write_text("doc beta")
    (docs / "gamma.md").write_text("doc gamma")
    sub = docs / "sub"
    sub.mkdir()
    (sub / "deep_alpha.md").write_text("doc deep alpha")
    skip_git = sub / ".git"
    skip_git.mkdir()
    (skip_git / "config").write_text("[core]")
    skip_node = sub / "node_modules"
    skip_node.mkdir()
    (skip_node / "pkg.json").write_text("{}")
    (notes / "beta.md").write_text("note beta")
    (notes / "random.txt").write_text("note random")


class _Fixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        _make_tree(self.base)
        self.docs_root = FileTaskRoot(
            label="Docs", path=str(self.base / "Docs")
        )
        self.notes_root = FileTaskRoot(
            label="Notes", path=str(self.base / "Notes")
        )

    def handler(self, **kwargs: Any) -> FileSearchHandler:
        kwargs.setdefault("roots", [self.docs_root, self.notes_root])
        return FileSearchHandler(**kwargs)

    def cleanup(self) -> None:
        self._tmp.cleanup()


class ArgsValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.handler = self.fx.handler()

    def test_empty_query_fails(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": ""}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        assert isinstance(emit.terminal, TaskFailed)
        self.assertIn("empty", emit.terminal.error.lower())

    def test_whitespace_only_query_fails(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "   "}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)

    def test_non_string_query_fails(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": 42}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        assert isinstance(emit.terminal, TaskFailed)
        self.assertIn("string", emit.terminal.error.lower())

    def test_missing_args_dict_fails_gracefully(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)

    def test_unknown_root_label_fails(self) -> None:
        emit = _CollectingEmit()
        self.handler.start(
            {"query": "a", "root_label": "Ghost"}, emit
        )
        self.assertIsInstance(emit.terminal, TaskFailed)
        assert isinstance(emit.terminal, TaskFailed)
        self.assertIn("Ghost", emit.terminal.error)


class SearchSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.handler = self.fx.handler()

    def test_substring_match_finds_across_roots(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "beta"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        assert isinstance(emit.terminal, TaskCompleted)
        names = sorted(
            (m["label"], m["relative_path"])
            for m in emit.terminal.result["matches"]
        )
        self.assertEqual(
            names,
            [("Docs", "beta.txt"), ("Notes", "beta.md")],
        )
        # The cue ``summary`` lists the matches so the passive cue path
        # tells Aiko what was found instead of a ``result keys=...`` fallback.
        summary = emit.terminal.result["summary"]
        self.assertIn("found 2 file(s)", summary)
        self.assertIn("beta", summary)

    def test_no_match_summary_says_none(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "zzz_no_such_file"}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertIn("no files matched", emit.terminal.result["summary"])

    def test_case_insensitive_by_default(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "BETA"}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["match_count"], 2)

    def test_case_sensitive_flag_respected(self) -> None:
        emit = _CollectingEmit()
        self.handler.start(
            {"query": "BETA", "case_sensitive": True}, emit
        )
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["match_count"], 0)

    def test_root_label_scopes_search(self) -> None:
        emit = _CollectingEmit()
        self.handler.start(
            {"query": "beta", "root_label": "Docs"}, emit
        )
        assert isinstance(emit.terminal, TaskCompleted)
        labels = {m["label"] for m in emit.terminal.result["matches"]}
        self.assertEqual(labels, {"Docs"})

    def test_skip_dirs_are_pruned(self) -> None:
        emit = _CollectingEmit()
        # ``.git`` and ``node_modules`` contain files that would
        # otherwise match a broad query; verify they're pruned.
        self.handler.start({"query": "pkg"}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        # node_modules/pkg.json must NOT appear.
        for m in emit.terminal.result["matches"]:
            self.assertNotIn("node_modules", m["relative_path"])

    def test_max_results_truncates_with_flag(self) -> None:
        emit = _CollectingEmit()
        self.handler.start(
            {"query": ".md", "max_results": 2}, emit
        )
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["match_count"], 2)
        self.assertTrue(emit.terminal.result["truncated"])

    def test_no_matches_completes_with_zero(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "unobtanium"}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["match_count"], 0)
        # Zero-hit completes "silently" (notify_aiko=False per the
        # doc — Aiko doesn't deserve a "found nothing" cue).
        self.assertEqual(emit.terminal.notify_aiko, False)

    def test_match_completion_sets_notify_aiko_true(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "beta"}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.notify_aiko, True)

    def test_result_includes_summary_counters(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": ".md"}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        r = emit.terminal.result
        self.assertGreaterEqual(r["files_scanned"], r["match_count"])
        self.assertGreaterEqual(r["dirs_scanned"], 2)
        self.assertIsInstance(r["elapsed_ms"], float)
        self.assertEqual(sorted(r["roots_searched"]), ["Docs", "Notes"])


class ProgressEmitsTests(unittest.TestCase):
    def test_progress_fires_periodically(self) -> None:
        fx = _Fixture()
        self.addCleanup(fx.cleanup)
        # Dial the progress beat down so the small fixture tree
        # produces at least one progress emit.
        handler = fx.handler(progress_every_n_dirs=1)
        emit = _CollectingEmit()
        handler.start({"query": "a"}, emit)
        progress = [o for o in emit.outcomes if isinstance(o, TaskProgress)]
        self.assertGreaterEqual(len(progress), 1)
        # Every progress message carries the running counters.
        for p in progress:
            self.assertIsNotNone(p.message)
            assert p.message is not None
            self.assertIn("dirs", p.message)


class NoRootsConfiguredTests(unittest.TestCase):
    def test_no_roots_emits_task_failed(self) -> None:
        handler = FileSearchHandler(roots=[])
        emit = _CollectingEmit()
        handler.start({"query": "anything"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        assert isinstance(emit.terminal, TaskFailed)
        self.assertIn("no active file roots", emit.terminal.error.lower())


class InactiveRootTests(unittest.TestCase):
    def test_inactive_root_silently_skipped(self) -> None:
        # Configure a missing root alongside a real one; the search
        # should silently skip the missing one and only return hits
        # from the real one.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_tree(base)
            handler = FileSearchHandler(
                roots=[
                    FileTaskRoot(label="Docs", path=str(base / "Docs")),
                    FileTaskRoot(
                        label="Ghost", path=str(base / "does-not-exist")
                    ),
                ]
            )
            emit = _CollectingEmit()
            handler.start({"query": "beta"}, emit)
            assert isinstance(emit.terminal, TaskCompleted)
            labels = {m["label"] for m in emit.terminal.result["matches"]}
            self.assertEqual(labels, {"Docs"})


class LifecycleEntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.handler = self.fx.handler()

    def test_resume_is_unsupported(self) -> None:
        emit = _CollectingEmit()
        self.handler.resume({"args": {"query": "a"}}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        assert isinstance(emit.terminal, TaskFailed)
        self.assertIn("resume", emit.terminal.error.lower())

    def test_on_input_is_unsupported(self) -> None:
        emit = _CollectingEmit()
        self.handler.on_input(
            {"args": {"query": "a"}}, "answer", emit
        )
        self.assertIsInstance(emit.terminal, TaskFailed)

    def test_cancel_is_a_noop(self) -> None:
        # Should not raise; nothing to verify beyond the no-raise.
        self.handler.cancel({"args": {"query": "a"}})


class _FakeKv:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self._d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self._d[key] = value


class OnlyNewTests(unittest.TestCase):
    """only_new filters matches to files new/modified since last scan."""

    def setUp(self) -> None:
        from app.core.tasks.file_snapshot import FileSnapshotStore

        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.store = FileSnapshotStore(_FakeKv())
        self.handler = self.fx.handler(snapshot_store=self.store)

    def test_first_run_baseline_returns_nothing(self) -> None:
        emit = _CollectingEmit()
        self.handler.start({"query": "md", "only_new": True}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        result = emit.terminal.result
        self.assertTrue(result["baseline_established"])
        self.assertTrue(result["only_new"])
        self.assertEqual(result["matches"], [])
        # Baseline run should not notify Aiko.
        self.assertFalse(emit.terminal.notify_aiko)

    def test_second_run_surfaces_new_file(self) -> None:
        # First scan establishes the baseline.
        self.handler.start({"query": "md", "only_new": True}, _CollectingEmit())
        # Add a new matching file.
        (self.fx.base / "Docs" / "fresh_note.md").write_text("brand new")
        emit = _CollectingEmit()
        self.handler.start({"query": "md", "only_new": True}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        result = emit.terminal.result
        self.assertFalse(result["baseline_established"])
        rels = {m["relative_path"] for m in result["matches"]}
        self.assertIn("fresh_note.md", rels)
        for m in result["matches"]:
            self.assertEqual(m.get("change"), "new")
        self.assertTrue(emit.terminal.notify_aiko)

    def test_unchanged_second_run_is_empty(self) -> None:
        self.handler.start({"query": "md", "only_new": True}, _CollectingEmit())
        emit = _CollectingEmit()
        self.handler.start({"query": "md", "only_new": True}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["matches"], [])

    def test_empty_query_allowed_with_only_new(self) -> None:
        # only_new makes an empty query mean "any new file".
        emit = _CollectingEmit()
        self.handler.start({"query": "", "only_new": True}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)

    def test_only_new_without_store_degrades_to_plain_search(self) -> None:
        # No snapshot store -> only_new flag is ignored, plain search.
        handler = self.fx.handler()  # no snapshot_store
        emit = _CollectingEmit()
        handler.start({"query": "md", "only_new": True}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        self.assertFalse(emit.terminal.result["only_new"])
        self.assertGreater(len(emit.terminal.result["matches"]), 0)

    def test_modified_file_surfaces(self) -> None:
        import os
        import time as _time

        self.handler.start({"query": "md", "only_new": True}, _CollectingEmit())
        target = self.fx.base / "Docs" / "a.md"
        # Bump mtime + size so the diff flags it modified.
        _time.sleep(0.01)
        target.write_text("doc a, now much longer content to change size")
        future = _time.time() + 100
        os.utime(target, (future, future))
        emit = _CollectingEmit()
        self.handler.start({"query": "a.md", "only_new": True}, emit)
        assert isinstance(emit.terminal, TaskCompleted)
        rels = {
            m["relative_path"]: m.get("change")
            for m in emit.terminal.result["matches"]
        }
        self.assertEqual(rels.get("a.md"), "modified")


if __name__ == "__main__":
    unittest.main()

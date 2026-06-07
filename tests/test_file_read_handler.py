"""Tests for :class:`app.core.tasks.handlers.file_read.FileReadHandler`.

Chunk 12 ships the second phase-1 reference handler — and the first
one that exercises the full ``running -> awaiting_input -> done``
lifecycle. These tests run the handler directly (no orchestrator
needed) so we can assert:

* Argument parsing: missing / empty / non-string ``path`` is rejected.
* Single-root happy path: the read returns ``TaskCompleted`` with
  the full result shape (label / relative_path / content / sizes /
  truncated / encoding / line_count).
* Multi-root path: a bare path matching in N roots emits
  ``TaskInputNeeded`` with N candidates as label-prefixed strings.
* ``on_input`` resolution: a valid candidate answer completes the
  task; an invalid one re-asks once, then fails on a second miss.
* Safety: extension allow-list rejects bad extensions; the
  binary-byte heuristic rejects non-text files; the byte cap
  truncates and flags ``truncated=True``; the line cap fires on
  catastrophically-long single-line input.
* Escape attempts: ``..`` and absolute paths outside the configured
  roots are rejected up-front (delegated to the sandbox; we just
  verify the handler doesn't bypass it).
* Lifecycle entry points: ``resume`` emits a graceful failure (no
  resume support in phase 1); ``cancel`` is a no-op.

The orchestrator-side wiring (handler registration, awaiting-input
events landing on the brain queue, cue parking, etc.) is covered
by :mod:`tests/test_task_orchestration_mixin.py`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.tasks.handlers.file_read import FileReadHandler
from app.core.tasks.sandbox import FileTaskRoot
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskFailed,
    TaskInputNeeded,
    TaskOutcome,
)


# ── helpers ──────────────────────────────────────────────────────────────


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

    @property
    def awaiting(self) -> TaskInputNeeded | None:
        for o in reversed(self.outcomes):
            if isinstance(o, TaskInputNeeded):
                return o
        return None


class _SingleRootFixture(unittest.TestCase):
    """Most tests only need one root with a small handful of files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root_path = Path(self._tmp.name) / "Docs"
        self.root_path.mkdir()
        (self.root_path / "hello.md").write_text(
            "# Hello\n\nworld\n", encoding="utf-8"
        )
        (self.root_path / "binary.bin").write_bytes(
            b"\x00\x01\x02PNG\x89\x00\x00ICCP"
        )
        (self.root_path / "weird.exe").write_text(
            "totally a text file, no really", encoding="utf-8"
        )
        sub = self.root_path / "sub"
        sub.mkdir()
        (sub / "deep.md").write_text("deep file", encoding="utf-8")
        self.root = FileTaskRoot(label="Docs", path=str(self.root_path))

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _handler(self, **overrides) -> FileReadHandler:
        defaults = {
            "roots": [self.root],
            "allowed_extensions": (".md", ".txt"),
        }
        defaults.update(overrides)
        return FileReadHandler(**defaults)


class _DualRootFixture(unittest.TestCase):
    """For the multi-root disambiguation case."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.docs = base / "Docs"
        self.notes = base / "Notes"
        self.docs.mkdir()
        self.notes.mkdir()
        # Same relative filename in both roots — the canonical
        # multi-root disambiguation case.
        (self.docs / "shared.md").write_text(
            "from docs root", encoding="utf-8"
        )
        (self.notes / "shared.md").write_text(
            "from notes root", encoding="utf-8"
        )
        # Unique-to-Notes file so the label-prefix happy path also
        # has something to read.
        (self.notes / "notes_only.md").write_text(
            "notes-only content", encoding="utf-8"
        )
        self.roots = [
            FileTaskRoot(label="Docs", path=str(self.docs)),
            FileTaskRoot(label="Notes", path=str(self.notes)),
        ]

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _handler(self) -> FileReadHandler:
        return FileReadHandler(
            roots=self.roots,
            allowed_extensions=(".md",),
        )


# ── 1. _parse_args ───────────────────────────────────────────────────────


class ParseArgsTests(_SingleRootFixture):
    def test_missing_path_rejected(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("path", emit.terminal.error.lower())

    def test_non_string_path_rejected(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": 42}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)

    def test_empty_path_rejected(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "   "}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)


# ── 2. Single-root happy path ────────────────────────────────────────────


class SingleRootReadTests(_SingleRootFixture):
    def test_happy_path_returns_full_result_shape(self) -> None:
        emit = _CollectingEmit()
        state = self._handler().start({"path": "Docs:hello.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        result = emit.terminal.result
        self.assertEqual(result["label"], "Docs")
        self.assertEqual(result["relative_path"], "hello.md")
        self.assertIn("Hello", result["content"])
        self.assertEqual(result["encoding"], "utf-8")
        self.assertFalse(result["truncated"])
        self.assertGreater(result["size_bytes"], 0)
        self.assertGreater(result["read_bytes"], 0)
        self.assertGreaterEqual(result["line_count"], 1)
        # The cue ``summary`` carries a readable content preview (not a
        # ``result keys=...`` fallback) so the passive cue path can tell
        # Aiko what the file said without a re-read.
        self.assertIn("summary", result)
        self.assertIn("Hello", result["summary"])
        # State should be terminal-shaped.
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["label"], "Docs")

    def test_bare_path_resolves_against_single_root(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "hello.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["label"], "Docs")

    def test_deep_path_resolves(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "Docs:sub/deep.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(
            emit.terminal.result["relative_path"], "sub/deep.md"
        )

    def test_no_match_emits_failed(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "Docs:does-not-exist.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("no_match", emit.terminal.error)

    def test_unknown_label_emits_failed(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "Bogus:any.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("unknown_label", emit.terminal.error)

    def test_no_active_roots_emits_failed(self) -> None:
        emit = _CollectingEmit()
        # Empty roots: handler can still construct but every read fails.
        FileReadHandler(roots=[]).start({"path": "hello.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("no active file roots", emit.terminal.error)


# ── 3. Multi-root awaiting_input ─────────────────────────────────────────


class MultiRootDisambiguationTests(_DualRootFixture):
    def test_bare_path_with_two_matches_emits_awaiting_input(self) -> None:
        emit = _CollectingEmit()
        state = self._handler().start({"path": "shared.md"}, emit)
        self.assertIsNone(emit.terminal)
        self.assertIsNotNone(emit.awaiting)
        self.assertEqual(state["phase"], "awaiting_disambiguation")
        # Both labels show up in the options.
        opts = emit.awaiting.options or []
        self.assertEqual(len(opts), 2)
        labels = {opt.split(":", 1)[0] for opt in opts}
        self.assertEqual(labels, {"Docs", "Notes"})
        # State carries the candidates verbatim.
        self.assertEqual(state["candidates"], opts)
        # The prompt mentions the path + count.
        self.assertIn("shared.md", emit.awaiting.prompt)
        self.assertIn("2", emit.awaiting.prompt)

    def test_label_prefixed_path_skips_disambiguation(self) -> None:
        emit = _CollectingEmit()
        state = self._handler().start({"path": "Docs:shared.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["label"], "Docs")
        self.assertIn("docs root", emit.terminal.result["content"])
        self.assertEqual(state["phase"], "done")

    def test_unique_to_notes_resolves_bare(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "notes_only.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["label"], "Notes")


class OnInputTests(_DualRootFixture):
    """Drive ``start`` -> ``on_input`` end-to-end."""

    def setUp(self) -> None:
        super().setUp()
        self.handler = self._handler()
        emit = _CollectingEmit()
        self.state = self.handler.start({"path": "shared.md"}, emit)
        self.assertEqual(self.state["phase"], "awaiting_disambiguation")

    def test_exact_candidate_answer_completes(self) -> None:
        emit = _CollectingEmit()
        out_state = self.handler.on_input(
            self.state, "Docs:shared.md", emit
        )
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["label"], "Docs")
        self.assertIn("docs root", emit.terminal.result["content"])
        self.assertEqual(out_state["phase"], "done")

    def test_label_only_answer_completes(self) -> None:
        # "Notes" alone uniquely identifies one candidate row.
        emit = _CollectingEmit()
        self.handler.on_input(self.state, "Notes", emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["label"], "Notes")
        self.assertIn("notes root", emit.terminal.result["content"])

    def test_case_insensitive_match(self) -> None:
        emit = _CollectingEmit()
        self.handler.on_input(self.state, "docs:shared.md", emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["label"], "Docs")

    def test_empty_answer_fails(self) -> None:
        emit = _CollectingEmit()
        self.handler.on_input(self.state, "", emit)
        self.assertIsInstance(emit.terminal, TaskFailed)

    def test_unrecognised_answer_re_asks_once(self) -> None:
        emit = _CollectingEmit()
        next_state = self.handler.on_input(self.state, "Mystery:x.md", emit)
        # Should be another awaiting-input, NOT a terminal failure yet.
        self.assertIsNone(emit.terminal)
        self.assertIsNotNone(emit.awaiting)
        self.assertEqual(next_state["phase"], "awaiting_disambiguation")
        self.assertEqual(next_state["retries"], 1)

    def test_unrecognised_answer_twice_fails(self) -> None:
        emit1 = _CollectingEmit()
        next_state = self.handler.on_input(self.state, "Mystery:x.md", emit1)
        emit2 = _CollectingEmit()
        out_state = self.handler.on_input(next_state, "Still:bad.md", emit2)
        self.assertIsInstance(emit2.terminal, TaskFailed)
        self.assertIn("could not match", emit2.terminal.error)
        self.assertEqual(out_state["phase"], "rejected")

    def test_state_without_candidates_fails(self) -> None:
        emit = _CollectingEmit()
        self.handler.on_input({"args": {}}, "anything", emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("no candidates", emit.terminal.error)


# ── 4. Safety caps ───────────────────────────────────────────────────────


class ExtensionAllowListTests(_SingleRootFixture):
    def test_disallowed_extension_rejected(self) -> None:
        emit = _CollectingEmit()
        # Non-existing path with bad extension would normally hit
        # ``no_match`` first; create the file so we exercise the
        # extension check itself.
        (self.root_path / "secret.dat").write_text("xxx", encoding="utf-8")
        self._handler().start({"path": "Docs:secret.dat"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("extension not allowed", emit.terminal.error)

    def test_empty_allow_list_accepts_everything(self) -> None:
        emit = _CollectingEmit()
        (self.root_path / "any.foo").write_text("hi", encoding="utf-8")
        handler = FileReadHandler(
            roots=[self.root], allowed_extensions=()
        )
        handler.start({"path": "Docs:any.foo"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)


class BinaryRejectionTests(_SingleRootFixture):
    def test_binary_file_rejected(self) -> None:
        emit = _CollectingEmit()
        # The .bin file has NULs in its body; even if we allowed the
        # extension, the binary heuristic should reject it.
        handler = FileReadHandler(
            roots=[self.root],
            allowed_extensions=(".bin", ".md", ".txt"),
        )
        handler.start({"path": "Docs:binary.bin"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("binary", emit.terminal.error.lower())


class ByteCapTests(_SingleRootFixture):
    def test_byte_cap_truncates_and_flags(self) -> None:
        big_path = self.root_path / "big.md"
        big_path.write_text("a" * 10000, encoding="utf-8")
        emit = _CollectingEmit()
        handler = FileReadHandler(
            roots=[self.root],
            max_bytes=1024,
            allowed_extensions=(".md",),
        )
        handler.start({"path": "Docs:big.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        result = emit.terminal.result
        self.assertTrue(result["truncated"])
        self.assertEqual(result["read_bytes"], 1024)
        self.assertEqual(result["size_bytes"], 10000)
        self.assertEqual(len(result["content"]), 1024)

    def test_args_max_bytes_clamps_to_handler_ceiling(self) -> None:
        # User-requested max_bytes can shrink but never exceed the cap.
        big_path = self.root_path / "big.md"
        big_path.write_text("b" * 4096, encoding="utf-8")
        emit = _CollectingEmit()
        handler = FileReadHandler(
            roots=[self.root],
            max_bytes=2048,
            allowed_extensions=(".md",),
        )
        handler.start(
            {"path": "Docs:big.md", "max_bytes": 100_000_000}, emit
        )
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertEqual(emit.terminal.result["read_bytes"], 2048)


class LineCapTests(_SingleRootFixture):
    def test_line_cap_truncates(self) -> None:
        many_lines = self.root_path / "lines.md"
        many_lines.write_text(
            "\n".join(f"line {i}" for i in range(50)), encoding="utf-8"
        )
        emit = _CollectingEmit()
        handler = FileReadHandler(
            roots=[self.root],
            max_lines=10,
            allowed_extensions=(".md",),
        )
        handler.start({"path": "Docs:lines.md"}, emit)
        self.assertIsInstance(emit.terminal, TaskCompleted)
        self.assertTrue(emit.terminal.result["truncated"])
        self.assertEqual(emit.terminal.result["line_count"], 10)


# ── 5. Escape rejection (sanity, delegated to sandbox) ─────────────────


class EscapeRejectionTests(_DualRootFixture):
    def test_dotdot_rejected(self) -> None:
        emit = _CollectingEmit()
        # ../ escape attempts get caught by ``resolve_path`` —
        # depending on the depth they may surface as ``escape`` or
        # ``no_match`` (they resolve outside the root). Either way,
        # we get a TaskFailed, never a TaskCompleted that exfiltrates
        # data from outside the sandbox.
        self._handler().start({"path": "Docs:../../etc/passwd"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)

    def test_absolute_path_rejected(self) -> None:
        emit = _CollectingEmit()
        self._handler().start({"path": "/etc/passwd"}, emit)
        self.assertIsInstance(emit.terminal, TaskFailed)


# ── 6. Lifecycle entry points ───────────────────────────────────────────


class ResumeAndCancelTests(_SingleRootFixture):
    def test_resume_emits_failed(self) -> None:
        emit = _CollectingEmit()
        self._handler().resume(
            {"args": {"path": "hello.md"}}, emit
        )
        self.assertIsInstance(emit.terminal, TaskFailed)
        self.assertIn("resume", emit.terminal.error)

    def test_cancel_returns_none(self) -> None:
        out = self._handler().cancel({"args": {}})
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()

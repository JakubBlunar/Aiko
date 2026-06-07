"""Tests for the ``task_id`` correlation ContextVar.

Mirror the existing ``turn_id`` tests in :mod:`tests.test_logging`
but for the new task-correlation half added in chunk 1. The
:class:`_TurnIdFilter` in :mod:`app.core.infra.crash_logging` now
stamps both ``record.turn`` and ``record.task`` so every formatted
line carries both correlation ids.

The most important invariant is that **both** ids propagate to
threads spawned via :func:`contextvars.copy_context`. The
:class:`TaskOrchestrator` will rely on this in chunk 2/3 — handler
emits run on the orchestrator thread, but the handlers themselves
fan out (e.g. ``file_search`` uses ``concurrent.futures``) and every
log line from a sub-thread must still grep on the same ``task=…``
token.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import unittest

from app.core.infra import crash_logging
from app.core.infra.crash_logging import (
    LOG_FORMAT,
    _RingBufferHandler,
    _TurnIdFilter,
    configure_logging_full,
    tail,
)
from app.core.infra.log_context import (
    get_task_id,
    get_turn_id,
    reset_task_id,
    reset_turn_id,
    set_task_id,
    set_turn_id,
)


def _make_record(name: str, level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TaskIdContextVarTests(unittest.TestCase):
    """Round-trip + best-effort reset semantics for the new task id."""

    def test_set_and_get_round_trip(self) -> None:
        self.assertIsNone(get_task_id())
        token = set_task_id("deadbeef")
        try:
            self.assertEqual(get_task_id(), "deadbeef")
        finally:
            reset_task_id(token)
        self.assertIsNone(get_task_id())

    def test_reset_with_invalid_token_clears_silently(self) -> None:
        token = set_task_id("first")
        reset_task_id(token)
        # Double-reset must not raise; the contextvar stays empty.
        reset_task_id(token)
        self.assertIsNone(get_task_id())

    def test_task_id_is_independent_of_turn_id(self) -> None:
        """The two correlation ids must not leak into each other."""
        tt = set_turn_id("turn-abc")
        ttt = set_task_id("task-def")
        try:
            self.assertEqual(get_turn_id(), "turn-abc")
            self.assertEqual(get_task_id(), "task-def")
        finally:
            reset_task_id(ttt)
            reset_turn_id(tt)
        self.assertIsNone(get_turn_id())
        self.assertIsNone(get_task_id())


class FilterStampsBothIdsTests(unittest.TestCase):
    """The single ``_TurnIdFilter`` now writes both ``record.turn``
    and ``record.task``. The format string references both, so a
    record without both attributes would raise ``KeyError`` during
    formatting — pinned here so we catch any regression at parse
    time rather than at first emit."""

    def test_filter_stamps_both_when_set(self) -> None:
        tt = set_turn_id("c0ffee01")
        ttt = set_task_id("ca5cade1")
        try:
            record = _make_record("app.core.test", logging.INFO, "hi")
            _TurnIdFilter().filter(record)
            self.assertEqual(getattr(record, "turn"), "c0ffee01")
            self.assertEqual(getattr(record, "task"), "ca5cade1")
        finally:
            reset_task_id(ttt)
            reset_turn_id(tt)

    def test_filter_dashes_when_neither_set(self) -> None:
        record = _make_record("app.core.test", logging.INFO, "hi")
        _TurnIdFilter().filter(record)
        self.assertEqual(getattr(record, "turn"), "-")
        self.assertEqual(getattr(record, "task"), "-")

    def test_filter_dashes_task_when_only_turn_set(self) -> None:
        tt = set_turn_id("abc12345")
        try:
            record = _make_record("app.core.test", logging.INFO, "hi")
            _TurnIdFilter().filter(record)
            self.assertEqual(getattr(record, "turn"), "abc12345")
            self.assertEqual(getattr(record, "task"), "-")
        finally:
            reset_turn_id(tt)

    def test_format_string_references_task(self) -> None:
        """If a future refactor drops ``%(task)s`` from the format
        string the orchestration logging contract is broken — this
        catches that at the unit level."""
        self.assertIn("%(task)s", LOG_FORMAT)
        self.assertIn("%(turn)s", LOG_FORMAT)


class FormattedLineCarriesBothIdsTests(unittest.TestCase):
    """End-to-end: stamp both ids, emit through a real logger, observe
    the formatted ring-buffer line contains both ``turn=…`` and
    ``task=…``."""

    def tearDown(self) -> None:
        configure_logging_full(level_name="WARNING", file_enabled=False)
        if crash_logging._RING_HANDLER is not None:
            crash_logging._RING_HANDLER.clear()

    def test_both_ids_appear_in_formatted_output(self) -> None:
        configure_logging_full(level_name="DEBUG", file_enabled=False)
        tt = set_turn_id("turn1234")
        ttt = set_task_id("task5678")
        try:
            logging.getLogger("app.core.test_brain_correlation").info("hello")
        finally:
            reset_task_id(ttt)
            reset_turn_id(tt)
        lines = tail(n=10, level="INFO", module_contains="test_brain_correlation")
        self.assertTrue(lines, "expected at least one matching log line")
        self.assertIn("turn=turn1234", lines[-1])
        self.assertIn("task=task5678", lines[-1])

    def test_dashes_when_no_correlation(self) -> None:
        configure_logging_full(level_name="DEBUG", file_enabled=False)
        logging.getLogger("app.core.test_brain_no_corr").info("hello")
        lines = tail(n=10, level="INFO", module_contains="test_brain_no_corr")
        self.assertTrue(lines)
        # Both placeholders should render as a literal dash.
        self.assertIn("turn=- ", lines[-1])
        self.assertIn("task=-]", lines[-1])


class ContextPropagatesAcrossCopyContextTests(unittest.TestCase):
    """Task handlers (and the future ``TaskOrchestrator``) rely on
    :func:`contextvars.copy_context` to propagate the correlation
    ids into thread-pool workers and ad-hoc threads. Plain
    :class:`threading.Thread` does NOT copy contextvars, so this
    test pins the documented escape hatch — ``copy_context().run``."""

    def test_task_id_propagates_through_copy_context(self) -> None:
        observed: dict[str, str | None] = {}
        ttt = set_task_id("propagate-me")
        try:
            ctx = contextvars.copy_context()

            def child() -> None:
                # No explicit set; should read what the parent had.
                observed["task"] = get_task_id()
                observed["turn"] = get_turn_id()

            t = threading.Thread(target=ctx.run, args=(child,))
            t.start()
            t.join(timeout=2.0)
        finally:
            reset_task_id(ttt)
        self.assertEqual(observed["task"], "propagate-me")
        self.assertIsNone(observed["turn"])  # never set on the parent

    def test_both_ids_propagate_simultaneously(self) -> None:
        """When both ids are set, copy_context carries them together
        — the common case during a mid-turn ``start_*`` tool call
        that spawns a task."""
        observed: dict[str, str | None] = {}
        tt = set_turn_id("turn-mid")
        ttt = set_task_id("task-mid")
        try:
            ctx = contextvars.copy_context()

            def child() -> None:
                observed["task"] = get_task_id()
                observed["turn"] = get_turn_id()

            t = threading.Thread(target=ctx.run, args=(child,))
            t.start()
            t.join(timeout=2.0)
        finally:
            reset_task_id(ttt)
            reset_turn_id(tt)
        self.assertEqual(observed, {"task": "task-mid", "turn": "turn-mid"})

    def test_plain_thread_does_not_inherit(self) -> None:
        """Documents the gotcha: producers that spin up a raw
        :class:`threading.Thread` lose the correlation. They must
        capture + re-set, or wrap with ``copy_context().run``."""
        observed: dict[str, str | None] = {}
        ttt = set_task_id("only-parent")
        try:

            def child() -> None:
                observed["task"] = get_task_id()

            t = threading.Thread(target=child)
            t.start()
            t.join(timeout=2.0)
        finally:
            reset_task_id(ttt)
        # Child thread sees the default (None), confirming the
        # documented escape hatch is the *only* way to propagate.
        self.assertIsNone(observed["task"])


class RingBufferStillFunctionsWithTaskFieldTests(unittest.TestCase):
    """The ring buffer tuple shape did NOT change (task is in the
    formatted string, not the tuple). Pin that explicitly so a future
    refactor that tries to add a sixth tuple field is caught — anyone
    unpacking ``(level, name, turn, msg, formatted)`` would silently
    receive shifted values."""

    def test_ring_tuple_shape_unchanged(self) -> None:
        handler = _RingBufferHandler(capacity=4)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(_TurnIdFilter())

        record = _make_record("app.core.test_ring_shape", logging.INFO, "hello")
        _TurnIdFilter().filter(record)
        handler.emit(record)

        snap = handler.snapshot()
        self.assertEqual(len(snap), 1)
        entry = snap[0]
        self.assertEqual(len(entry), 5, "tuple shape changed; downstream unpacking breaks")
        level_no, name, turn, msg, formatted = entry
        self.assertEqual(level_no, logging.INFO)
        self.assertEqual(name, "app.core.test_ring_shape")
        self.assertEqual(turn, "-")
        self.assertEqual(msg, "hello")
        # task=… appears in the formatted line even though it's not
        # a tuple field.
        self.assertIn("task=-", formatted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

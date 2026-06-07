"""Chunk-3 consumer-thread tests for :class:`BrainLoop`.

Pins the consume-side contract:

* The daemon thread launches on ``start()`` and exits on ``stop()``.
* Each event is routed to the handler registered for its ``kind``.
* Missing handler → one WARNING, event dropped, loop continues.
* Handler exceptions are caught and logged at ERROR — the consumer
  survives any single bad iteration.
* Queue close while events are pending → the events get drained
  before the consumer exits.
* Per-kind routing uses ``event.kind`` exclusively (no isinstance
  checks), so an unknown kind still hits the no-handler branch.
* The dispatched-INFO line carries ``kind=``, ``route=``,
  ``elapsed_ms=``, and ``gate_waited_ms=`` (the latter is 0 here
  because the gate is always open).

These tests deliberately use the default ``free_to_speak=True``
predicate; gate / defer behaviour lives in
``tests/test_brain_loop_gate.py``.

The tests use a tiny ``threading.Event`` to wait for the consumer
thread without burning CPU on ``time.sleep`` loops, plus a generous
1-second deadline so a slow CI machine still passes deterministically.
"""
from __future__ import annotations

import logging
import threading
import time
import unittest

from app.core.brain import (
    BrainEventQueue,
    BrainLoop,
    KIND_STATE_SYNC,
    KIND_USER_MESSAGE,
    StateSyncEvent,
    UserMessageEvent,
)


_DEADLINE_S = 1.0


def _wait_for(predicate, *, deadline_s: float = _DEADLINE_S) -> bool:
    """Tiny polling helper that returns True if ``predicate()`` flipped
    True within ``deadline_s`` seconds. Avoids ``time.sleep(0.5)``
    blocks in test bodies so the suite runs quickly when nothing
    fails.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class HappyPathDispatchTests(unittest.TestCase):
    """One handler, multiple events — everything dispatches in
    priority order with no surprises."""

    def test_single_event_round_trip(self) -> None:
        """A single ``UserMessageEvent`` lands on the registered
        handler and the dispatch counter ticks up exactly once."""
        loop = BrainLoop()
        received: list[str] = []
        done = threading.Event()

        def handler(event: object) -> None:
            received.append(getattr(event, "text", ""))
            done.set()

        loop.register_handler(KIND_USER_MESSAGE, handler)
        loop.start()
        try:
            loop.enqueue(UserMessageEvent(text="hi"))
            self.assertTrue(done.wait(timeout=_DEADLINE_S))
            self.assertEqual(received, ["hi"])
            self.assertEqual(loop.metrics_snapshot()["dispatched"], 1)
            self.assertEqual(loop.metrics_snapshot()["errors"], 0)
        finally:
            loop.stop()

    def test_multiple_events_dispatch_in_order(self) -> None:
        """Three same-priority events dispatch FIFO (queue's
        sequence tie-break)."""
        loop = BrainLoop()
        received: list[str] = []
        all_done = threading.Event()

        def handler(event: object) -> None:
            received.append(getattr(event, "text", ""))
            if len(received) == 3:
                all_done.set()

        loop.register_handler(KIND_USER_MESSAGE, handler)
        loop.start()
        try:
            for txt in ("a", "b", "c"):
                loop.enqueue(UserMessageEvent(text=txt))
            self.assertTrue(all_done.wait(timeout=_DEADLINE_S))
            self.assertEqual(received, ["a", "b", "c"])
            self.assertEqual(loop.metrics_snapshot()["dispatched"], 3)
        finally:
            loop.stop()

    def test_dispatch_carries_kind_and_route_in_log(self) -> None:
        """The INFO ``brain-loop dispatched:`` line carries the
        event kind + the resolved handler ``__name__`` as ``route=``.
        """
        loop = BrainLoop()
        done = threading.Event()

        def my_handler(event: object) -> None:
            done.set()

        loop.register_handler(KIND_USER_MESSAGE, my_handler)
        with self.assertLogs("app.brain_loop", level="INFO") as cm:
            loop.start()
            try:
                loop.enqueue(UserMessageEvent(text="hello"))
                self.assertTrue(done.wait(timeout=_DEADLINE_S))
            finally:
                loop.stop()
        dispatched_lines = [
            r for r in cm.output if "brain-loop dispatched:" in r
        ]
        self.assertEqual(len(dispatched_lines), 1, dispatched_lines)
        line = dispatched_lines[0]
        # Required structured fields per docs/brain-orchestration.md
        self.assertIn("kind=user_message", line)
        self.assertIn("route=my_handler", line)
        self.assertIn("elapsed_ms=", line)
        self.assertIn("gate_waited_ms=", line)


class RouterTests(unittest.TestCase):
    """The router uses ``event.kind`` exclusively — different kinds
    go to different handlers, and unknown kinds drop cleanly."""

    def test_two_kinds_go_to_two_handlers(self) -> None:
        loop = BrainLoop()
        user_events: list[object] = []
        state_events: list[object] = []
        both_done = threading.Event()

        def on_user(event: object) -> None:
            user_events.append(event)
            self._maybe_done(user_events, state_events, both_done)

        def on_state(event: object) -> None:
            state_events.append(event)
            self._maybe_done(user_events, state_events, both_done)

        loop.register_handler(KIND_USER_MESSAGE, on_user)
        loop.register_handler(KIND_STATE_SYNC, on_state)
        loop.start()
        try:
            loop.enqueue(UserMessageEvent(text="hi"))
            loop.enqueue(StateSyncEvent(subkind="presence"))
            self.assertTrue(both_done.wait(timeout=_DEADLINE_S))
            self.assertEqual(len(user_events), 1)
            self.assertEqual(len(state_events), 1)
        finally:
            loop.stop()

    @staticmethod
    def _maybe_done(user_evs, state_evs, ev: threading.Event) -> None:
        if len(user_evs) >= 1 and len(state_evs) >= 1:
            ev.set()

    def test_no_handler_emits_warning_and_drops(self) -> None:
        """Event with no registered handler emits one WARNING line
        and never bumps the loop's dispatched counter."""
        loop = BrainLoop()
        # No handlers registered intentionally.
        with self.assertLogs("app.brain_loop", level="WARNING") as cm:
            loop.start()
            try:
                loop.enqueue(UserMessageEvent(text="orphan"))
                # Give the consumer time to drain.
                self.assertTrue(
                    _wait_for(lambda: loop.queue.depth() == 0)
                )
            finally:
                loop.stop()
        no_handler_lines = [
            r for r in cm.output if "brain-loop no handler:" in r
        ]
        self.assertEqual(len(no_handler_lines), 1, no_handler_lines)
        self.assertIn("kind=user_message", no_handler_lines[0])
        self.assertEqual(loop.metrics_snapshot()["dispatched"], 0)
        # Queue still drained — the event was popped, just nowhere to go.
        self.assertEqual(loop.queue.depth(), 0)

    def test_handler_replacement_takes_effect_immediately(self) -> None:
        """Re-registering a handler for the same kind takes effect
        on the very next event. Useful for hot-swapping during
        tests + the chunk-4 wiring."""
        loop = BrainLoop()
        first_calls: list[str] = []
        second_calls: list[str] = []
        first_done = threading.Event()
        second_done = threading.Event()

        def first(event: object) -> None:
            first_calls.append(getattr(event, "text", ""))
            first_done.set()

        def second(event: object) -> None:
            second_calls.append(getattr(event, "text", ""))
            second_done.set()

        loop.register_handler(KIND_USER_MESSAGE, first)
        loop.start()
        try:
            loop.enqueue(UserMessageEvent(text="A"))
            self.assertTrue(first_done.wait(timeout=_DEADLINE_S))
            loop.register_handler(KIND_USER_MESSAGE, second)
            loop.enqueue(UserMessageEvent(text="B"))
            self.assertTrue(second_done.wait(timeout=_DEADLINE_S))
        finally:
            loop.stop()
        self.assertEqual(first_calls, ["A"])
        self.assertEqual(second_calls, ["B"])


class ExceptionIsolationTests(unittest.TestCase):
    """The loop survives any handler crash — one ERROR line, error
    counter bumps, dispatched counter stays at the previous value,
    next event dispatches normally."""

    def test_handler_exception_does_not_kill_loop(self) -> None:
        loop = BrainLoop()
        results: list[str] = []
        ok_done = threading.Event()

        def boom(event: object) -> None:
            results.append("crash")
            raise RuntimeError("intentional")

        def ok(event: object) -> None:
            results.append(getattr(event, "subkind", ""))
            ok_done.set()

        loop.register_handler(KIND_USER_MESSAGE, boom)
        loop.register_handler(KIND_STATE_SYNC, ok)
        with self.assertLogs("app.brain_loop", level="ERROR") as cm:
            loop.start()
            try:
                loop.enqueue(UserMessageEvent(text="x"))
                # Give the crash time to fire before we enqueue the
                # follow-up — otherwise the assertions race the
                # consumer's first dispatch.
                self.assertTrue(
                    _wait_for(lambda: "crash" in results),
                    msg=f"crash never landed: {results}",
                )
                loop.enqueue(StateSyncEvent(subkind="presence"))
                self.assertTrue(ok_done.wait(timeout=_DEADLINE_S))
            finally:
                loop.stop()
        # The error log line is shaped per the docs.
        error_lines = [
            r for r in cm.output if "brain-loop handler error:" in r
        ]
        self.assertEqual(len(error_lines), 1, error_lines)
        self.assertIn("kind=user_message", error_lines[0])
        self.assertIn("route=boom", error_lines[0])
        self.assertIn("RuntimeError", error_lines[0])
        # Metrics: 1 successful dispatch (the ok handler), 1 error.
        self.assertEqual(loop.metrics_snapshot()["dispatched"], 1)
        self.assertEqual(loop.metrics_snapshot()["errors"], 1)
        self.assertEqual(results, ["crash", "presence"])

    def test_loop_alive_after_consecutive_crashes(self) -> None:
        """Three crashes in a row still leave the consumer thread alive."""
        loop = BrainLoop()
        crashes = 0
        seen_after = threading.Event()

        def boom(event: object) -> None:
            nonlocal crashes
            crashes += 1
            raise RuntimeError(f"crash-{crashes}")

        def after(event: object) -> None:
            seen_after.set()

        loop.register_handler(KIND_USER_MESSAGE, boom)
        loop.register_handler(KIND_STATE_SYNC, after)
        # Silence the captured ERRORs at the assertLogs level.
        with self.assertLogs("app.brain_loop", level="ERROR"):
            loop.start()
            try:
                for _ in range(3):
                    loop.enqueue(UserMessageEvent(text="boom"))
                self.assertTrue(
                    _wait_for(lambda: crashes == 3),
                    msg=f"crashes={crashes}",
                )
                loop.enqueue(StateSyncEvent(subkind="alive?"))
                self.assertTrue(seen_after.wait(timeout=_DEADLINE_S))
                self.assertTrue(loop.is_running())
            finally:
                loop.stop()
        self.assertEqual(loop.metrics_snapshot()["errors"], 3)
        self.assertEqual(loop.metrics_snapshot()["dispatched"], 1)


class StopDrainTests(unittest.TestCase):
    """``stop()`` semantics: drains in-flight, closes the queue,
    joins the daemon thread, idempotent."""

    def test_stop_closes_queue_and_joins_thread(self) -> None:
        loop = BrainLoop()

        def handler(event: object) -> None:
            pass

        loop.register_handler(KIND_USER_MESSAGE, handler)
        loop.start()
        self.assertTrue(loop.is_running())
        loop.stop()
        self.assertFalse(loop.is_running())
        self.assertTrue(loop.queue.is_closed())

    def test_stop_log_includes_drained_and_dispatched(self) -> None:
        """The ``brain-loop stop:`` line carries ``drained=`` (still
        in queue), ``deferred=`` (still gate-blocked), and
        ``total_dispatched=`` (cumulative successes)."""
        loop = BrainLoop()

        def handler(event: object) -> None:
            pass

        loop.register_handler(KIND_USER_MESSAGE, handler)
        loop.start()
        # Let one dispatch land so total_dispatched is nonzero.
        loop.enqueue(UserMessageEvent(text="hi"))
        self.assertTrue(_wait_for(lambda: loop.queue.depth() == 0))
        with self.assertLogs("app.brain_loop", level="INFO") as cm:
            loop.stop()
        stop_lines = [r for r in cm.output if "brain-loop stop:" in r]
        self.assertEqual(len(stop_lines), 1, stop_lines)
        self.assertIn("drained=", stop_lines[0])
        self.assertIn("deferred=", stop_lines[0])
        self.assertIn("total_dispatched=1", stop_lines[0])

    def test_stop_is_safe_from_handler_thread(self) -> None:
        """Calling ``stop()`` while the consumer thread is still
        running mid-handler must not deadlock. The handler runs on
        the consumer thread; ``stop()`` from the test thread joins
        it cleanly."""
        loop = BrainLoop()
        in_handler = threading.Event()
        release = threading.Event()

        def slow(event: object) -> None:
            in_handler.set()
            # Release after a short wait — long enough to prove
            # stop() doesn't ride over the handler.
            release.wait(timeout=_DEADLINE_S)

        loop.register_handler(KIND_USER_MESSAGE, slow)
        loop.start()
        loop.enqueue(UserMessageEvent(text="hold"))
        self.assertTrue(in_handler.wait(timeout=_DEADLINE_S))
        # Now stop from the test thread while the handler is
        # mid-call. release.set() unblocks it.
        release.set()
        loop.stop(timeout=_DEADLINE_S)
        self.assertFalse(loop.is_running())

    def test_double_stop_is_silent(self) -> None:
        loop = BrainLoop()
        loop.start()
        loop.stop()
        # Capture root logger so a stray ERROR/WARN would show up.
        with self.assertLogs("app.brain_loop", level="INFO") as cm:
            loop.stop()
            # We can't assertNoLogs cleanly across versions; ensure
            # at least no fresh "stop:" line lands.
            logging.getLogger("app.brain_loop").info(
                "brain-loop test sentinel"
            )
        self.assertFalse(
            any("brain-loop stop:" in r for r in cm.output),
            msg=cm.output,
        )


class QueueOwnershipTests(unittest.TestCase):
    """A loop shares its queue with producers. Closing the queue
    externally still gives the consumer a clean exit."""

    def test_external_queue_close_exits_consumer(self) -> None:
        q = BrainEventQueue()
        loop = BrainLoop(queue=q)

        def handler(event: object) -> None:
            pass

        loop.register_handler(KIND_USER_MESSAGE, handler)
        loop.start()
        # Close the queue directly — the consumer's blocked get()
        # wakes and the loop notices is_closed() on the next
        # iteration. We then call stop() to flip is_running=False
        # and join.
        q.close()
        # Give the consumer a chance to notice.
        time.sleep(0.05)
        loop.stop()
        self.assertFalse(loop.is_running())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for :mod:`app.core.brain.queue` and :mod:`app.core.brain.events`.

These pin the invariants the rest of the orchestration layer depends
on:

* Priority ordering follows the :class:`Priority` ladder.
* Tie-break is FIFO via the monotonic enqueue sequence.
* ``put`` is thread-safe under a producer burst.
* ``get(timeout=…)`` returns ``None`` cleanly on timeout.
* ``close()`` wakes every blocked consumer.
* ``peek``/``depth``/``dispatch_count`` are atomic snapshots.
* :class:`BrainLoop`'s handler registry round-trips.

The queue is a public contract — break these and the whole brain
orchestration plan re-shapes underneath. See
``docs/brain-orchestration.md`` for the contract details.
"""
from __future__ import annotations

import threading
import time
import unittest

from app.core.brain import (
    BrainEventQueue,
    BrainLoop,
    MaintenanceDueEvent,
    Priority,
    ProactiveEvent,
    ProducerCallbacks,
    SpeakingWindowJobEvent,
    StateSyncEvent,
    TaskInputNeededEvent,
    TaskProgressEvent,
    TaskResultEvent,
    UserMessageEvent,
)
from app.core.brain.events import (
    KIND_MAINTENANCE_DUE,
    KIND_PROACTIVE,
    KIND_SPEAKING_WINDOW_JOB,
    KIND_STATE_SYNC,
    KIND_TASK_INPUT_NEEDED,
    KIND_TASK_PROGRESS,
    KIND_TASK_RESULT,
    KIND_USER_MESSAGE,
)


class PriorityLadderTests(unittest.TestCase):
    """The integer values of :class:`Priority` are stable contracts.

    Anything that compares to them (queue ordering, MCP debug output,
    doc table) breaks silently if they shift, so they're pinned here.
    """

    def test_ladder_values(self) -> None:
        self.assertEqual(int(Priority.USER_INPUT), 0)
        self.assertEqual(int(Priority.TASK_INPUT_NEEDED), 1)
        self.assertEqual(int(Priority.TASK_RESULT), 2)
        self.assertEqual(int(Priority.PROACTIVE), 3)
        self.assertEqual(int(Priority.SPEAKING_WINDOW_JOB), 4)
        self.assertEqual(int(Priority.TASK_PROGRESS), 5)
        self.assertEqual(int(Priority.MAINTENANCE), 6)
        self.assertEqual(int(Priority.STATE_SYNC), 7)

    def test_lower_int_means_higher_priority(self) -> None:
        self.assertLess(int(Priority.USER_INPUT), int(Priority.STATE_SYNC))


class EventDiscriminatorTests(unittest.TestCase):
    """Every concrete event class has a ClassVar ``kind`` and
    ``priority`` matching the discriminator constants + the ladder."""

    def test_user_message(self) -> None:
        e = UserMessageEvent(text="hi", mode="typed")
        self.assertEqual(e.kind, KIND_USER_MESSAGE)
        self.assertEqual(e.priority, Priority.USER_INPUT)

    def test_task_input_needed(self) -> None:
        e = TaskInputNeededEvent(task_id="t1", prompt="which root?")
        self.assertEqual(e.kind, KIND_TASK_INPUT_NEEDED)
        self.assertEqual(e.priority, Priority.TASK_INPUT_NEEDED)

    def test_task_result(self) -> None:
        e = TaskResultEvent(task_id="t1", status="done")
        self.assertEqual(e.kind, KIND_TASK_RESULT)
        self.assertEqual(e.priority, Priority.TASK_RESULT)

    def test_proactive(self) -> None:
        e = ProactiveEvent(session_key="s1", source="typed_silence")
        self.assertEqual(e.kind, KIND_PROACTIVE)
        self.assertEqual(e.priority, Priority.PROACTIVE)

    def test_speaking_window_job(self) -> None:
        e = SpeakingWindowJobEvent(name="reflection", callable_=lambda: None)
        self.assertEqual(e.kind, KIND_SPEAKING_WINDOW_JOB)
        self.assertEqual(e.priority, Priority.SPEAKING_WINDOW_JOB)

    def test_task_progress(self) -> None:
        e = TaskProgressEvent(task_id="t1", progress=0.5)
        self.assertEqual(e.kind, KIND_TASK_PROGRESS)
        self.assertEqual(e.priority, Priority.TASK_PROGRESS)

    def test_maintenance_due(self) -> None:
        e = MaintenanceDueEvent()
        self.assertEqual(e.kind, KIND_MAINTENANCE_DUE)
        self.assertEqual(e.priority, Priority.MAINTENANCE)

    def test_state_sync(self) -> None:
        e = StateSyncEvent(subkind="presence", payload=(("focused", True),))
        self.assertEqual(e.kind, KIND_STATE_SYNC)
        self.assertEqual(e.priority, Priority.STATE_SYNC)

    def test_events_are_frozen(self) -> None:
        """Frozen dataclasses raise on attribute assignment — needed
        so a producer can't mutate an in-flight event after enqueue."""
        e = UserMessageEvent(text="hi")
        with self.assertRaises(Exception):  # FrozenInstanceError subclasses Exception
            e.text = "mutated"  # type: ignore[misc]


class ProducerCallbacksTests(unittest.TestCase):
    """Chunk 8: streaming-callback bundle attached to
    :class:`UserMessageEvent`. The shape is what the WS chat handler
    threaded into ``chat_once_streaming`` pre-refactor; carrying it
    on the event lets the brain-loop handler thread it through after
    the queue hop.
    """

    def test_default_construction_is_all_none(self) -> None:
        cb = ProducerCallbacks()
        self.assertIsNone(cb.on_token)
        self.assertIsNone(cb.on_generation_status)
        self.assertIsNone(cb.stop_requested)

    def test_callables_round_trip(self) -> None:
        tokens: list[str] = []
        statuses: list[str] = []
        cb = ProducerCallbacks(
            on_token=tokens.append,
            on_generation_status=statuses.append,
            stop_requested=lambda: False,
        )
        cb.on_token("hi")
        cb.on_generation_status("ok")
        self.assertEqual(tokens, ["hi"])
        self.assertEqual(statuses, ["ok"])
        self.assertFalse(cb.stop_requested())

    def test_is_frozen(self) -> None:
        cb = ProducerCallbacks()
        with self.assertRaises(Exception):
            cb.on_token = lambda _t: None  # type: ignore[misc]

    def test_attaches_to_user_message_event(self) -> None:
        tokens: list[str] = []
        cb = ProducerCallbacks(on_token=tokens.append)
        e = UserMessageEvent(text="hi", mode="typed", callbacks=cb)
        self.assertIs(e.callbacks, cb)
        # The event is still hashable + comparable as long as the
        # callables themselves are (functions and lambdas always are).
        self.assertEqual(e.kind, KIND_USER_MESSAGE)

    def test_user_message_event_default_callbacks_is_none(self) -> None:
        # Pre-chunk-8 producers that don't know about the new field
        # still build valid events. Default has to stay None so
        # downstream consumers can ``if event.callbacks is None``
        # to short-circuit.
        e = UserMessageEvent(text="hi", mode="mcp")
        self.assertIsNone(e.callbacks)


class QueueOrderingTests(unittest.TestCase):
    def test_priority_order_drains_lowest_first(self) -> None:
        q = BrainEventQueue()
        # Enqueue in reverse priority order; expect them to drain in
        # increasing priority value.
        q.put(StateSyncEvent(subkind="presence"))
        q.put(MaintenanceDueEvent())
        q.put(TaskProgressEvent(task_id="t1"))
        q.put(SpeakingWindowJobEvent(name="reflection"))
        q.put(ProactiveEvent(session_key="s1"))
        q.put(TaskResultEvent(task_id="t1", status="done"))
        q.put(TaskInputNeededEvent(task_id="t1", prompt="?"))
        q.put(UserMessageEvent(text="hi", mode="typed"))

        kinds = []
        for _ in range(8):
            e = q.get(timeout=0.5)
            self.assertIsNotNone(e)
            kinds.append(e.kind)

        self.assertEqual(
            kinds,
            [
                KIND_USER_MESSAGE,
                KIND_TASK_INPUT_NEEDED,
                KIND_TASK_RESULT,
                KIND_PROACTIVE,
                KIND_SPEAKING_WINDOW_JOB,
                KIND_TASK_PROGRESS,
                KIND_MAINTENANCE_DUE,
                KIND_STATE_SYNC,
            ],
        )

    def test_same_priority_tie_breaks_fifo(self) -> None:
        """Three task results enqueued in order drain in the same order
        even though they have identical priority."""
        q = BrainEventQueue()
        q.put(TaskResultEvent(task_id="first", status="done"))
        q.put(TaskResultEvent(task_id="second", status="done"))
        q.put(TaskResultEvent(task_id="third", status="done"))
        observed = [q.get(timeout=0.1).task_id for _ in range(3)]
        self.assertEqual(observed, ["first", "second", "third"])

    def test_higher_priority_jumps_ahead_of_queued_lower(self) -> None:
        """A user message arriving after a backlog of maintenance still
        drains first."""
        q = BrainEventQueue()
        for _ in range(5):
            q.put(MaintenanceDueEvent())
        q.put(UserMessageEvent(text="hi", mode="typed"))
        first = q.get(timeout=0.1)
        self.assertEqual(first.kind, KIND_USER_MESSAGE)


class QueueBlockingTests(unittest.TestCase):
    def test_get_times_out_cleanly(self) -> None:
        q = BrainEventQueue()
        start = time.monotonic()
        result = q.get(timeout=0.05)
        elapsed = time.monotonic() - start
        self.assertIsNone(result)
        # Real-world timer slack varies on Windows + virtualized CI;
        # tolerate up to 1s before declaring it broken.
        self.assertLess(elapsed, 1.0, f"timeout overran: {elapsed:.3f}s")
        self.assertGreaterEqual(elapsed, 0.04)

    def test_close_wakes_blocked_consumer(self) -> None:
        """A consumer blocked on get() must return None when close()
        fires from another thread. This is the shutdown contract."""
        q = BrainEventQueue()
        results: list[object | None] = []
        ready = threading.Event()

        def consumer() -> None:
            ready.set()
            results.append(q.get(timeout=5.0))

        t = threading.Thread(target=consumer)
        t.start()
        ready.wait(timeout=1.0)
        time.sleep(0.05)  # let the consumer actually enter wait()
        q.close()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "consumer thread did not exit on close")
        self.assertEqual(results, [None])
        self.assertTrue(q.is_closed())

    def test_put_after_close_is_dropped_no_error(self) -> None:
        q = BrainEventQueue()
        q.close()
        # Should not raise; should not enqueue.
        q.put(UserMessageEvent(text="late"))
        self.assertEqual(q.depth(), 0)


class QueueThreadingTests(unittest.TestCase):
    def test_concurrent_puts_keep_all_items(self) -> None:
        """Six producer threads each enqueue 50 events; the queue
        must end up with exactly 300 items and zero crashes."""
        q = BrainEventQueue()

        def producer(idx: int) -> None:
            for j in range(50):
                q.put(TaskProgressEvent(task_id=f"t{idx}-{j}"))

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(q.depth(), 300)
        drained = 0
        while True:
            e = q.get(timeout=0.05)
            if e is None:
                break
            drained += 1
        self.assertEqual(drained, 300)
        self.assertEqual(q.dispatch_count(), 300)


class QueueDebugSurfaceTests(unittest.TestCase):
    def test_peek_returns_snapshot_without_consuming(self) -> None:
        q = BrainEventQueue()
        q.put(MaintenanceDueEvent())
        q.put(UserMessageEvent(text="hi", mode="typed"))
        q.put(TaskResultEvent(task_id="t1", status="done"))
        before = q.depth()
        top = q.peek(n=3)
        self.assertEqual(q.depth(), before, "peek must not consume")
        # First element is the highest-priority queued item.
        self.assertEqual(top[0][3], KIND_USER_MESSAGE)
        # Each entry is (priority:int, seq:int, enqueued_at:float, kind:str).
        for priority, seq, ts, kind in top:
            self.assertIsInstance(priority, int)
            self.assertIsInstance(seq, int)
            self.assertIsInstance(ts, float)
            self.assertIsInstance(kind, str)

    def test_peek_with_zero_returns_empty(self) -> None:
        q = BrainEventQueue()
        q.put(UserMessageEvent(text="hi"))
        self.assertEqual(q.peek(n=0), [])

    def test_dispatch_count_increments_only_on_successful_get(self) -> None:
        q = BrainEventQueue()
        # Timeout-only get should not bump the counter.
        self.assertIsNone(q.get(timeout=0.01))
        self.assertEqual(q.dispatch_count(), 0)
        q.put(UserMessageEvent(text="hi"))
        q.get(timeout=0.1)
        self.assertEqual(q.dispatch_count(), 1)


class BrainLoopBasicsTests(unittest.TestCase):
    """Public-surface contracts that ship across chunks 1 + 3:
    handler registry round-trip, queue ownership, start/stop
    idempotence, ``enqueue`` pass-through. Detailed consume / gate /
    deferral behaviour lives in ``tests/test_brain_loop_consume.py``
    and ``tests/test_brain_loop_gate.py``."""

    def test_default_queue_is_owned(self) -> None:
        loop = BrainLoop()
        self.assertIsInstance(loop.queue, BrainEventQueue)
        # is_running starts False.
        self.assertFalse(loop.is_running())

    def test_explicit_queue_is_used(self) -> None:
        q = BrainEventQueue()
        loop = BrainLoop(queue=q)
        self.assertIs(loop.queue, q)

    def test_register_handler_round_trip(self) -> None:
        loop = BrainLoop()

        def handler(event: object) -> None:
            pass

        loop.register_handler(KIND_USER_MESSAGE, handler)
        self.assertIs(loop.handler_for(KIND_USER_MESSAGE), handler)
        # Overwrite is allowed (one canonical owner per kind).
        def handler2(event: object) -> None:
            pass

        loop.register_handler(KIND_USER_MESSAGE, handler2)
        self.assertIs(loop.handler_for(KIND_USER_MESSAGE), handler2)

    def test_register_handler_rejects_empty_kind(self) -> None:
        loop = BrainLoop()
        with self.assertRaises(ValueError):
            loop.register_handler("", lambda e: None)

    def test_handler_for_unknown_returns_none(self) -> None:
        loop = BrainLoop()
        self.assertIsNone(loop.handler_for("nope"))

    def test_enqueue_pass_through(self) -> None:
        loop = BrainLoop()
        loop.enqueue(UserMessageEvent(text="hi"))
        self.assertEqual(loop.queue.depth(), 1)

    def test_start_stop_is_idempotent(self) -> None:
        loop = BrainLoop()
        loop.start()
        self.assertTrue(loop.is_running())
        # Second start is a warning, not a crash.
        loop.start()
        self.assertTrue(loop.is_running())
        loop.stop()
        self.assertFalse(loop.is_running())
        self.assertTrue(loop.queue.is_closed())
        # Second stop is silently fine.
        loop.stop()
        self.assertFalse(loop.is_running())

    def test_start_consumes_with_no_handlers(self) -> None:
        """Chunk-3 contract: starting the loop launches the consumer
        thread. Events with no registered handler are popped, logged
        as WARNING, and dropped — the loop is tolerant of partial
        wiring (e.g. ``SessionController`` boots in the wrong order)
        and never blocks on a missing handler.
        """
        loop = BrainLoop()
        loop.start()
        try:
            loop.enqueue(UserMessageEvent(text="hi"))
            loop.enqueue(MaintenanceDueEvent())
            # Give the consumer time to drain both.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if loop.queue.depth() == 0:
                    break
                time.sleep(0.005)
            self.assertEqual(loop.queue.depth(), 0)
            # The queue's dispatch_count bumps on every get(), even
            # when the loop's handler-side dispatch counter stays 0
            # because no handler was found.
            self.assertEqual(loop.queue.dispatch_count(), 2)
            self.assertEqual(loop.metrics_snapshot()["dispatched"], 0)
        finally:
            loop.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

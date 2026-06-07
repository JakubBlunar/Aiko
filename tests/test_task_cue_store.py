"""Unit tests for :class:`TaskCueStore` — chunk 4.

Pins the cue parking + stale-sweep + aggregation cap contract:

* Park appends a cue with monotonic + wall timestamps.
* Park with an existing ``task_id`` replaces (latest state wins).
* ``drain_for_render`` returns FIFO up to ``max_aggregated``, with
  excess staying parked.
* ``drain_for_render`` silently drops cues older than
  ``max_age_seconds`` (one INFO line each).
* ``peek_for_escalation`` reads without clearing, but still applies
  the stale-sweep side-effect.
* ``clear(task_id)`` removes a single cue; ``clear_all`` wipes all.
* Concurrent producers + drains hold the internal invariants.
"""
from __future__ import annotations

import threading
import time
import unittest

from app.core.tasks.task_cue_store import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    TaskCue,
    TaskCueStore,
)


# ── construction ────────────────────────────────────────────────────


class ConstructionTests(unittest.TestCase):
    def test_construct_with_defaults(self) -> None:
        s = TaskCueStore()
        m = s.metrics_snapshot()
        self.assertEqual(m["pending"], 0)
        self.assertEqual(m["park_count"], 0)
        # Defaults pulled from the docstring contract.
        self.assertEqual(m["max_age_seconds"], 1800.0)
        self.assertEqual(m["max_aggregated"], 5)

    def test_rejects_non_positive_max_age(self) -> None:
        with self.assertRaises(ValueError):
            TaskCueStore(max_age_seconds=0)
        with self.assertRaises(ValueError):
            TaskCueStore(max_age_seconds=-1)

    def test_rejects_non_positive_max_aggregated(self) -> None:
        with self.assertRaises(ValueError):
            TaskCueStore(max_aggregated=0)


# ── park ────────────────────────────────────────────────────────────


class ParkTests(unittest.TestCase):
    def test_park_returns_cue_with_timestamps(self) -> None:
        s = TaskCueStore()
        cue = s.park(
            task_id="t1",
            session_key="u1",
            kind=CUE_KIND_RESULT,
            title="search",
            status="done",
            summary="found 3 docs",
        )
        self.assertIsInstance(cue, TaskCue)
        self.assertEqual(cue.task_id, "t1")
        self.assertEqual(cue.kind, CUE_KIND_RESULT)
        self.assertEqual(cue.title, "search")
        self.assertEqual(cue.summary, "found 3 docs")
        self.assertEqual(cue.status, "done")
        self.assertGreater(cue.parked_at, 0.0)
        self.assertGreater(cue.parked_at_wall, 0.0)
        self.assertEqual(s.pending_count(), 1)
        self.assertEqual(s.metrics_snapshot()["park_count"], 1)

    def test_park_with_options_keeps_tuple(self) -> None:
        s = TaskCueStore()
        cue = s.park(
            task_id="t1",
            session_key="u1",
            kind=CUE_KIND_INPUT_NEEDED,
            title="search",
            summary="which one?",
            options=("a", "b", "c"),
        )
        self.assertEqual(cue.options, ("a", "b", "c"))
        # And ``None`` is the contract for "no options".
        cue2 = s.park(
            task_id="t2", session_key="u1",
            kind=CUE_KIND_INPUT_NEEDED, summary="anything?",
        )
        self.assertIsNone(cue2.options)

    def test_park_replaces_existing_cue_for_same_task(self) -> None:
        """The latest park for a given task id wins; a result cue
        should clobber an earlier input-needed cue for the same task."""
        s = TaskCueStore()
        s.park(
            task_id="t1", session_key="u1",
            kind=CUE_KIND_INPUT_NEEDED, summary="which?",
        )
        self.assertEqual(s.pending_count(), 1)
        s.park(
            task_id="t1", session_key="u1",
            kind=CUE_KIND_RESULT, status="done", summary="never mind, done",
        )
        self.assertEqual(s.pending_count(), 1)
        snapshot = s.snapshot()
        self.assertEqual(snapshot[0].kind, CUE_KIND_RESULT)
        self.assertEqual(snapshot[0].summary, "never mind, done")

    def test_park_rejects_empty_task_id(self) -> None:
        s = TaskCueStore()
        with self.assertRaises(ValueError):
            s.park(
                task_id="", session_key="u1",
                kind=CUE_KIND_RESULT, summary="x",
            )

    def test_park_rejects_unknown_kind(self) -> None:
        s = TaskCueStore()
        with self.assertRaises(ValueError):
            s.park(
                task_id="t1", session_key="u1",
                kind="bogus", summary="x",
            )

    def test_park_logs_info_line(self) -> None:
        s = TaskCueStore()
        with self.assertLogs("app.task_orchestrator", level="INFO") as cm:
            s.park(
                task_id="t42", session_key="u1",
                kind=CUE_KIND_RESULT, summary="done",
            )
        match = [r for r in cm.output if "task cue parked:" in r]
        self.assertEqual(len(match), 1, match)
        self.assertIn("task=t42", match[0])
        self.assertIn("kind=task_result", match[0])
        self.assertIn("aggregated=1", match[0])


# ── drain_for_render ────────────────────────────────────────────────


class DrainTests(unittest.TestCase):
    def test_drain_returns_fifo(self) -> None:
        s = TaskCueStore()
        for i in range(3):
            s.park(
                task_id=f"t{i}", session_key="u",
                kind=CUE_KIND_RESULT, summary=f"#{i}",
            )
        result = s.drain_for_render(turn_id="abcd1234")
        self.assertEqual([c.task_id for c in result.surfaced], ["t0", "t1", "t2"])
        self.assertEqual(result.deferred, 0)
        self.assertEqual(result.stale_dropped, [])
        self.assertEqual(s.pending_count(), 0)

    def test_drain_applies_aggregation_cap(self) -> None:
        """Excess cues stay parked; deferred count reflects the
        remainder."""
        s = TaskCueStore(max_aggregated=2)
        for i in range(5):
            s.park(
                task_id=f"t{i}", session_key="u",
                kind=CUE_KIND_RESULT, summary=f"#{i}",
            )
        result = s.drain_for_render()
        self.assertEqual(len(result.surfaced), 2)
        self.assertEqual([c.task_id for c in result.surfaced], ["t0", "t1"])
        self.assertEqual(result.deferred, 3)
        self.assertEqual(s.pending_count(), 3)
        # Subsequent drain pulls the remainder.
        result2 = s.drain_for_render()
        self.assertEqual(len(result2.surfaced), 2)
        self.assertEqual([c.task_id for c in result2.surfaced], ["t2", "t3"])
        self.assertEqual(result2.deferred, 1)
        self.assertEqual(s.pending_count(), 1)

    def test_drain_drops_stale(self) -> None:
        s = TaskCueStore(max_age_seconds=5.0)
        s.park(
            task_id="old", session_key="u",
            kind=CUE_KIND_RESULT, summary="forever ago",
        )
        # Drain at a synthetic future time that exceeds max_age.
        now_old = s.snapshot()[0].parked_at + 10.0
        result = s.drain_for_render(now=now_old)
        self.assertEqual(result.surfaced, [])
        self.assertEqual(len(result.stale_dropped), 1)
        self.assertEqual(result.stale_dropped[0].task_id, "old")
        self.assertEqual(s.pending_count(), 0)
        self.assertEqual(s.metrics_snapshot()["stale_drop_count"], 1)

    def test_drain_stale_logs_each(self) -> None:
        s = TaskCueStore(max_age_seconds=1.0)
        with self.assertLogs("app.task_orchestrator", level="INFO") as cm:
            s.park(
                task_id="ancient", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            s.park(
                task_id="alsoancient", session_key="u",
                kind=CUE_KIND_RESULT, summary="y",
            )
            future = s.snapshot()[0].parked_at + 5.0
            s.drain_for_render(now=future)
        stale_lines = [r for r in cm.output if "task cue stale-dropped:" in r]
        self.assertEqual(len(stale_lines), 2)
        self.assertTrue(any("task=ancient" in r for r in stale_lines))
        self.assertTrue(any("task=alsoancient" in r for r in stale_lines))

    def test_drain_surfaced_log_carries_turn_id(self) -> None:
        s = TaskCueStore()
        s.park(
            task_id="t1", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        with self.assertLogs("app.task_orchestrator", level="INFO") as cm:
            s.drain_for_render(turn_id="deadbeef")
        match = [r for r in cm.output if "task cue surfaced:" in r]
        self.assertEqual(len(match), 1, match)
        self.assertIn("turn=deadbeef", match[0])
        self.assertIn("count=1", match[0])

    def test_drain_empty_returns_empty_result(self) -> None:
        s = TaskCueStore()
        result = s.drain_for_render()
        self.assertEqual(result.surfaced, [])
        self.assertEqual(result.stale_dropped, [])
        self.assertEqual(result.deferred, 0)


# ── peek_for_escalation ─────────────────────────────────────────────


class PeekTests(unittest.TestCase):
    def test_peek_does_not_clear(self) -> None:
        s = TaskCueStore()
        s.park(
            task_id="t1", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        snap = s.peek_for_escalation()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0].task_id, "t1")
        # Still parked.
        self.assertEqual(s.pending_count(), 1)

    def test_peek_applies_stale_sweep(self) -> None:
        """``peek_for_escalation`` must still GC stale cues so a
        long-quiet session doesn't leak them on the escalation
        path."""
        s = TaskCueStore(max_age_seconds=1.0)
        s.park(
            task_id="old", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        future = s.snapshot()[0].parked_at + 10.0
        snap = s.peek_for_escalation(now=future)
        self.assertEqual(snap, [])
        self.assertEqual(s.pending_count(), 0)
        self.assertEqual(s.metrics_snapshot()["stale_drop_count"], 1)

    def test_peek_returns_caller_owned_snapshot(self) -> None:
        s = TaskCueStore()
        s.park(
            task_id="t1", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        snap = s.peek_for_escalation()
        snap.clear()  # Mutating the snapshot must not affect the store.
        self.assertEqual(s.pending_count(), 1)


# ── clear ───────────────────────────────────────────────────────────


class ClearTests(unittest.TestCase):
    def test_clear_removes_single_cue(self) -> None:
        s = TaskCueStore()
        s.park(
            task_id="t1", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        s.park(
            task_id="t2", session_key="u",
            kind=CUE_KIND_RESULT, summary="y",
        )
        self.assertTrue(s.clear("t1"))
        self.assertEqual(s.pending_count(), 1)
        self.assertEqual(s.snapshot()[0].task_id, "t2")

    def test_clear_unknown_returns_false(self) -> None:
        s = TaskCueStore()
        s.park(
            task_id="t1", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        self.assertFalse(s.clear("nope"))
        self.assertEqual(s.pending_count(), 1)

    def test_clear_all_wipes(self) -> None:
        s = TaskCueStore()
        for i in range(3):
            s.park(
                task_id=f"t{i}", session_key="u",
                kind=CUE_KIND_RESULT, summary=f"#{i}",
            )
        self.assertEqual(s.clear_all(), 3)
        self.assertEqual(s.pending_count(), 0)


# ── concurrency ─────────────────────────────────────────────────────


class ConcurrencyTests(unittest.TestCase):
    """The store is touched by the brain-loop consumer thread, the
    prompt-assembly thread, and the escalation timer thread. Pin the
    invariants under concurrent producers + drains."""

    def test_concurrent_parks_preserve_count(self) -> None:
        s = TaskCueStore(max_aggregated=1000)
        n_threads = 8
        per_thread = 50
        barrier = threading.Barrier(n_threads)

        def push(start: int) -> None:
            barrier.wait()
            for i in range(per_thread):
                s.park(
                    task_id=f"t{start}-{i}",
                    session_key="u",
                    kind=CUE_KIND_RESULT,
                    summary="x",
                )

        threads = [
            threading.Thread(target=push, args=(i,))
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())
        self.assertEqual(s.pending_count(), n_threads * per_thread)
        self.assertEqual(s.metrics_snapshot()["park_count"], n_threads * per_thread)

    def test_park_and_drain_race_no_loss(self) -> None:
        """A producer that interleaves with a drain doesn't lose
        cues — every parked cue eventually surfaces."""
        s = TaskCueStore(max_aggregated=1000)
        n = 200
        produced = [f"t{i}" for i in range(n)]
        seen: list[str] = []
        seen_lock = threading.Lock()
        stop = threading.Event()

        def drain_forever() -> None:
            while not stop.is_set():
                result = s.drain_for_render()
                if result.surfaced:
                    with seen_lock:
                        seen.extend(c.task_id for c in result.surfaced)
                time.sleep(0.001)

        consumer = threading.Thread(target=drain_forever)
        consumer.start()
        try:
            for task_id in produced:
                s.park(
                    task_id=task_id, session_key="u",
                    kind=CUE_KIND_RESULT, summary="x",
                )
            # Give the consumer time to drain the tail.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if s.pending_count() == 0:
                    break
                time.sleep(0.005)
        finally:
            stop.set()
            consumer.join(timeout=2.0)
        # Final drain to catch any straggling.
        tail = s.drain_for_render()
        with seen_lock:
            seen.extend(c.task_id for c in tail.surfaced)
        self.assertEqual(sorted(seen), sorted(produced))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

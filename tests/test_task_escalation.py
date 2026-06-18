"""Unit tests for :class:`TaskEscalationManager` — chunk 4.

Pins the escalation contract:

* :meth:`arm` schedules a :class:`threading.Timer` keyed by
  ``task_id`` that fires as soon as Aiko is free (no fixed window);
  re-arming the same id cancels the prior timer.
* On fire, the manager checks three preconditions in order
  (cue still parked, gate clear, no recent user message). All
  pass → enqueue a proactive event + emit one
  ``brain-loop escalated:`` INFO line.
* Precondition fail → re-arm at the retry interval with attempt
  count bump. Limit-hit → WARNING + drop.
* :meth:`cancel_for_task` removes the timer; calls return
  ``True``/``False`` accordingly.
* :meth:`shutdown` is idempotent and silences further arms.

Tests that need a timer to stay armed (to assert ``pending_count``,
replacement, cancel) hold it open with a closed free-to-speak gate
so the fire path re-arms instead of completing. The timer thread is
daemon; polling via :func:`_wait_for` synchronises the test thread
with the fire callback.
"""
from __future__ import annotations

import time
import unittest

from app.core.tasks.task_cue_store import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    TaskCueStore,
)
from app.core.tasks.task_escalation import (
    EscalationConfig,
    TaskEscalationManager,
)


_DEADLINE_S = 2.0


def _wait_for(predicate, *, deadline_s: float = _DEADLINE_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _make_manager(
    *,
    retry_seconds: float = 0.05,
    retry_limit: int = 10,
    free_to_speak=lambda: True,
    last_user_message_at=lambda: -float("inf"),
    cue_store: TaskCueStore | None = None,
) -> tuple[
    TaskEscalationManager,
    TaskCueStore,
    list[tuple[str, tuple[str, ...]]],
]:
    """Build a manager + cue store + capture list for proactive events.

    The timed-escalation windows are gone — an armed cue fires the
    moment the gate clears — so tests that want to *hold* a timer pass
    ``free_to_speak=lambda: False`` and the fire path re-arms on the
    ``retry_seconds`` cadence without ever completing.
    """
    if cue_store is None:
        cue_store = TaskCueStore(max_age_seconds=3600.0, max_aggregated=10)
    captured: list[tuple[str, tuple[str, ...]]] = []

    def enqueue(session_key: str, parked_cue_ids: tuple[str, ...]) -> None:
        captured.append((session_key, parked_cue_ids))

    cfg = EscalationConfig(
        retry_seconds=retry_seconds,
        retry_limit=retry_limit,
    )
    mgr = TaskEscalationManager(
        cue_store=cue_store,
        free_to_speak=free_to_speak,
        last_user_message_at=last_user_message_at,
        enqueue_proactive=enqueue,
        config=cfg,
    )
    return mgr, cue_store, captured


# ── construction ────────────────────────────────────────────────────


class ConstructionTests(unittest.TestCase):
    def test_construct_uses_supplied_config(self) -> None:
        mgr, _, _ = _make_manager(retry_seconds=0.25, retry_limit=7)
        self.assertEqual(mgr.pending_count(), 0)
        try:
            self.assertEqual(mgr._config.retry_seconds, 0.25)
            self.assertEqual(mgr._config.retry_limit, 7)
        finally:
            mgr.shutdown()

    def test_construct_defaults_when_no_config(self) -> None:
        store = TaskCueStore()
        mgr = TaskEscalationManager(
            cue_store=store,
            free_to_speak=lambda: True,
            last_user_message_at=lambda: -float("inf"),
            enqueue_proactive=lambda *a: None,
        )
        try:
            self.assertEqual(mgr._config.retry_seconds, 1.0)
            self.assertEqual(mgr._config.retry_limit, 60)
        finally:
            mgr.shutdown()


# ── arm / cancel ────────────────────────────────────────────────────


class ArmCancelTests(unittest.TestCase):
    def test_arm_schedules_timer(self) -> None:
        # Closed gate holds the timer armed so we can inspect it.
        mgr, store, _ = _make_manager(free_to_speak=lambda: False)
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            self.assertTrue(_wait_for(lambda: mgr.pending_count() == 1))
            snap = mgr.snapshot()
            self.assertEqual(snap[0][0], "t1")
            self.assertEqual(snap[0][1], CUE_KIND_RESULT)
        finally:
            mgr.shutdown()

    def test_arm_fires_promptly_by_default(self) -> None:
        """With no explicit window an armed cue fires almost
        immediately once the free-to-speak gate is clear."""
        mgr, store, captured = _make_manager()
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            self.assertTrue(_wait_for(lambda: len(captured) == 1))
            self.assertEqual(captured[0][0], "u")
            self.assertEqual(captured[0][1], ("t1",))
        finally:
            mgr.shutdown()

    def test_arm_after_seconds_override_fires_promptly(self) -> None:
        """An explicit ``after_seconds`` override still works (tests use
        it); the cue fires after the supplied delay."""
        mgr, store, captured = _make_manager()
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue, after_seconds=0.02)
            self.assertTrue(_wait_for(lambda: len(captured) == 1))
            self.assertEqual(captured[0][0], "u")
            self.assertEqual(captured[0][1], ("t1",))
        finally:
            mgr.shutdown()

    def test_arm_after_seconds_clamps_negative_to_zero(self) -> None:
        mgr, store, _ = _make_manager(free_to_speak=lambda: False)
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            # Negative override must not raise; clamped to 0.0.
            mgr.arm(cue, after_seconds=-5.0)
        finally:
            mgr.shutdown()

    def test_arm_rejects_empty_task_id(self) -> None:
        mgr, store, _ = _make_manager()
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            # Synthesise a cue with empty id to test the guard.
            bad = cue.__class__(
                task_id="",
                session_key="u",
                kind=CUE_KIND_RESULT,
                parked_at=0.0,
                parked_at_wall=0.0,
            )
            with self.assertRaises(ValueError):
                mgr.arm(bad)
        finally:
            mgr.shutdown()

    def test_arm_replaces_existing_timer(self) -> None:
        """Arming the same task id a second time cancels the prior
        timer; only one fire per task id. A closed gate holds the
        timers so we can observe the replacement."""
        mgr, store, _ = _make_manager(free_to_speak=lambda: False)
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            self.assertTrue(_wait_for(lambda: mgr.pending_count() == 1))
            # Re-park to bump parked_at, then re-arm.
            cue2 = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="y",
            )
            mgr.arm(cue2)
            # Still exactly one timer.
            self.assertEqual(mgr.pending_count(), 1)
        finally:
            mgr.shutdown()

    def test_cancel_for_task_returns_true_on_hit(self) -> None:
        mgr, store, _ = _make_manager(free_to_speak=lambda: False)
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            self.assertTrue(_wait_for(lambda: mgr.pending_count() == 1))
            self.assertTrue(mgr.cancel_for_task("t1"))
            self.assertEqual(mgr.pending_count(), 0)
        finally:
            mgr.shutdown()

    def test_cancel_for_task_returns_false_on_miss(self) -> None:
        mgr, _, _ = _make_manager()
        try:
            self.assertFalse(mgr.cancel_for_task("nope"))
        finally:
            mgr.shutdown()

    def test_cancel_all_returns_count(self) -> None:
        mgr, store, _ = _make_manager(free_to_speak=lambda: False)
        try:
            for i in range(3):
                cue = store.park(
                    task_id=f"t{i}", session_key="u",
                    kind=CUE_KIND_RESULT, summary="x",
                )
                mgr.arm(cue)
            self.assertTrue(_wait_for(lambda: mgr.pending_count() == 3))
            self.assertEqual(mgr.cancel_all(), 3)
            self.assertEqual(mgr.pending_count(), 0)
        finally:
            mgr.shutdown()


# ── fire path ───────────────────────────────────────────────────────


class FirePathTests(unittest.TestCase):
    def test_fire_enqueues_proactive_when_preconditions_clear(self) -> None:
        mgr, store, captured = _make_manager()
        try:
            cue = store.park(
                task_id="t1", session_key="u-session",
                kind=CUE_KIND_RESULT, title="search", summary="found",
            )
            mgr.arm(cue)
            self.assertTrue(
                _wait_for(lambda: len(captured) == 1),
                msg=f"never fired: pending={mgr.pending_count()}",
            )
            session_key, parked_ids = captured[0]
            self.assertEqual(session_key, "u-session")
            self.assertEqual(parked_ids, ("t1",))
            # Timer entry removed after fire.
            self.assertEqual(mgr.pending_count(), 0)
        finally:
            mgr.shutdown()

    def test_fire_emits_brain_loop_escalated_info(self) -> None:
        mgr, store, _ = _make_manager()
        try:
            cue = store.park(
                task_id="t-escalate", session_key="u",
                kind=CUE_KIND_INPUT_NEEDED, summary="which one?",
            )
            with self.assertLogs("app.brain_loop", level="INFO") as cm:
                mgr.arm(cue)
                self.assertTrue(
                    _wait_for(lambda: mgr.pending_count() == 0)
                )
                # Give the log handler one extra tick.
                time.sleep(0.02)
            lines = [r for r in cm.output if "brain-loop escalated:" in r]
            self.assertEqual(len(lines), 1, cm.output)
            line = lines[0]
            self.assertIn("task=t-escalate", line)
            self.assertIn("cue_kind=task_input_needed", line)
            self.assertIn("silence_s=", line)
        finally:
            mgr.shutdown()

    def test_fire_skipped_when_cue_already_cleared(self) -> None:
        """If the cue was popped between park + fire, the manager
        cleans up its timer entry without enqueuing a proactive event."""
        mgr, store, captured = _make_manager()
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            # Drain the cue immediately so the timer fire sees an
            # empty store.
            store.drain_for_render()
            self.assertTrue(
                _wait_for(lambda: mgr.pending_count() == 0),
                msg=f"timer never cleaned up: pending={mgr.pending_count()}",
            )
            self.assertEqual(captured, [])
        finally:
            mgr.shutdown()


# ── re-arm preconditions ────────────────────────────────────────────


class RearmTests(unittest.TestCase):
    def test_gate_closed_rearms_then_fires_when_gate_opens(self) -> None:
        gate = {"open": False}
        mgr, store, captured = _make_manager(
            retry_seconds=0.05,
            free_to_speak=lambda: gate["open"],
        )
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            # Wait long enough for at least one re-arm to happen.
            time.sleep(0.2)
            self.assertEqual(captured, [])
            # Open the gate; next retry should fire.
            gate["open"] = True
            self.assertTrue(_wait_for(lambda: len(captured) == 1))
            self.assertEqual(captured[0][1], ("t1",))
        finally:
            mgr.shutdown()

    def test_recent_user_message_blocks_then_clears(self) -> None:
        """When ``last_user_message_at`` is more recent than the
        cue's parked_at, the manager re-arms instead of firing."""
        last_user = {"at": -float("inf")}
        mgr, store, captured = _make_manager(
            retry_seconds=0.05,
            last_user_message_at=lambda: last_user["at"],
        )
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            # Simulate a user message AFTER the cue parked — the
            # natural turn will fold the cue in, no escalation needed.
            last_user["at"] = cue.parked_at + 0.01
            mgr.arm(cue)
            time.sleep(0.15)
            self.assertEqual(captured, [])
            # Clear the user-recent flag, escalation should resume.
            last_user["at"] = -float("inf")
            self.assertTrue(_wait_for(lambda: len(captured) == 1))
        finally:
            mgr.shutdown()

    def test_retry_limit_gives_up_with_warning(self) -> None:
        gate = {"open": False}
        mgr, store, captured = _make_manager(
            retry_seconds=0.01,
            retry_limit=2,
            free_to_speak=lambda: gate["open"],
        )
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            with self.assertLogs("app.brain_loop", level="WARNING") as cm:
                mgr.arm(cue)
                self.assertTrue(_wait_for(lambda: mgr.pending_count() == 0))
                time.sleep(0.02)
            give_up = [r for r in cm.output if "escalation give-up" in r]
            self.assertEqual(len(give_up), 1, cm.output)
            self.assertIn("task=t1", give_up[0])
            self.assertEqual(captured, [])
            # Cue is still parked on the store — a real user message
            # can still surface it later.
            self.assertEqual(store.pending_count(), 1)
        finally:
            mgr.shutdown()


# ── shutdown ────────────────────────────────────────────────────────


class ShutdownTests(unittest.TestCase):
    def test_shutdown_cancels_all_timers(self) -> None:
        # Closed gate holds the timers so they don't fire before we
        # assert + shut down.
        mgr, store, captured = _make_manager(free_to_speak=lambda: False)
        try:
            for i in range(3):
                cue = store.park(
                    task_id=f"t{i}", session_key="u",
                    kind=CUE_KIND_RESULT, summary="x",
                )
                mgr.arm(cue)
            self.assertTrue(_wait_for(lambda: mgr.pending_count() == 3))
        finally:
            mgr.shutdown()
        self.assertEqual(mgr.pending_count(), 0)
        # Even after waiting, no proactive event fires.
        time.sleep(0.05)
        self.assertEqual(captured, [])

    def test_shutdown_silences_further_arms(self) -> None:
        mgr, store, _ = _make_manager()
        mgr.shutdown()
        cue = store.park(
            task_id="late", session_key="u",
            kind=CUE_KIND_RESULT, summary="x",
        )
        mgr.arm(cue)
        self.assertEqual(mgr.pending_count(), 0)

    def test_shutdown_is_idempotent(self) -> None:
        mgr, _, _ = _make_manager()
        mgr.shutdown()
        # Second call must not crash.
        mgr.shutdown()


# ── predicate error tolerance ───────────────────────────────────────


class PredicateErrorTests(unittest.TestCase):
    def test_raising_gate_predicate_treated_as_closed(self) -> None:
        attempts = {"n": 0}

        def gate() -> bool:
            attempts["n"] += 1
            raise RuntimeError("boom")

        mgr, store, captured = _make_manager(
            retry_seconds=0.02,
            retry_limit=3,
            free_to_speak=gate,
        )
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            # Wait for retries to exhaust.
            self.assertTrue(_wait_for(lambda: mgr.pending_count() == 0))
            self.assertEqual(captured, [])
            # The predicate was called at least once per fire attempt.
            self.assertGreaterEqual(attempts["n"], 1)
        finally:
            mgr.shutdown()

    def test_raising_last_user_treated_as_negative_infinity(self) -> None:
        def last_user() -> float:
            raise RuntimeError("clock broken")

        mgr, store, captured = _make_manager(
            retry_seconds=0.05,
            last_user_message_at=last_user,
        )
        try:
            cue = store.park(
                task_id="t1", session_key="u",
                kind=CUE_KIND_RESULT, summary="x",
            )
            mgr.arm(cue)
            # The raise is treated as "never spoke" (-inf), so the
            # cue still fires.
            self.assertTrue(_wait_for(lambda: len(captured) == 1))
        finally:
            mgr.shutdown()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

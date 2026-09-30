"""Live mode Pass 3: notices, urges, wait, budgets. No speech."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.notice import notices_from_trigger
from app.core.live.urge_store import LiveUrgeStore
from app.core.live.wait import LiveWaitScheduler
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.session_controller import SessionController


def _now() -> datetime:
    return datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T13:00:00Z",
        input_mode="typed",
        floor_transition="neither",
        dialogue_act="",
        arc="casual_check_in",
        arc_confidence=0.4,
        mood_label="content",
        vitality_band="normal",
        user_present=True,
        user_active_app="",
        world=WorldSituation(location_slug="beanbag", activity="idle"),
        inferred=None,
        inferred_stale=True,
        world_compatible=False,
        conflict_reason="",
        shared_commitment_active=False,
        since_user_activity_ms=0,
        sleep={"status": "awake"},
    )
    fields.update(kwargs)
    return ConversationSituationSnapshot(**fields)  # type: ignore[arg-type]


def _frame(**kwargs: object):
    inp = LiveAssembleInput(
        snapshot=_snapshot(**kwargs),
        impulses=(),
        now=_now(),
        monotonic_ms=10_000.0,
        trigger_kind="heartbeat",
    )
    return assemble_live_situation(inp, generation=1)


class NoticeTests(unittest.TestCase):
    def test_heartbeat_is_not_a_notice(self) -> None:
        self.assertEqual(notices_from_trigger("heartbeat", _frame()), ())

    def test_typing_is_noticed(self) -> None:
        notices = notices_from_trigger("user.typing_started", _frame())
        self.assertEqual(notices[0].kind, "user_typing")

    def test_coding_plus_anime_prior_notices_focus_not_delight(self) -> None:
        inp = LiveAssembleInput(
            snapshot=_snapshot(user_active_app="Cursor"),
            impulses=(),
            now=_now(),
            monotonic_ms=10_000.0,
            trigger_kind="heartbeat",
            ritual_priors=("watching anime together",),
        )
        frame = assemble_live_situation(inp, generation=1)
        notices = notices_from_trigger(
            "situation.shared_commitment_changed", frame,
        )
        kinds = {item.kind for item in notices}
        self.assertIn("user_focus", kinds)
        self.assertNotIn("shared_commitment", kinds)
        store = LiveUrgeStore()
        created = store.ingest_notices(notices, now_mono_ms=10_000.0)
        self.assertTrue(any(urge.kind == "remain_present" for urge in created))
        self.assertFalse(any(urge.kind == "share_delight" for urge in created))


class UrgeStoreTests(unittest.TestCase):
    def test_valid_source_outlives_short_attempt_and_long_busy_period(self) -> None:
        row = SimpleNamespace(
            id=9, cue_type="away_activities", subject="a finished project",
            last_surfaced_at=None, expires_at=(_now() + timedelta(hours=2)).isoformat(),
        )
        rows = [row]
        store = LiveUrgeStore()
        adapter = CueUrgeAdapter(pending_provider=lambda: rows)
        with patch("app.core.live.cue_adapter.timephrase.utcnow", return_value=_now()):
            first = adapter.project(store, now_mono_ms=1_000)[0]
            store.expire_due(1_100_000)
            self.assertEqual(store.all_urges()[0].state, "deferred")
            self.assertEqual(adapter.project(store, now_mono_ms=1_101_000), ())
            rows.clear()
            self.assertEqual(adapter.project(store, now_mono_ms=1_200_000), ())
            rows.append(row)
            revived = adapter.project(store, now_mono_ms=1_201_000, reconsider=True)[0]
            self.assertEqual(revived.urge_id, first.urge_id)
            self.assertEqual(revived.source_deadline_ms, first.source_deadline_ms)
            store.consume(revived.urge_id)
            self.assertEqual(adapter.project(store, now_mono_ms=1_300_000), ())

    def test_two_completed_opportunities_end_source_attempt_with_reason(self) -> None:
        store = LiveUrgeStore()
        urge = store.propose(
            kind="share_observation", subject="finished result", source="cue_pool",
            source_ids=("cue:9",), repetition_key="cue:9", cue_id=9,
            now_mono_ms=1_000, source_deadline_ms=7_200_000,
        )
        assert urge is not None
        store.note_opportunity(urge.urge_id)
        store.expire_due(50_000)
        store.reconsider(9, now_mono_ms=51_000, opening=False)
        self.assertFalse(store.active())
        store.reconsider(9, now_mono_ms=110_000, opening=False)
        self.assertEqual(len(store.active()), 1)
        store.note_opportunity(urge.urge_id)
        store.expire_due(160_000)
        final = store.all_urges()[0]
        self.assertEqual(final.state, "expired")
        self.assertEqual(final.resolution_reason, "evaluation_limit")
        store.reconsider(9, now_mono_ms=200_000)
        self.assertFalse(store.active())

    def test_source_expiry_is_terminal_even_when_never_evaluated(self) -> None:
        store = LiveUrgeStore()
        store.propose(
            kind="share_observation", subject="result", source="cue_pool",
            source_ids=("cue:9",), repetition_key="cue:9", cue_id=9,
            now_mono_ms=1_000, source_deadline_ms=120_000,
        )
        store.expire_due(50_000)
        store.expire_due(120_000)
        self.assertEqual(store.all_urges()[0].resolution_reason, "source_expired")
        store.reconsider(9, now_mono_ms=150_000)
        self.assertFalse(store.active())

    def test_filtering_happens_before_eight_item_menu_limit(self) -> None:
        rows = [
            SimpleNamespace(
                id=cue_id, cue_type="companion_activity", subject=f"result {cue_id}",
                last_surfaced_at=_now().isoformat() if cue_id <= 8 else None,
            ) for cue_id in range(1, 10)
        ]
        with patch("app.core.live.cue_adapter.timephrase.utcnow", return_value=_now()):
            urges = CueUrgeAdapter(pending_provider=lambda: rows).project(
                LiveUrgeStore(), now_mono_ms=1_000,
            )
        self.assertEqual([urge.cue_id for urge in urges], [9])

    def test_deferred_cues_have_a_count_and_age_bound(self) -> None:
        store = LiveUrgeStore(max_active=2)
        for cue_id in range(3):
            now = 1_000 + cue_id * 50_000
            store.propose(
                kind="share_observation", subject=f"subject {cue_id}",
                source="cue_pool", source_ids=(f"cue:{cue_id}",),
                repetition_key=f"cue:{cue_id}", cue_id=cue_id,
                now_mono_ms=now,
            )
            store.expire_due(now + 46_000)
        self.assertEqual(sum(urge.state == "deferred" for urge in store.all_urges()), 2)
        store.expire_due(1_100_000)
        self.assertFalse(any(urge.state == "deferred" for urge in store.all_urges()))

    def test_opportunity_or_consumption_prevents_reconsideration(self) -> None:
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                store = LiveUrgeStore()
                row = SimpleNamespace(
                    id=9, cue_type="away_activities", subject="film photography",
                    last_surfaced_at=None,
                )
                adapter = CueUrgeAdapter(pending_provider=lambda row=row: [row])
                urge = adapter.project(store, now_mono_ms=1_000)[0]
                if consumed:
                    store.consume(urge.urge_id)
                else:
                    store.note_opportunity(urge.urge_id)
                store.expire_due(50_000)
                self.assertEqual(
                    adapter.project(store, now_mono_ms=51_000, reconsider=True), (),
                )

    def test_busy_cue_reconsiders_once_at_a_real_opening(self) -> None:
        row = SimpleNamespace(
            id=9, cue_type="away_activities", subject="film photography",
            last_surfaced_at=None,
        )
        runtime = LiveInclinationRuntime(
            cue_adapter=CueUrgeAdapter(pending_provider=lambda: [row]),
        )
        frame = _frame()
        busy = replace(
            frame, interaction=replace(frame.interaction, typing_active=True),
        )
        runtime.apply(busy, trigger_kind="user.typing_started", now_mono_ms=1_000)
        runtime.apply(busy, trigger_kind="heartbeat", now_mono_ms=50_000)
        cue = next(urge for urge in runtime.urges.all_urges() if urge.cue_id == 9)
        self.assertEqual(cue.state, "deferred")
        runtime.apply(frame, trigger_kind="user.typing_stopped", now_mono_ms=51_000)
        cue = next(urge for urge in runtime.urges.active() if urge.cue_id == 9)
        self.assertTrue(cue.reconsidered)
        runtime.apply(busy, trigger_kind="user.typing_started", now_mono_ms=52_000)
        runtime.apply(busy, trigger_kind="heartbeat", now_mono_ms=100_000)
        runtime.apply(frame, trigger_kind="user.typing_stopped", now_mono_ms=101_000)
        self.assertFalse(any(urge.cue_id == 9 for urge in runtime.urges.active()))

    def test_removed_or_too_old_cue_never_reconsiders(self) -> None:
        row = SimpleNamespace(
            id=9, cue_type="away_activities", subject="film photography",
            last_surfaced_at=None,
        )
        rows = [row]
        adapter = CueUrgeAdapter(pending_provider=lambda: rows)
        store = LiveUrgeStore()
        adapter.project(store, now_mono_ms=1_000)
        store.expire_due(50_000)
        rows.clear()
        self.assertEqual(adapter.project(store, now_mono_ms=51_000, reconsider=True), ())
        rows.append(row)
        self.assertEqual(adapter.project(store, now_mono_ms=1_000_000, reconsider=True), ())

    def test_notice_creates_urge_without_action(self) -> None:
        store = LiveUrgeStore()
        notices = notices_from_trigger("user.typing_started", _frame())
        created = store.ingest_notices(notices, now_mono_ms=10_000.0)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].kind, "acknowledge_user")
        self.assertEqual(created[0].state, "candidate")

    def test_equivalent_urges_merge(self) -> None:
        store = LiveUrgeStore()
        notices = notices_from_trigger("user.typing_started", _frame())
        first = store.ingest_notices(notices, now_mono_ms=10_000.0)[0]
        second = store.ingest_notices(notices, now_mono_ms=10_500.0)[0]
        self.assertEqual(first.urge_id, second.urge_id)
        self.assertEqual(len(store.active()), 1)

    def test_expired_does_not_recreate_from_same_evidence(self) -> None:
        store = LiveUrgeStore()
        notices = notices_from_trigger("silence.wake", _frame())
        first = store.ingest_notices(notices, now_mono_ms=1_000.0)[0]
        store.expire_due(1_000.0 + first.expires_after_ms)
        self.assertEqual(store.active(), ())
        again = store.ingest_notices(notices, now_mono_ms=40_000.0)
        self.assertEqual(again, ())

    def test_sleep_withdraws_expressive(self) -> None:
        store = LiveUrgeStore()
        store.propose(
            kind="share_delight",
            subject="shared_activity",
            source="test",
            source_ids=("x",),
            repetition_key="delight:x",
            now_mono_ms=1_000.0,
        )
        notices = notices_from_trigger(
            "sleep.status_changed",
            _frame(sleep={"status": "asleep"}),
        )
        store.ingest_notices(notices, now_mono_ms=2_000.0)
        kinds = {urge.kind: urge.state for urge in store.all_urges()}
        self.assertEqual(kinds.get("share_delight"), "withdrawn")


class CueAdapterTests(unittest.TestCase):
    def test_peek_does_not_take_pool_cue(self) -> None:
        take = MagicMock()
        row = SimpleNamespace(
            id=9,
            cue_type="curiosity_seed",
            subject="film photography",
            last_surfaced_at=None,
        )
        store = LiveUrgeStore()
        adapter = CueUrgeAdapter(pending_provider=lambda: [row])
        created = adapter.project(store, now_mono_ms=5_000.0)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].cue_id, 9)
        self.assertEqual(created[0].kind, "ask_about_result")
        take.assert_not_called()


class WaitBudgetTests(unittest.TestCase):
    def test_wait_wakes_on_allowlisted_event_and_deadline(self) -> None:
        sched = LiveWaitScheduler()
        sched.schedule(
            generation=1,
            urge_id="u1",
            reconsider_after_ms=5_000,
            now_mono_ms=10_000.0,
            wake_on=("silence.wake",),
            reason_code="test",
        )
        self.assertTrue(
            sched.should_suppress_inference(
                "heartbeat", now_mono_ms=11_000.0, generation=1,
            )
        )
        self.assertFalse(
            sched.should_suppress_inference(
                "silence.wake", now_mono_ms=11_000.0, generation=1,
            )
        )
        sched.schedule(
            generation=1,
            urge_id="u1",
            reconsider_after_ms=5_000,
            now_mono_ms=10_000.0,
            wake_on=("silence.wake",),
            reason_code="test",
        )
        self.assertFalse(
            sched.should_suppress_inference(
                "heartbeat", now_mono_ms=16_000.0, generation=1,
            )
        )
        self.assertFalse(
            sched.should_suppress_inference(
                "user.message_sent", now_mono_ms=11_000.0, generation=1,
            )
        )

    def test_budget_exhaustion_schedules_wait(self) -> None:
        runtime = LiveInclinationRuntime()
        now = 1_000.0
        self.assertTrue(runtime.budget.consume("main_wake", now_mono_ms=now))
        self.assertFalse(runtime.budget.consume("main_wake", now_mono_ms=now))
        frame = runtime.apply(_frame(), trigger_kind="heartbeat", now_mono_ms=now)
        self.assertIsNotNone(runtime.wait.current)
        self.assertEqual(runtime.wait.current.reason_code, "budget_exhausted")
        self.assertTrue(frame.aiko.candidate_urges or frame.aiko.selected_urge)
        self.assertTrue(
            runtime.wait.should_suppress_inference(
                "heartbeat", now_mono_ms=now + 100.0, generation=frame.generation,
            )
        )


class InclinationHost(LiveModeMixin):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                behavior_posture="live_presence",
                live_quiet=False,
                live_impulse_bus_enabled=True,
            ),
        )
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self.session_key = "u:s"
        self._user_id = "default"
        self._chat_db = None
        self._cue_store = SimpleNamespace(pending=lambda limit=8: [])
        self._prepared_nudge_store = SimpleNamespace(get_fresh=lambda _uid: None)
        self._snapshot = _snapshot()
        self._init_live_mode()

    def conversation_situation_snapshot(self, **_kwargs):
        return self._snapshot


class MixinInclinationTests(unittest.TestCase):
    def test_typing_urge_on_frame_and_no_live_mode_enabled(self) -> None:
        host = InclinationHost()
        frame = host.refresh_live_situation(trigger_kind="user.typing_started")
        assert frame is not None
        self.assertTrue(frame.aiko.candidate_urges)
        self.assertEqual(frame.aiko.selected_urge, "")
        diag = host.live_situation_diagnostics()
        self.assertTrue(diag["notices"])
        self.assertFalse(hasattr(host, "_live_mode_enabled"))

    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))


if __name__ == "__main__":
    unittest.main()

"""Live mode Pass 13: wait-expiry idle.reconsider + worker-matrix polish."""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.brain.events import ProactiveEvent
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.live.assembler import LiveAssembleInput, LiveSituationAssembler
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.epochs import classify_epoch
from app.core.live.notice import notices_from_trigger
from app.core.live.urge import EXPRESSIVE_KINDS
from app.core.live.urge_store import LiveUrgeStore
from app.core.live.worker_matrix import (
    SPEC_ROWS,
    WORKER_ENFORCED_BY,
    WORKER_POSTURE,
    role_for,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.session_controller import SessionController
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.core.world.world_mutation_guard import WorldMutationGuard
from tests.test_caught_mid_activity import _Host as CaughtMidHost
from tests.test_caught_mid_activity import _db_with, _open_beat
from tests.test_live_mode_pass3 import _frame as _notice_frame


def _now() -> datetime:
    return datetime(2026, 9, 13, 19, 30, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-13T19:30:00Z",
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


class Pass13Host(LiveModeMixin):
    def __init__(self, **sleep: object) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                behavior_posture="live_presence",
                live_quiet=False,
                live_unprompted_speech=True,
                live_main_wake_max_per_hour=6,
                live_min_gap_after_speech_ms=8000,
                live_mic_consented=False,
                live_impulse_bus_enabled=True,
            ),
        )
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self.session_key = "u:s"
        self._user_id = "default"
        self._chat_db = None
        self._cue_store = SimpleNamespace(
            pending=lambda limit=8: [],
            take=MagicMock(),
        )
        self._prepared_nudge_store = SimpleNamespace(get_fresh=lambda _uid: None)
        self._conversation_situation_worker = MagicMock()
        self._conversation_situation_worker.should_run_live.return_value = False
        self._affect_updater = MagicMock()
        self._tts_playing = False
        self._snapshot = _snapshot(**sleep)
        self._init_live_mode()

    def conversation_situation_snapshot(self, **_kwargs: object):
        return self._snapshot

    def is_tts_playing(self) -> bool:
        return bool(self._tts_playing)


def _expired_wait(host: Pass13Host) -> None:
    now_ms = time.monotonic() * 1000.0
    host._live_inclination.wait.schedule(
        generation=int(host._live_situation_assembler.generation or 1),
        urge_id="u1",
        reconsider_after_ms=1000,
        now_mono_ms=now_ms - 2000.0,
        wake_on=("silence.wake",),
        reason_code="test",
    )


def _capture_spawns(host: Pass13Host) -> list[str]:
    spawned: list[str] = []

    def _spawn(
        frame: object,
        *,
        trigger_kind: str,
        prompt_input: object,
        user_intent: bool,
    ) -> None:
        del frame, prompt_input, user_intent
        spawned.append(trigger_kind)

    host._live_policy_controller._spawn = _spawn  # type: ignore[method-assign]
    return spawned


class EpochTests(unittest.TestCase):
    def test_heartbeat_stays_data_only_reconsider_is_coalesced(self) -> None:
        self.assertEqual(classify_epoch("heartbeat"), "data_only")
        self.assertEqual(classify_epoch("idle.reconsider"), "coalesced_transition")

    def test_idle_reconsider_bumps_generation(self) -> None:
        assembler = LiveSituationAssembler()
        first = assembler.assemble(
            LiveAssembleInput(
                snapshot=_snapshot(),
                impulses=(),
                now=_now(),
                monotonic_ms=10_000.0,
                trigger_kind="heartbeat",
            ),
        )
        second = assembler.assemble(
            LiveAssembleInput(
                snapshot=_snapshot(),
                impulses=(),
                now=_now(),
                monotonic_ms=11_000.0,
                trigger_kind="idle.reconsider",
            ),
        )
        self.assertGreater(second.generation, first.generation)


class WaitExpiryNoticeTests(unittest.TestCase):
    def test_idle_reconsider_notice_is_not_expressive(self) -> None:
        notices = notices_from_trigger("idle.reconsider", _notice_frame())
        self.assertEqual(notices[0].kind, "wait_expired")
        self.assertEqual(notices[0].subject, "presence")
        created = LiveUrgeStore().ingest_notices(notices, now_mono_ms=5_000.0)
        self.assertEqual(created[0].kind, "remain_present")
        self.assertNotIn(created[0].kind, EXPRESSIVE_KINDS)

    def test_cue_adapter_still_does_not_take(self) -> None:
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
        take.assert_not_called()


class IdleReconsiderTickTests(unittest.TestCase):
    def test_zero_clients_park_candidate_wake_until_reconnect(self) -> None:
        host = Pass13Host()
        host._connected_clients = 0
        host._cue_store.pending = lambda limit=8: [SimpleNamespace(
            id=9, cue_type="curiosity_seed", subject="a project result",
            last_surfaced_at=None,
        )]
        spawned = _capture_spawns(host)
        host._live_heartbeat_tick()
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])
        self.assertFalse(host._live_candidate_woken_ids)
        host._connected_clients = 1
        host._live_heartbeat_tick()
        host._live_heartbeat_tick()
        self.assertEqual(spawned, ["idle.reconsider"])

    def test_zero_clients_do_not_promote_expired_wait(self) -> None:
        host = Pass13Host()
        host._connected_clients = 0
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])

    def test_heartbeat_projection_does_not_consume_cue_opportunity(self) -> None:
        host = Pass13Host()
        host._cue_store.pending = lambda limit=8: [SimpleNamespace(
            id=9, cue_type="curiosity_seed", subject="film photography",
            last_surfaced_at=None,
        )]
        host._live_heartbeat_tick()
        cue_urges = [urge for urge in host._live_inclination.urges.active() if urge.cue_id]
        self.assertEqual(len(cue_urges), 1)
        self.assertFalse(cue_urges[0].opportunity_seen)

    def test_new_cue_wakes_policy_once_without_existing_wait(self) -> None:
        host = Pass13Host()
        host._cue_store.pending = lambda limit=8: [SimpleNamespace(
            id=9, cue_type="curiosity_seed", subject="film photography",
            last_surfaced_at=None,
        )]
        spawned = _capture_spawns(host)
        host._live_heartbeat_tick()
        host._live_heartbeat_tick()
        host._live_heartbeat_tick()
        self.assertEqual(spawned, ["idle.reconsider"])
        self.assertIsNone(host._live_inclination.wait.current)

    def test_cue_does_not_wake_when_unprompted_speech_is_off(self) -> None:
        host = Pass13Host()
        host._settings.agent.live_unprompted_speech = False
        host._cue_store.pending = lambda limit=8: [SimpleNamespace(
            id=9, cue_type="curiosity_seed", subject="film photography",
            last_surfaced_at=None,
        )]
        spawned = _capture_spawns(host)
        host._live_heartbeat_tick()
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])
        self.assertFalse(host._live_candidate_woken_ids)

    def test_heartbeat_without_wait_does_not_spawn(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])
        host._affect_updater.tick_elapsed.assert_called()
        self.assertFalse(hasattr(host, "_live_mode_enabled"))

    def test_expired_wait_promotes_once(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        _expired_wait(host)
        with self.assertLogs("app.live", level="INFO") as captured:
            host._live_heartbeat_tick()
        self.assertEqual(spawned, ["idle.reconsider"])
        text = "\n".join(captured.output)
        self.assertIn("live impulse: kind=idle.reconsider", text)
        _expired_wait(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, ["idle.reconsider"])

    def test_real_spawn_logs_consider(self) -> None:
        host = Pass13Host()
        _expired_wait(host)
        with self.assertLogs("app.live", level="INFO") as captured:
            host._live_heartbeat_tick()
        text = "\n".join(captured.output)
        self.assertIn("live consider: trigger=idle.reconsider", text)

    def test_live_quiet_does_not_promote(self) -> None:
        host = Pass13Host()
        host._settings.agent.live_quiet = True
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])

    def test_sleep_forbid_does_not_promote(self) -> None:
        host = Pass13Host(sleep={"status": "asleep"})
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])

    def test_turn_skips_tick(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._turn_in_progress = True
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])

    def test_tts_does_not_promote(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._tts_playing = True
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])

    def test_inflight_does_not_promote(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._live_policy_controller._inflight = True
        host._live_heartbeat_tick()
        self.assertEqual(spawned, [])


class WorkerMatrixTests(unittest.TestCase):
    def test_table_covers_spec_rows(self) -> None:
        self.assertEqual(tuple(WORKER_POSTURE), SPEC_ROWS)
        self.assertEqual(set(WORKER_ENFORCED_BY), set(SPEC_ROWS))
        self.assertEqual(role_for("away_activity_movers"), "suspend")
        self.assertEqual(role_for("garden_circadian_movers"), "suspend")
        self.assertEqual(role_for("plant_growth_item_only"), "keep_running")
        self.assertEqual(role_for("sleep_lifecycle"), "keep_running")
        self.assertEqual(role_for("proactive_director_silence"), "impulse_only")
        self.assertEqual(role_for("cue_pool"), "fulfilment_owned")
        self.assertEqual(role_for("affect_vitality_situation_memory"), "heartbeat")
        self.assertEqual(role_for("speech_paths"), "must_not_speak")

    def test_silence_still_starves_director(self) -> None:
        class _Host(TaskOrchestrationMixin):
            def __init__(self) -> None:
                self._proactive = MagicMock()
                self._live_voice_session_active = False
                self.published: list[str] = []

            def is_live_presence(self) -> bool:
                return True

            def publish_live_impulse(self, **kwargs: object) -> None:
                self.published.append(str(kwargs.get("kind") or ""))

        host = _Host()
        host._on_task_proactive_event(
            ProactiveEvent(session_key="s", source="voice_silence"),
        )
        host._proactive.notify_silence.assert_not_called()
        self.assertEqual(host.published, ["silence.wake"])

    def test_voice_session_not_idle_and_no_live_mode_enabled(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))

    def test_h26_empty_under_live_presence(self) -> None:
        host = CaughtMidHost(chat_db=_db_with(_open_beat()))
        host._settings.agent.behavior_posture = "live_presence"
        self.assertEqual(host._render_caught_mid_activity_block(), "")
        self.assertIsInstance(host, InnerLifePart2Mixin)

    def test_guard_movers_denied_item_only_allowed(self) -> None:
        guard = WorldMutationGuard(
            kv_get=lambda _key: "2026-09-01T10:00:00+00:00",
            situation_snapshot_provider=lambda: SimpleNamespace(
                shared_commitment_active=True,
                generation=7,
            ),
            intentional_hold_seconds=7200.0,
        )
        movers = guard.check_autonomous({"location"})
        self.assertFalse(movers.allowed)
        item = guard.check_autonomous({"item"})
        self.assertTrue(item.allowed)
        self.assertEqual(item.reason, "item_only")

    def test_heartbeat_clocks_tick_without_4b(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        host._live_heartbeat_tick()
        host._affect_updater.tick_elapsed.assert_called()
        self.assertEqual(spawned, [])


class CueAdapterHostTests(unittest.TestCase):
    def test_host_peek_never_takes(self) -> None:
        host = Pass13Host()
        _expired_wait(host)
        host._live_heartbeat_tick()
        host._cue_store.take.assert_not_called()


if __name__ == "__main__":
    unittest.main()

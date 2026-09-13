"""Live mode Pass 2: situation frame, heartbeat, journal, epochs."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    ConversationSituationState,
    WorldSituation,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.epochs import classify_epoch
from app.core.live.impulse import LiveImpulse
from app.core.live.journal import LiveExperienceJournal
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.session_controller import SessionController


def _now() -> datetime:
    return datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _impulse(
    kind: str,
    *,
    sequence: int = 1,
    monotonic_ms: float = 1_000.0,
    payload: dict | None = None,
) -> LiveImpulse:
    return LiveImpulse(
        event_id=f"e{sequence}",
        kind=kind,
        source="test",
        session_key="s",
        mode_generation=1,
        sequence=sequence,
        occurred_at="2026-09-11T12:00:00Z",
        monotonic_ms=monotonic_ms,
        priority="attention",
        ttl_ms=30_000,
        coalesce_key=kind,
        privacy="local_state",
        payload=payload or {},
    )


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T12:00:00Z",
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


def _assemble(
    snapshot: ConversationSituationSnapshot,
    impulses: tuple[LiveImpulse, ...] = (),
    *,
    monotonic_ms: float = 10_000.0,
    generation: int = 1,
    **kwargs: object,
):
    inp = LiveAssembleInput(
        snapshot=snapshot,
        impulses=impulses,
        now=_now(),
        monotonic_ms=monotonic_ms,
        **kwargs,  # type: ignore[arg-type]
    )
    return assemble_live_situation(inp, generation=generation)


class FrameIdentityTests(unittest.TestCase):
    def test_identical_evidence_identical_frames(self) -> None:
        snap = _snapshot()
        impulses = (_impulse("user.typing_started", payload={"active": True}),)
        a = _assemble(snap, impulses)
        b = _assemble(snap, impulses)
        self.assertEqual(a.to_payload(), b.to_payload())

    def test_prior_cannot_mint_present_tense_anime(self) -> None:
        snap = _snapshot(user_active_app="Cursor")
        frame = _assemble(
            snap,
            ritual_priors=("watching anime together",),
        )
        self.assertEqual(frame.shared.inferred.label, "user_coding")
        self.assertNotEqual(frame.shared.inferred.label, "watching_anime_together")
        self.assertIn(
            "prior_cannot_override_observation",
            frame.shared.inferred.conflicts,
        )
        self.assertEqual(frame.continuity.situation_concepts, ("watching anime together",))

    def test_typing_attention_is_user_engaged(self) -> None:
        frame = _assemble(
            _snapshot(),
            (_impulse("user.typing_started", monotonic_ms=9_500.0),),
            monotonic_ms=10_000.0,
        )
        self.assertTrue(frame.interaction.typing_active)
        self.assertEqual(frame.attention.target, "user")
        self.assertEqual(frame.attention.mode, "engaged")
        self.assertEqual(frame.attention.reason_code, "user_typing")

    def test_speech_attention_and_shared_activity(self) -> None:
        speech = _assemble(
            _snapshot(),
            (
                _impulse(
                    "user.speech_final",
                    monotonic_ms=9_000.0,
                    payload={"text": "hey"},
                ),
            ),
            monotonic_ms=10_000.0,
        )
        self.assertEqual(speech.attention.target, "user")
        self.assertEqual(speech.attention.reason_code, "user_speech")
        self.assertEqual(speech.interaction.last_user_meaning, "hey")

        inferred = ConversationSituationState(
            session_id="s",
            generation=1,
            status="active",
            summary="Watching together",
            shared=True,
            shared_activity="watching a film",
            place_ref="beanbag",
            aiko_activity="watching with the user",
            evidence_message_ids=(3,),
            source_message_id=3,
            miss_count=0,
            updated_at="2026-09-11T12:00:00Z",
        )
        shared = _assemble(
            _snapshot(
                inferred=inferred,
                inferred_stale=False,
                world_compatible=True,
                shared_commitment_active=True,
            ),
        )
        self.assertEqual(shared.attention.target, "shared_activity")
        self.assertEqual(shared.shared.sharing, "shared")

    def test_sleep_forbids_speech(self) -> None:
        frame = _assemble(_snapshot(sleep={"status": "asleep"}))
        self.assertIn("sleep_asleep", frame.constraints.speech_forbid_reasons)
        self.assertEqual(frame.attention.mode, "resting")

    def test_twenty_minutes_without_a_turn_is_still_fresh(self) -> None:
        twenty_min = 20 * 60 * 1000.0
        frame = _assemble(
            _snapshot(since_user_activity_ms=int(twenty_min)),
            last_user_intent_ms=1_000.0,
            monotonic_ms=1_000.0 + twenty_min,
        )
        self.assertEqual(frame.temporal.phase, "prolonged")
        self.assertGreaterEqual(frame.temporal.since_user_intent_ms, int(twenty_min))
        self.assertEqual(frame.observed_at.startswith("2026-09-11"), True)


class EpochTests(unittest.TestCase):
    def test_typing_is_data_only_speech_is_immediate(self) -> None:
        self.assertEqual(classify_epoch("user.typing_started"), "data_only")
        self.assertEqual(classify_epoch("heartbeat"), "data_only")
        self.assertEqual(classify_epoch("user.message_sent"), "immediate")
        self.assertEqual(classify_epoch("user.speech_final"), "immediate")
        self.assertEqual(
            classify_epoch("heartbeat", situation_changed=True),
            "coalesced_transition",
        )


class JournalTests(unittest.TestCase):
    def test_reject_deictic_and_cap(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        db = ChatDatabase(Path(tmp.name) / "journal.db")
        try:
            journal = LiveExperienceJournal(db, max_rows=8, ttl_seconds=3600)
            self.assertIsNone(
                journal.append(
                    session_id="s",
                    kind="silence.wake",
                    text="quiet tonight",
                )
            )
            for i in range(12):
                journal.append(
                    session_id="s",
                    kind="silence.wake",
                    text=f"silence wake {i}",
                )
            rows = journal.tail("s", limit=20)
            self.assertEqual(len(rows), 8)
            self.assertTrue(all(row.privacy == "local_state" for row in rows))
            sent = journal.append(
                session_id="s",
                kind="user.message_sent",
                text="hello there",
            )
            self.assertIsNotNone(sent)
            assert sent is not None
            self.assertEqual(sent.privacy, "transcript")
        finally:
            conn = getattr(db._local, "conn", None)
            if conn is not None:
                conn.close()
                db._local.conn = None
            try:
                tmp.cleanup()
            except PermissionError:
                pass


class HeartbeatHost(LiveModeMixin):
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
        self._conversation_situation_worker = MagicMock()
        self._conversation_situation_worker.should_run_live.return_value = False
        self._affect_updater = MagicMock()
        self._snapshot = _snapshot()
        self._init_live_mode()

    def conversation_situation_snapshot(self, **_kwargs):
        return self._snapshot


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_refreshes_without_user_turn(self) -> None:
        host = HeartbeatHost()
        host._live_heartbeat_tick()
        frame = host.live_situation_frame()
        self.assertIsNotNone(frame)
        host._conversation_situation_worker.notify_user_turn.assert_not_called()
        host._affect_updater.tick_elapsed.assert_called()
        self.assertFalse(hasattr(host, "_live_mode_enabled"))
        self.assertFalse(host._live_heartbeat.running)

    def test_data_only_does_not_bump_generation(self) -> None:
        host = HeartbeatHost()
        first = host.refresh_live_situation(trigger_kind="heartbeat")
        second = host.refresh_live_situation(trigger_kind="user.typing_started")
        assert first is not None and second is not None
        self.assertEqual(first.generation, second.generation)
        third = host.refresh_live_situation(trigger_kind="user.message_sent")
        assert third is not None
        self.assertGreater(third.generation, second.generation)


class IdleGatePinTests(unittest.TestCase):
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

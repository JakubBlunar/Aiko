"""Live mode Pass 11: C6 as Live evidence (Phase 9)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.activity.evidence import (
    ActivityEvidence,
    evidence_from_store,
    late_night_dates_from_sessions,
)
from app.core.activity.store import ActivityStore
from app.core.brain.events import ProactiveEvent
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.epochs import classify_epoch
from app.core.live.main_wake import admit_main_wake
from app.core.live.modifiers import modifiers_from_situation
from app.core.live.notice import notices_from_trigger
from app.core.live.policy_context import situation_summary
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.urge_store import LiveUrgeStore
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.proactive_presence_mixin import ProactivePresenceMixin
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from tests.test_live_mode_pass10 import _delight_runtime


_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-13T12:00:00Z",
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


def _coding_evidence(**kwargs: object) -> ActivityEvidence:
    fields = dict(
        app="Cursor",
        source="foreground",
        duration_seconds=90,
        os_idle="active",
        stale=False,
        confidence=0.7,
        present=True,
    )
    fields.update(kwargs)
    return ActivityEvidence(**fields)  # type: ignore[arg-type]


def _frame(**kwargs: object):
    evidence = kwargs.pop("activity_evidence", _coding_evidence())
    ritual = kwargs.pop("ritual_priors", ())
    trigger = str(kwargs.pop("trigger_kind", "heartbeat") or "heartbeat")
    inp = LiveAssembleInput(
        snapshot=_snapshot(**kwargs),
        impulses=(),
        now=_NOW,
        monotonic_ms=10_000.0,
        trigger_kind=trigger,
        ritual_priors=tuple(ritual) if ritual else (),
        activity_evidence=evidence,  # type: ignore[arg-type]
    )
    return assemble_live_situation(inp, generation=1)


def _payload_keys(obj: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(str(key))
            keys.update(_payload_keys(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            keys.update(_payload_keys(item))
    return keys


def _envelope(
    *,
    source: str = "foreground",
    app: str | None = "Cursor",
    title: str | None = "secret.py — assistant",
    at: str = "2026-09-13T12:00:00+00:00",
    kind: str = "focus",
) -> dict:
    return {
        "v": 1,
        "at": at,
        "source": source,
        "tier": "cheap",
        "subject": {"app": app, "title": title, "surface_id": "abc"},
        "signal": {"kind": kind},
        "payload": {},
    }


class EvidenceHelperTests(unittest.TestCase):
    def test_store_exception_is_missing(self) -> None:
        class Boom:
            def last_event(self):
                raise RuntimeError("nope")

            def recent_sessions(self, *, limit=20):  # noqa: ARG002
                raise RuntimeError("nope")

        ev = evidence_from_store(Boom(), now=_NOW)
        self.assertFalse(ev.present)
        self.assertEqual(ev.os_idle, "missing")
        self.assertEqual(ev.confidence, 0.0)
        self.assertNotIn("title", ev.to_payload())

    def test_none_store_is_missing(self) -> None:
        ev = evidence_from_store(None, now=_NOW)
        self.assertFalse(ev.present)
        self.assertTrue(ev.stale)

    def test_never_copies_titles(self) -> None:
        class Store:
            def last_event(self):
                return {
                    "at": "2026-09-13T12:00:00+00:00",
                    "source": "foreground",
                    "app": "Cursor",
                    "title": "secret.py — assistant",
                }

            def recent_sessions(self, *, limit=20):  # noqa: ARG002
                return [{
                    "source": "foreground",
                    "app": "Cursor",
                    "title": "secret.py — assistant",
                    "started_at": "2026-09-13T11:58:00+00:00",
                    "ended_at": "2026-09-13T12:00:00+00:00",
                    "duration_seconds": 120,
                }]

        ev = evidence_from_store(Store(), now=_NOW)
        self.assertEqual(ev.app, "Cursor")
        self.assertEqual(ev.os_idle, "active")
        self.assertEqual(ev.duration_seconds, 120)
        self.assertFalse(ev.stale)
        self.assertEqual(ev.confidence, 0.7)
        self.assertNotIn("title", ev.to_payload())
        self.assertNotIn("secret", str(ev.to_payload()))


class FrameEvidenceTests(unittest.TestCase):
    def test_foreground_cursor_fills_os_idle_and_duration(self) -> None:
        frame = _frame(user_active_app="Cursor")
        self.assertEqual(frame.shared.os_idle, "active")
        self.assertGreater(frame.shared.session_duration_s, 0)
        self.assertEqual(frame.shared.inferred.label, "user_coding")
        self.assertNotIn("c6", frame.constraints.stale_sources)
        self.assertNotIn("os_idle", frame.constraints.stale_sources)
        self.assertNotIn("title", _payload_keys(frame.to_payload()))
        summary = situation_summary(frame)
        rendered = LivePolicyPromptAssembler()._render_situation(frame)
        self.assertIn("os_idle active", summary)
        self.assertIn("session_s=90", summary)
        self.assertIn("os_idle=active", rendered)
        self.assertIn("session_s=90", rendered)
        self.assertNotIn("title=", summary)
        self.assertNotIn("title=", rendered)
        self.assertNotIn("secret", summary)
        self.assertNotIn("secret", rendered)

    def test_lock_forbids_speech(self) -> None:
        ev = _coding_evidence(
            app="", source="lock", os_idle="locked", confidence=0.0,
        )
        frame = _frame(user_active_app="", activity_evidence=ev)
        self.assertEqual(frame.shared.os_idle, "locked")
        mods = modifiers_from_situation(frame)
        self.assertEqual(mods.speech_budget, "forbidden")
        notices = notices_from_trigger("activity.lock", frame)
        self.assertFalse(any(item.kind == "user_focus" for item in notices))
        self.assertFalse(any(item.kind == "focus_started" for item in notices))
        self.assertFalse(any(item.kind == "shared_commitment" for item in notices))

    def test_idle_is_not_expressive(self) -> None:
        ev = _coding_evidence(
            app="", source="idle", os_idle="idle", confidence=0.0,
        )
        frame = _frame(user_active_app="", activity_evidence=ev)
        self.assertEqual(frame.shared.os_idle, "idle")
        notices = notices_from_trigger("activity.idle", frame)
        self.assertEqual(notices, ())
        mods = modifiers_from_situation(frame)
        self.assertNotEqual(mods.speech_budget, "forbidden")
        self.assertNotEqual(mods.reason_code, "world_truth_coding")

    def test_stale_c6_does_not_open_coding_speech(self) -> None:
        ev = _coding_evidence(stale=True, confidence=0.0)
        frame = _frame(user_active_app="Cursor", activity_evidence=ev)
        self.assertIn("c6", frame.constraints.stale_sources)
        self.assertEqual(frame.shared.inferred.confidence, 0.0)
        mods = modifiers_from_situation(frame)
        self.assertNotEqual(mods.reason_code, "world_truth_coding")
        notices = notices_from_trigger("activity.session_changed", frame)
        self.assertFalse(any(item.kind == "user_focus" for item in notices))
        self.assertFalse(any(item.kind == "focus_started" for item in notices))

    def test_disabled_or_missing_c6_does_not_stall(self) -> None:
        frame = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(user_active_app="Cursor"),
                impulses=(),
                now=_NOW,
                monotonic_ms=10_000.0,
                activity_evidence=None,
            ),
            generation=1,
        )
        self.assertEqual(frame.shared.os_idle, "missing")
        self.assertEqual(frame.shared.inferred.label, "user_coding")
        mods = modifiers_from_situation(frame)
        self.assertEqual(mods.speech_budget, "rare")
        notices = notices_from_trigger("heartbeat", frame)
        self.assertEqual(notices, ())

    def test_coding_plus_anime_prior_still_conflicts(self) -> None:
        frame = _frame(
            user_active_app="Cursor",
            ritual_priors=("watching anime together",),
        )
        self.assertEqual(frame.shared.inferred.label, "user_coding")
        self.assertIn(
            "prior_cannot_override_observation",
            frame.shared.inferred.conflicts,
        )
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

    def test_weak_chrome_is_not_media_sharing(self) -> None:
        ev = _coding_evidence(
            app="Chrome",
            confidence=0.35,
            duration_seconds=12,
        )
        frame = _frame(user_active_app="Chrome", activity_evidence=ev)
        self.assertEqual(frame.shared.inferred.label, "user_using_app")
        self.assertEqual(frame.shared.sharing, "user_only")
        notices = notices_from_trigger("activity.session_changed", frame)
        self.assertFalse(any(item.kind == "user_focus" for item in notices))
        self.assertFalse(any(item.kind == "focus_started" for item in notices))


class EpochTests(unittest.TestCase):
    def test_activity_edges_are_coalesced_heartbeat_is_not(self) -> None:
        self.assertEqual(
            classify_epoch("activity.session_changed"),
            "coalesced_transition",
        )
        self.assertEqual(classify_epoch("activity.idle"), "coalesced_transition")
        self.assertEqual(classify_epoch("activity.lock"), "coalesced_transition")
        self.assertEqual(classify_epoch("heartbeat"), "data_only")


class IngestImpulseTests(unittest.TestCase):
    def test_foreground_publishes_without_title_or_idle_touch(self) -> None:
        controller = _ingest_controller()
        controller.ingest_activity_envelope(_envelope())
        self.assertEqual(controller._user_active_app, "Cursor")
        self.assertEqual(controller._last_user_activity_at, 0.0)
        tail = controller._live_impulse_bus.snapshot()["tail"]
        self.assertEqual(tail[-1]["kind"], "activity.session_changed")
        self.assertEqual(tail[-1]["payload"]["app"], "Cursor")
        self.assertNotIn("title", tail[-1]["payload"])

    def test_lock_clears_app_and_publishes(self) -> None:
        controller = _ingest_controller()
        controller.set_user_active_app("Cursor")
        controller.ingest_activity_envelope(
            _envelope(source="lock", app=None, title=None, kind="lock"),
        )
        self.assertIsNone(controller._user_active_app)
        self.assertEqual(
            controller._live_impulse_bus.snapshot()["tail"][-1]["kind"],
            "activity.lock",
        )

    def test_real_store_duration_and_stale(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            db = ChatDatabase(Path(tmp.name) / "chat.db")
            store = ActivityStore(db)
            controller = _ingest_controller(store=store)
            controller.ingest_activity_envelope(
                _envelope(at="2026-09-13T12:00:00+00:00"),
            )
            controller.ingest_activity_envelope(
                _envelope(at="2026-09-13T12:00:05+00:00"),
            )
            ev = evidence_from_store(
                store, now=datetime(2026, 9, 13, 12, 0, 10, tzinfo=timezone.utc),
            )
            self.assertTrue(ev.present)
            self.assertFalse(ev.stale)
            self.assertGreater(ev.duration_seconds, 0)
            self.assertEqual(ev.os_idle, "active")
            stale = evidence_from_store(
                store, now=datetime(2026, 9, 13, 12, 5, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(stale.stale)
            self.assertEqual(stale.confidence, 0.0)
        finally:
            tmp.cleanup()


class K72SessionDatesTests(unittest.TestCase):
    def test_small_hours_foreground_session_counts(self) -> None:
        local = datetime.now().astimezone()
        night = local.replace(hour=3, minute=0, second=0, microsecond=0)
        sessions = [{
            "source": "foreground",
            "app": "Cursor",
            "title": "secret.py",
            "started_at": night.isoformat(),
            "ended_at": night.replace(hour=4).isoformat(),
            "duration_seconds": 3600,
        }]
        dates = late_night_dates_from_sessions(
            sessions,
            now=local,
            window_days=7,
            start_hour=1,
            end_hour=5,
        )
        self.assertEqual(dates, [night.date().isoformat()])

    def test_daytime_session_does_not_count(self) -> None:
        local = datetime.now().astimezone()
        day = local.replace(hour=14, minute=0, second=0, microsecond=0)
        dates = late_night_dates_from_sessions(
            [{
                "source": "foreground",
                "app": "Cursor",
                "started_at": day.isoformat(),
                "ended_at": (day + timedelta(hours=2)).isoformat(),
                "duration_seconds": 7200,
            }],
            now=local,
            window_days=7,
            start_hour=1,
            end_hour=5,
        )
        self.assertEqual(dates, [])


class MainWakeAndSilencePins(unittest.TestCase):
    def test_lock_still_rejects_main_wake(self) -> None:
        runtime, urge_id = _delight_runtime()
        ev = _coding_evidence(
            app="", source="lock", os_idle="locked", confidence=0.0,
        )
        frame = _frame(user_active_app="", activity_evidence=ev)
        mods = modifiers_from_situation(frame)
        from dataclasses import replace
        gated = replace(
            frame,
            constraints=replace(frame.constraints, speech_budget=mods.speech_budget),
        )
        self.assertEqual(
            admit_main_wake(
                intent="request_main_speech",
                user_intent=False,
                selected_urge_id=urge_id,
                urges=runtime.urges.active(),
                frame=gated,
                decided_generation=None,
                now_mono_ms=10_000.0,
                budget_remaining=1,
            ),
            "speech_forbidden",
        )

    def test_silence_sources_stay_starved_under_live(self) -> None:
        class _Host(TaskOrchestrationMixin):
            def __init__(self) -> None:
                self._proactive = MagicMock()
                self._live_voice_session_active = False
                self.ran: list[object] = []

            def is_live_presence(self) -> bool:
                return True

            def publish_live_impulse(self, **_kwargs: object) -> None:
                return None

            def _run_live_main_wake(self, event: object) -> None:
                self.ran.append(event)

        host = _Host()
        host._on_task_proactive_event(
            ProactiveEvent(session_key="s", source="voice_silence"),
        )
        host._on_task_proactive_event(
            ProactiveEvent(session_key="s", source="typed_silence"),
        )
        host._proactive.notify_silence.assert_not_called()
        host._proactive.notify_typed_silence.assert_not_called()
        self.assertEqual(host.ran, [])


class MixinEvidenceTests(unittest.TestCase):
    def test_disabled_c6_skips_store(self) -> None:
        host = _LiveHost()
        host._settings.agent.activity_awareness_enabled = False
        host._activity_store = SimpleNamespace(
            last_event=lambda: (_ for _ in ()).throw(RuntimeError("no")),
        )
        self.assertIsNone(host._live_activity_evidence())


def _ingest_controller(*, store=None):
    host = _IngestHost(store=store)
    return host


class _IngestHost(LiveModeMixin, ProactivePresenceMixin):
    def __init__(self, *, store=None) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                activity_awareness_enabled=True,
                activity_title_allowlist=["Cursor", "Code"],
                behavior_posture="turn_based",
                live_quiet=False,
                live_impulse_bus_enabled=True,
            ),
            assistant=SimpleNamespace(user_display_name="Jacob"),
        )
        self._user_active_app = None
        self._activity_store = store
        self._last_user_activity_at = 0.0
        self._chat_db = None
        self._turn_in_progress = False
        self._live_voice_session_active = False
        self.session_key = "u:s"
        self._user_id = "default"
        self._init_live_mode()

    def conversation_situation_snapshot(self, **_kwargs: object):
        return _snapshot(user_active_app=self._user_active_app or "")


class _LiveHost(LiveModeMixin):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                behavior_posture="live_presence",
                live_quiet=False,
                live_impulse_bus_enabled=True,
                activity_awareness_enabled=True,
            ),
        )
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self.session_key = "u:s"
        self._user_id = "default"
        self._chat_db = None
        self._activity_store = None
        self._init_live_mode()


if __name__ == "__main__":
    unittest.main()

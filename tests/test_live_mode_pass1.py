"""Live mode Pass 1: speech-floor starve, impulse bus, dual-write."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.brain.events import ProactiveEvent
from app.core.live.bus import LiveImpulseBus
from app.core.live.impulse import FORBIDDEN_PAYLOAD_KEYS, new_impulse
from app.core.session.live_mode_mixin import (
    BEHAVIOR_POSTURE_LIVE,
    LiveModeMixin,
    normalize_behavior_posture,
)
from app.core.session.live_session import LiveSession
from app.core.session.session_controller import SessionController


class NormalizePostureTests(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(normalize_behavior_posture("live_presence"), "live_presence")
        self.assertEqual(normalize_behavior_posture("turn_based"), "turn_based")

    def test_garbage_falls_back(self) -> None:
        self.assertEqual(normalize_behavior_posture("blab"), "turn_based")
        self.assertEqual(normalize_behavior_posture(None), "turn_based")


class ImpulseBusTests(unittest.TestCase):
    def test_coalesce_replaces_same_key(self) -> None:
        bus = LiveImpulseBus()
        bus.publish(
            kind="user.typing_started",
            source="web.chat",
            session_key="s",
            mode_generation=1,
            coalesce_key="user.typing.chat",
            payload={"active": True, "draft": "secret"},
        )
        bus.publish(
            kind="user.typing_started",
            source="web.chat",
            session_key="s",
            mode_generation=1,
            coalesce_key="user.typing.chat",
            payload={"active": True},
        )
        snap = bus.snapshot()
        self.assertEqual(snap["pending"], 1)
        self.assertEqual(snap["coalesced"], 1)
        self.assertEqual(snap["capture_available"], False)
        payload = snap["tail"][-1]["payload"]
        self.assertNotIn("draft", payload)

    def test_forbidden_payload_keys_stripped(self) -> None:
        impulse = new_impulse(
            kind="user.message_sent",
            source="session.turn",
            session_key="s",
            mode_generation=0,
            sequence=1,
            coalesce_key="user.message_sent",
            privacy="transcript",
            payload={
                "text": "hello",
                "draft": "nope",
                "title": "secret window",
                "audio": b"no",
            },
        )
        for key in FORBIDDEN_PAYLOAD_KEYS:
            self.assertNotIn(key, impulse.payload)
        self.assertEqual(impulse.payload["text"], "hello")

    def test_generation_drop(self) -> None:
        bus = LiveImpulseBus()
        bus.publish(
            kind="user.typing_started",
            source="web.chat",
            session_key="s",
            mode_generation=1,
            coalesce_key="a",
        )
        dropped = bus.drop_stale_generation(2)
        self.assertEqual(dropped, 1)
        self.assertEqual(bus.snapshot()["pending"], 0)


class LiveModeMixinHost(LiveModeMixin):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                behavior_posture="turn_based",
                live_quiet=False,
                live_unprompted_speech=True,
                live_main_wake_max_per_hour=6,
                live_min_gap_after_speech_ms=8000,
                live_mic_consented=False,
                live_impulse_bus_enabled=False,
            ),
        )
        self._live_voice_session_active = False
        self.session_key = "u:s"
        self._init_live_mode()
        self.enqueued: list[ProactiveEvent] = []

    @property
    def _brain_loop(self) -> SimpleNamespace:
        return SimpleNamespace(enqueue=self.enqueued.append)


class LiveModeMixinTests(unittest.TestCase):
    def test_bus_off_until_live_or_flag(self) -> None:
        host = LiveModeMixinHost()
        host.publish_live_user_meaning("hello", mode="typed")
        self.assertEqual(host._live_impulse_bus.snapshot()["accepted"], 0)
        host._settings.agent.behavior_posture = BEHAVIOR_POSTURE_LIVE
        host.publish_live_user_meaning("hello", mode="typed")
        snap = host._live_impulse_bus.snapshot()
        self.assertEqual(snap["accepted"], 1)
        self.assertEqual(snap["tail"][-1]["kind"], "user.message_sent")
        self.assertEqual(snap["tail"][-1]["payload"]["text"], "hello")

    def test_voice_meaning_kind(self) -> None:
        host = LiveModeMixinHost()
        host._settings.agent.live_impulse_bus_enabled = True
        host.publish_live_user_meaning("spoken", mode="voice")
        self.assertEqual(
            host._live_impulse_bus.snapshot()["tail"][-1]["kind"],
            "user.speech_final",
        )

    def test_enqueue_silence(self) -> None:
        host = LiveModeMixinHost()
        host._enqueue_silence_proactive("typed_silence")
        self.assertEqual(len(host.enqueued), 1)
        self.assertEqual(host.enqueued[0].source, "typed_silence")

    def test_capture_available_tracks_voice_session(self) -> None:
        host = LiveModeMixinHost()
        self.assertFalse(host.live_capture_available())
        self.assertFalse(host.live_impulse_diagnostics()["capture_available"])
        host._live_voice_session_active = True
        self.assertTrue(host.live_capture_available())
        self.assertTrue(host.live_impulse_diagnostics()["capture_available"])


class IdleGatePinTests(unittest.TestCase):
    def test_voice_session_not_idle_and_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))


class LiveSessionProactiveTests(unittest.TestCase):
    def test_maybe_proactive_enqueues_not_director(self) -> None:
        session = MagicMock()
        session.is_tts_playing.return_value = False
        session._settings.agent.proactive_silence_seconds = 0.0
        live = LiveSession(session, on_event=lambda *_a, **_k: None)
        live._last_activity_monotonic = 0.0
        live._maybe_proactive()
        session.generate_proactive_message.assert_called_once()
        session._proactive.notify_silence.assert_not_called()


class PersistPostureTests(unittest.TestCase):
    def test_set_behavior_posture_persists_and_bumps_generation(self) -> None:
        host = LiveModeMixinHost()
        with patch(
            "app.core.session.live_mode_mixin.persist_user_overrides",
        ) as persist:
            host.set_behavior_posture("live_presence")
        persist.assert_called_once()
        self.assertEqual(host._settings.agent.behavior_posture, "live_presence")
        self.assertEqual(host._live_mode_generation, 1)


if __name__ == "__main__":
    unittest.main()

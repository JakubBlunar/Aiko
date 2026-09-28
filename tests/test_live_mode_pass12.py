"""Live mode Pass 12: Phase 10 hardening (cadence, ownership, logs)."""
from __future__ import annotations

import logging
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.brain.events import ProactiveEvent
from app.core.live.budget import LiveBehaviorBudget
from app.core.live.diagnostics import sanitize_live_dump
from app.core.live.main_wake import admit_main_wake
from app.core.live.modifiers import modifiers_from_situation
from app.core.session.live_mode_mixin import BEHAVIOR_POSTURE_LIVE
from app.core.session.proactive_presence_mixin import ProactivePresenceMixin
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.web.ws_live_commands import handle_live_ws_command
from tests.test_live_mode_pass1 import LiveModeMixinHost
from tests.test_live_mode_pass7 import _controller, _FakePolicyClient, _frame
from tests.test_live_mode_pass10 import _delight_runtime


class CadenceKnobTests(unittest.TestCase):
    def test_setters_persist_and_clamp(self) -> None:
        host = LiveModeMixinHost()
        with patch(
            "app.core.session.live_mode_mixin.persist_user_overrides",
        ) as persist:
            host.set_live_unprompted_speech(False)
            host.set_live_main_wake_max_per_hour(99)
            host.set_live_min_gap_after_speech_ms(-5)
            host.set_live_mic_consented(True)
        self.assertFalse(host._settings.agent.live_unprompted_speech)
        self.assertEqual(host._settings.agent.live_main_wake_max_per_hour, 30)
        self.assertEqual(host._settings.agent.live_min_gap_after_speech_ms, 0)
        self.assertTrue(host._settings.agent.live_mic_consented)
        self.assertGreaterEqual(persist.call_count, 4)

    def test_main_wake_budget_follows_hourly_cap(self) -> None:
        budget = LiveBehaviorBudget()
        budget.configure_main_wake(6)
        now = time.monotonic() * 1000.0
        self.assertEqual(budget.remaining("main_wake", now_mono_ms=now), 1)
        self.assertTrue(budget.consume("main_wake", now_mono_ms=now))
        self.assertEqual(budget.remaining("main_wake", now_mono_ms=now), 0)
        budget.refund("main_wake", now_mono_ms=now)
        budget.refund("main_wake", now_mono_ms=now)
        self.assertEqual(budget.remaining("main_wake", now_mono_ms=now), 1)
        budget.configure_main_wake(0)
        self.assertEqual(budget.remaining("main_wake", now_mono_ms=now), 0)
        budget.refund("main_wake", now_mono_ms=now)
        self.assertEqual(budget.remaining("main_wake", now_mono_ms=now), 0)
        self.assertFalse(budget.consume("main_wake", now_mono_ms=now))

    def test_min_gap_floors_modifiers(self) -> None:
        mods = modifiers_from_situation(
            _frame(), live_quiet=False, min_gap_after_speech_ms=8000,
        )
        self.assertGreaterEqual(mods.min_gap_after_speech_ms, 8000)


class UnpromptedSpeechGateTests(unittest.TestCase):
    def test_admit_and_micro_noop_when_unprompted_off(self) -> None:
        runtime, urge_id = _delight_runtime()
        frame = _frame()
        reason = admit_main_wake(
            intent="request_main_speech",
            user_intent=False,
            selected_urge_id=urge_id,
            urges=runtime.urges.active(),
            frame=frame,
            decided_generation=None,
            now_mono_ms=time.monotonic() * 1000.0,
            budget_remaining=1,
            unprompted_speech=False,
        )
        self.assertEqual(reason, "speech_forbidden")

        host = LiveModeMixinHost()
        host._settings.agent.live_unprompted_speech = False
        host._turn_in_progress = False
        host.is_playback_drained = lambda: True  # type: ignore[method-assign]
        host._chat_db = MagicMock()
        ok = host._deliver_live_micro_utterance(
            "oh, that was beautiful",
            SimpleNamespace(reason_code="x"),
            frame,
        )
        self.assertFalse(ok)
        host._chat_db.add_message.assert_not_called()


class ImpulseOwnerTests(unittest.TestCase):
    def test_non_owner_composing_is_dropped(self) -> None:
        session = MagicMock()
        hub = SimpleNamespace(voice_owner_id="owner", audio_owner_id="owner")
        consumed = handle_live_ws_command(
            session,
            "composing",
            {"active": True, "surface": "chat"},
            client_id="other",
            hub=hub,
        )
        self.assertTrue(consumed)
        session.publish_live_impulse.assert_not_called()

    def test_owner_composing_publishes(self) -> None:
        session = MagicMock()
        hub = SimpleNamespace(voice_owner_id=None, audio_owner_id="owner")
        consumed = handle_live_ws_command(
            session,
            "composing",
            {"active": True, "surface": "chat"},
            client_id="owner",
            hub=hub,
        )
        self.assertTrue(consumed)
        session.publish_live_impulse.assert_called_once()


class ReconnectGenerationTests(unittest.TestCase):
    def test_first_client_bumps_reconnect(self) -> None:
        class _Host(ProactivePresenceMixin):
            def __init__(self) -> None:
                self._connected_clients = 0
                self.reasons: list[str] = []

            def bump_live_mode_generation(self, reason: str = "") -> int:
                self.reasons.append(reason)
                return 1

        host = _Host()
        host.set_connected_clients(1)
        self.assertEqual(host.reasons, ["reconnect"])
        host.set_connected_clients(2)
        self.assertEqual(host.reasons, ["reconnect"])
        host.set_connected_clients(0)
        host.set_connected_clients(1)
        self.assertEqual(host.reasons, ["reconnect", "reconnect"])

    def test_bump_cancels_inflight_controller(self) -> None:
        host = LiveModeMixinHost()
        controller = host._live_policy_controller
        controller._cancel.clear()
        gen = host.bump_live_mode_generation("reconnect")
        self.assertEqual(gen, 1)
        self.assertTrue(controller._cancel.is_set())


class SanitizeDumpTests(unittest.TestCase):
    def test_cursor_title_never_appears(self) -> None:
        dump = sanitize_live_dump({
            "last_policy_proposal": {
                "proposal": {"intent": "wait", "reason_code": "idle"},
                "main_wake_payload": {
                    "situation_summary": "Cursor — secret.md",
                    "text": "hello there",
                },
            },
            "journal": [{"kind": "user.message_sent", "text": "secret chat"}],
            "tail": [{
                "kind": "activity.session_changed",
                "payload": {
                    "app": "Cursor",
                    "title": "Cursor — secret.md",
                    "text": "draft",
                },
            }],
        })
        blob = str(dump)
        self.assertNotIn("secret.md", blob)
        self.assertNotIn("hello there", blob)
        self.assertNotIn("secret chat", blob)
        self.assertEqual(dump["tail"][0]["payload"]["app"], "Cursor")
        self.assertEqual(
            dump["last_policy_proposal"]["proposal"]["intent"], "wait",
        )

    def test_host_diagnostics_strip_transcript(self) -> None:
        host = LiveModeMixinHost()
        host._settings.agent.behavior_posture = BEHAVIOR_POSTURE_LIVE
        host.publish_live_user_meaning("private utterance", mode="typed")
        dump = host.live_impulse_diagnostics()
        blob = str(dump)
        self.assertNotIn("private utterance", blob)


class LiveLoggingTests(unittest.TestCase):
    def test_posture_and_coalesced_impulse_are_info(self) -> None:
        host = LiveModeMixinHost()
        with self.assertLogs("app.live", level="INFO") as captured:
            with patch(
                "app.core.session.live_mode_mixin.persist_user_overrides",
            ):
                host.set_behavior_posture("live_presence")
            host.publish_live_impulse(
                kind="silence.wake",
                source="test",
                coalesce_key="silence.wake",
                payload={"source": "typed_silence"},
            )
        text = "\n".join(captured.output)
        self.assertIn("live posture:", text)
        self.assertIn("live impulse: kind=silence.wake", text)

    def test_heartbeat_does_not_info_consider(self) -> None:
        host = LiveModeMixinHost()
        with patch(
            "app.core.session.live_mode_mixin.persist_user_overrides",
        ):
            host.set_behavior_posture("live_presence")
        live = logging.getLogger("app.live")
        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Handler()
        live.addHandler(handler)
        try:
            host.refresh_live_situation(trigger_kind="heartbeat")
        finally:
            live.removeHandler(handler)
        messages = [record.getMessage() for record in records]
        self.assertFalse(any(msg.startswith("live consider:") for msg in messages))
        self.assertFalse(any(msg.startswith("live impulse:") for msg in messages))


class WorkerMatrixPinTests(unittest.TestCase):
    def test_live_still_starves_proactive_director(self) -> None:
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
        host._on_task_proactive_event(
            ProactiveEvent(session_key="s", source="typed_silence"),
        )
        host._proactive.notify_silence.assert_not_called()
        host._proactive.notify_typed_silence.assert_not_called()
        self.assertEqual(host.published, ["silence.wake", "silence.wake"])


class QuietUnpromptedControllerTests(unittest.TestCase):
    def test_unprompted_off_rejects_main_wake_execute(self) -> None:
        runtime, urge_id = _delight_runtime()
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": urge_id,
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "share_the_scene",
            "context_refs": [],
        })
        frame = _frame()
        enqueued: list[dict] = []
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_main_wake=lambda payload: enqueued.append(payload) or True,
            unprompted_speech_provider=lambda: False,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": (urge_id,)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertEqual(
            controller.last_proposal.get("main_wake_rejected"),
            "speech_forbidden",
        )
        self.assertEqual(enqueued, [])


if __name__ == "__main__":
    unittest.main()

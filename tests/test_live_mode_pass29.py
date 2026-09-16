"""Live mode Pass 29: wait horizon and wake-set (L8)."""
from __future__ import annotations

import threading
import unittest

from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.proposal import LIVE_POLICY_JSON_SCHEMA, LivePolicyProposal, sanitize_arguments
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.wait import (
    CRITICAL_WAKES,
    DEFAULT_WAIT_MS,
    MAX_WAIT_MS,
    MIN_WAIT_MS,
    LiveWaitScheduler,
    clamp_wait_horizon,
    clamp_wake_set,
    expand_wake_set,
    wait_ms_for_horizon,
)
from tests.test_live_mode_pass7 import _FakePolicyClient, _controller, _frame
from tests.test_live_mode_pass13 import Pass13Host, _capture_spawns, _expired_wait


class HorizonAndWakeSetTests(unittest.TestCase):
    def test_bands_map_onto_wait_range(self) -> None:
        self.assertEqual(wait_ms_for_horizon("short"), MIN_WAIT_MS)
        self.assertEqual(wait_ms_for_horizon("medium"), DEFAULT_WAIT_MS)
        self.assertEqual(wait_ms_for_horizon("long"), MAX_WAIT_MS)
        self.assertLess(wait_ms_for_horizon("short"), wait_ms_for_horizon("medium"))
        self.assertLess(wait_ms_for_horizon("medium"), wait_ms_for_horizon("long"))
        self.assertEqual(clamp_wait_horizon("explode"), "medium")
        self.assertEqual(clamp_wake_set("nope"), "user_only")

    def test_unknown_wake_names_drop(self) -> None:
        events = expand_wake_set("user_only")
        self.assertTrue(CRITICAL_WAKES.issubset(events))
        self.assertNotIn("invented.event", events)
        self.assertNotIn("silence.wake", events)
        silence = expand_wake_set("user_or_silence")
        self.assertIn("silence.wake", silence)
        activity = expand_wake_set("user_or_activity")
        self.assertIn("activity.session_changed", activity)
        self.assertNotIn("silence.wake", activity)

    def test_sleep_cannot_pick_a_speech_wake(self) -> None:
        asleep = expand_wake_set("user_or_silence", sleep_status="asleep")
        self.assertNotIn("silence.wake", asleep)
        self.assertNotIn("idle.reconsider", asleep)
        self.assertIn("user.message_sent", asleep)
        awake = expand_wake_set("user_or_silence", sleep_status="awake")
        self.assertIn("silence.wake", awake)

    def test_critical_user_wakes_even_if_omitted(self) -> None:
        sched = LiveWaitScheduler()
        sched.schedule(
            generation=1,
            urge_id="",
            reconsider_after_ms=30_000,
            now_mono_ms=10_000.0,
            wake_on=expand_wake_set("user_only"),
            reason_code="test",
        )
        self.assertNotIn("silence.wake", sched.current.wake_on)
        self.assertFalse(
            sched.should_suppress_inference(
                "user.message_sent", now_mono_ms=11_000.0, generation=1,
            )
        )


class ProposalIgnoreRawMsTests(unittest.TestCase):
    def test_schema_has_bands_not_raw_ms(self) -> None:
        props = LIVE_POLICY_JSON_SCHEMA["properties"]
        self.assertNotIn("reconsider_after_ms", props)
        self.assertNotIn("wake_on", props)
        args = props["arguments"]["properties"]
        self.assertEqual(args["wait_horizon"]["enum"], ["short", "medium", "long"])
        self.assertEqual(
            args["wake_set"]["enum"],
            ["user_only", "user_or_activity", "user_or_silence"],
        )

    def test_raw_ms_and_invented_wakes_are_ignored(self) -> None:
        parsed = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {
                "wait_horizon": "short",
                "wake_set": "user_or_silence",
                "title": "secret.md",
            },
            "reason_code": "idle",
            "context_refs": [],
            "reconsider_after_ms": 999_000,
            "wake_on": ["invented.event", "silence.wake"],
        })
        self.assertIsNone(parsed.reconsider_after_ms)
        self.assertEqual(parsed.wake_on, ())
        self.assertEqual(parsed.arguments["wait_horizon"], "short")
        self.assertEqual(parsed.arguments["wake_set"], "user_or_silence")
        cleaned = sanitize_arguments(
            "wait",
            {"wait_horizon": "long", "wake_set": "explode", "title": "nope"},
        )
        self.assertEqual(cleaned, {"wait_horizon": "long"})


class ControllerWaitTests(unittest.TestCase):
    def test_horizon_schedules_clamped_wait(self) -> None:
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {
                "wait_horizon": "short",
                "wake_set": "user_or_silence",
            },
            "reason_code": "idle",
            "context_refs": [],
            "reconsider_after_ms": 999_000,
            "wake_on": ["invented.event"],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        wait = runtime.wait.current
        assert wait is not None
        self.assertEqual(wait.reconsider_after_ms, MIN_WAIT_MS)
        self.assertIn("silence.wake", wait.wake_on)
        self.assertNotIn("invented.event", wait.wake_on)
        self.assertEqual(
            controller.last_proposal.get("wait_horizon"), "short",
        )
        self.assertEqual(
            controller.last_proposal.get("wake_set"), "user_or_silence",
        )

    def test_sleep_wait_drops_silence_wake(self) -> None:
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {"wake_set": "user_or_silence", "wait_horizon": "long"},
            "reason_code": "sleep",
            "context_refs": [],
        })
        frame = _frame(sleep={"status": "asleep"})
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        wait = runtime.wait.current
        assert wait is not None
        self.assertEqual(wait.reconsider_after_ms, MAX_WAIT_MS)
        self.assertNotIn("silence.wake", wait.wake_on)
        self.assertIn("user.message_sent", wait.wake_on)


class ExpiryAndPromptTests(unittest.TestCase):
    def test_expiry_still_promotes_once(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        _expired_wait(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, ["idle.reconsider"])
        _expired_wait(host)
        host._live_heartbeat_tick()
        self.assertEqual(spawned, ["idle.reconsider"])

    def test_quiet_sleep_turn_tts_still_skip_reconsider(self) -> None:
        quiet = Pass13Host()
        quiet._settings.agent.live_quiet = True
        spawned = _capture_spawns(quiet)
        _expired_wait(quiet)
        quiet._live_heartbeat_tick()
        self.assertEqual(spawned, [])
        asleep = Pass13Host(sleep={"status": "asleep"})
        spawned = _capture_spawns(asleep)
        _expired_wait(asleep)
        asleep._live_heartbeat_tick()
        self.assertEqual(spawned, [])

    def test_prompt_names_horizon_not_raw_ms(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            context_window=4096,
            max_tokens=64,
            prompt_ceiling=4000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("wait_horizon", text)
        self.assertIn("wake_set", text)
        self.assertIn("reconsider_after_ms", text)
        self.assertIn("wake_on event", text)


if __name__ == "__main__":
    unittest.main()

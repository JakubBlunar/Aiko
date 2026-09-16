"""Live mode Pass 20: action IDs, result impulses, bounded arguments."""
from __future__ import annotations

import threading
import unittest

from app.core.live.actions import (
    ACTION_RESULT_KINDS,
    action_result_kind,
    start_action,
)
from app.core.live.arbiter import LiveArbiterResult, arbitrate_live_proposal
from app.core.live.epochs import classify_epoch
from app.core.live.proposal import LivePolicyProposal, sanitize_arguments
from tests.test_live_mode_pass7 import (
    _FakePolicyClient,
    _Host,
    _controller,
    _frame,
    _proposal,
)


class ArgumentSanitizeTests(unittest.TestCase):
    def test_unknown_keys_and_titles_are_dropped(self) -> None:
        cleaned = sanitize_arguments(
            "wait",
            {
                "reasoning": "nothing new",
                "note": "ok",
                "title": "secret.md",
                "text": "should not keep",
            },
        )
        self.assertEqual(cleaned, {"reasoning": "nothing new"})
        parsed = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {"note": "ok", "title": "secret.md"},
            "reason_code": "shared",
            "context_refs": [],
        })
        self.assertEqual(parsed.arguments, {})

    def test_micro_keeps_delivery_and_text(self) -> None:
        cleaned = sanitize_arguments(
            "react_affectively",
            {
                "delivery": "micro_utterance",
                "text": "oh, that was beautiful",
                "title": "secret.md",
            },
        )
        self.assertEqual(
            cleaned,
            {
                "delivery": "micro_utterance",
                "text": "oh, that was beautiful",
            },
        )


class ActionRecordTests(unittest.TestCase):
    def test_wait_emits_started_then_completed(self) -> None:
        seen: list[str] = []
        records: list[object] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {"reasoning": "nothing new", "title": "nope"},
            "reason_code": "idle",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            on_action_result=lambda rec: (
                seen.append(action_result_kind(rec.state)),
                records.append(rec),
            ),
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
        self.assertTrue(result.accepted)
        self.assertEqual(seen, ["aiko.action_started", "aiko.action_completed"])
        self.assertIn(seen[0], ACTION_RESULT_KINDS)
        first = records[0]
        last = records[-1]
        self.assertEqual(first.action_id, last.action_id)
        self.assertEqual(last.state, "completed")
        self.assertNotIn("title", last.to_impulse_payload())
        self.assertNotIn("text", last.to_impulse_payload())
        extra = controller.last_proposal
        self.assertEqual(extra.get("action_id"), last.action_id)
        self.assertEqual(extra.get("action_state"), "completed")

    def test_not_allowed_emits_rejected_only(self) -> None:
        seen: list[str] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "react_affectively",
            "arguments": {
                "text": "I'm still here",
                "delivery": "micro_utterance",
            },
            "reason_code": "here",
            "context_refs": [],
        })
        frame = _frame(sleep={"status": "asleep"})
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            on_action_result=lambda rec: seen.append(
                action_result_kind(rec.state),
            ),
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
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "not_allowed")
        self.assertEqual(seen, ["aiko.action_rejected"])

    def test_stale_generation_cancels_parsed_action(self) -> None:
        controller = _controller()
        proposal = _proposal(intent="wait")
        controller._current_action = start_action(
            intent="wait", generation=1, reason="idle",
        )
        record = controller._finish_action(
            LiveArbiterResult(True, "ok", proposal),
            executed=False,
            extra={},
            cancelled=True,
        )
        assert record is not None
        self.assertEqual(record.state, "cancelled")
        self.assertEqual(action_result_kind(record.state), "aiko.action_cancelled")

    def test_shadow_main_wake_does_not_emit(self) -> None:
        seen: list[str] = []
        result = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
        )
        controller = _controller(
            on_action_result=lambda rec: seen.append(rec.state),
        )
        controller._current_action = start_action(
            intent="request_main_speech", generation=1,
        )
        controller._finish_action(
            result, executed=False, extra={}, cancelled=False,
        )
        self.assertEqual(seen, [])
        self.assertIsNone(controller.last_action)


class MixinImpulseTests(unittest.TestCase):
    def test_action_results_do_not_refresh(self) -> None:
        host = _Host()
        host._settings.agent.live_impulse_bus_enabled = True
        refreshes: list[str] = []
        original = host.refresh_live_situation

        def _wrapped(*, trigger_kind: str = "heartbeat", **kwargs: object):
            refreshes.append(str(trigger_kind))
            return original(trigger_kind=trigger_kind, **kwargs)  # type: ignore[arg-type]

        host.refresh_live_situation = _wrapped  # type: ignore[method-assign]
        frame = _frame()
        started = int(host._live_mode_generation or 0)
        client = _FakePolicyClient({
            "snapshot_generation": int(frame.generation),
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {},
            "reason_code": "idle",
            "context_refs": [],
        })
        host._live_policy_controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=started,
            cancel=threading.Event(),
            client=client,
        )
        kinds = [
            item["kind"]
            for item in host._live_impulse_bus.snapshot()["tail"]
        ]
        self.assertIn("aiko.action_started", kinds)
        self.assertIn("aiko.action_completed", kinds)
        self.assertNotIn("aiko.action_started", refreshes)
        self.assertNotIn("aiko.action_completed", refreshes)
        payloads = host._live_impulse_bus.snapshot()["tail"]
        for item in payloads:
            if str(item["kind"]).startswith("aiko.action_"):
                self.assertNotIn("title", item["payload"])
                self.assertNotIn("text", item["payload"])
                self.assertIn("action_id", item["payload"])

    def test_ledger_stores_action_id(self) -> None:
        host = _Host()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        started = int(host._live_mode_generation or 0)
        client = _FakePolicyClient({
            "snapshot_generation": int(frame.generation),
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {},
            "reason_code": "idle",
            "context_refs": [],
        })
        host._live_policy_controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=started,
            cancel=threading.Event(),
            client=client,
        )
        last = host._live_policy_context._ledger[-1]
        self.assertTrue(last.action_id)
        self.assertEqual(last.action_state, "completed")
        self.assertEqual(last.completed_action, "wait")

    def test_action_results_are_data_only(self) -> None:
        self.assertEqual(classify_epoch("aiko.action_started"), "data_only")
        self.assertEqual(classify_epoch("aiko.action_completed"), "data_only")
        self.assertEqual(classify_epoch("aiko.action_rejected"), "data_only")
        self.assertEqual(classify_epoch("aiko.action_cancelled"), "data_only")


class HoldIdTests(unittest.TestCase):
    def test_hold_carries_action_id(self) -> None:
        frame = _frame()
        client = _FakePolicyClient({
            "snapshot_generation": int(frame.generation),
            "selected_urge_id": None,
            "intent": "attend",
            "arguments": {},
            "reason_code": "look",
            "context_refs": [],
        })
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
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
        held = controller.last_accepted_nonverbal
        self.assertTrue(held.get("action_id"))
        self.assertEqual(held.get("intent"), "attend")


if __name__ == "__main__":
    unittest.main()

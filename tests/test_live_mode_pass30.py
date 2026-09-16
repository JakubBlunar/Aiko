"""Live mode Pass 30: explicit keep_attention / keep_style hold (L16)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from app.core.live.capabilities import SemanticCapabilities
from app.core.live.presence import admit_attention_target, clamp_presence_style
from app.core.live.proposal import LIVE_POLICY_JSON_SCHEMA, LivePolicyProposal, sanitize_arguments
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.resolver import LiveBehaviorResolver
from app.core.live.wait import MAX_WAIT_MS
from tests.test_live_mode_pass7 import _controller, _frame
from tests.test_live_mode_pass22 import _coding_frame, _shared_frame


def _caps() -> SemanticCapabilities:
    return SemanticCapabilities(
        can_orient=True, can_express=True, can_breathe=True,
    )


class KeepFlagTests(unittest.TestCase):
    def test_keep_plus_new_target_preserves_current(self) -> None:
        frame = replace(
            _shared_frame(),
            temporal=replace(_shared_frame().temporal, min_hold_remaining_ms=0),
        )
        self.assertEqual(frame.attention.target, "shared_activity")
        kept = admit_attention_target(
            "user",
            intent="remain_present",
            frame=frame,
            keep_attention=True,
        )
        self.assertEqual(kept, "shared_activity")
        switched = admit_attention_target(
            "user",
            intent="remain_present",
            frame=frame,
            keep_attention=False,
        )
        self.assertEqual(switched, "user")

    def test_keep_style_preserves_current_then_clamps(self) -> None:
        frame = _shared_frame()
        self.assertEqual(
            clamp_presence_style(
                "playful", frame, keep_style=True, current_style="give_space",
            ),
            "give_space",
        )
        self.assertEqual(clamp_presence_style("playful", frame), "playful")

    def test_user_speech_still_cancels_keep(self) -> None:
        frame = _coding_frame(typing=True)
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {
                "attention_target": "cursor",
                "presence_style": "playful",
                "keep_attention": True,
                "keep_style": True,
            },
            "reason_code": "stay",
            "context_refs": [],
        })
        ok, extra = _controller()._maybe_execute(
            proposal, frame, trigger_kind="user.speech_final", user_intent=True,
        )
        self.assertTrue(ok)
        self.assertFalse(extra.get("keep_attention"))
        self.assertFalse(extra.get("keep_style"))

    def test_generation_change_drops_keep_style(self) -> None:
        frame = replace(
            _shared_frame(),
            generation=2,
            temporal=replace(_shared_frame().temporal, min_hold_remaining_ms=0),
        )
        controller = _controller()
        controller.last_accepted_nonverbal = {
            "generation": 1,
            "intent": "remain_present",
            "attention_target": "shared_activity",
            "presence_style": "give_space",
            "keep_style": True,
            "ttl_ms": 8_000,
            "hold_until_ms": 1e18,
        }
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 2,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {
                "presence_style": "playful",
                "keep_style": True,
            },
            "reason_code": "stay",
            "context_refs": [],
        })
        ok, extra = controller._maybe_execute(
            proposal, frame, trigger_kind="silence.wake", user_intent=False,
        )
        self.assertTrue(ok)
        self.assertFalse(extra.get("keep_style"))
        self.assertEqual(extra.get("presence_style"), "playful")
        self.assertIsNone(
            controller.accepted_nonverbal_for(1),
        )


class HoldTtlAndResolverTests(unittest.TestCase):
    def test_hold_cannot_extend_past_max_wait(self) -> None:
        frame = _frame()
        controller = _controller()
        controller._hold_nonverbal(
            frame, "remain_present", "user", 999_000, 10_000.0, "",
            keep_attention=True,
        )
        held = controller.last_accepted_nonverbal
        self.assertEqual(held["ttl_ms"], MAX_WAIT_MS)
        self.assertEqual(held["hold_until_ms"], 10_000.0 + MAX_WAIT_MS)

    def test_resolver_sets_hold_attention(self) -> None:
        frame = _shared_frame()
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(),
            policy_intent="remain_present",
            attention_target="none",
            keep_attention=True,
        )
        self.assertTrue(plan.hold_attention)
        self.assertEqual(plan.attention_target, "none")

    def test_sanitize_and_schema(self) -> None:
        cleaned = sanitize_arguments(
            "remain_present",
            {
                "keep_attention": True,
                "keep_style": "yes",
                "title": "nope",
            },
        )
        self.assertEqual(cleaned, {"keep_attention": True, "keep_style": True})
        dropped = sanitize_arguments("wait", {"keep_attention": "maybe"})
        self.assertEqual(dropped, {})
        args = LIVE_POLICY_JSON_SCHEMA["properties"]["arguments"]["properties"]
        self.assertEqual(args["keep_attention"]["type"], "boolean")
        self.assertEqual(args["keep_style"]["type"], "boolean")

    def test_prompt_names_keep_flags(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            context_window=4096,
            max_tokens=64,
            prompt_ceiling=4000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("keep_attention", text)
        self.assertIn("keep_style", text)

    def test_controller_keep_records_hold(self) -> None:
        frame = replace(
            _shared_frame(),
            temporal=replace(_shared_frame().temporal, min_hold_remaining_ms=0),
        )
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {
                "attention_target": "user",
                "presence_style": "playful",
                "keep_attention": True,
            },
            "reason_code": "stay",
            "context_refs": [],
        })
        controller = _controller()
        ok, extra = controller._maybe_execute(
            proposal, frame, trigger_kind="silence.wake", user_intent=False,
        )
        self.assertTrue(ok)
        self.assertTrue(extra.get("keep_attention"))
        self.assertEqual(
            controller.last_accepted_nonverbal.get("attention_target"),
            "shared_activity",
        )
        self.assertTrue(controller.last_accepted_nonverbal.get("keep_attention"))


if __name__ == "__main__":
    unittest.main()

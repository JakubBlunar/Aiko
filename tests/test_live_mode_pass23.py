"""Live mode Pass 23: reaction_tone and intensity band (L3)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.capabilities import SemanticCapabilities
from app.core.live.modifiers import modifiers_from_situation
from app.core.live.proposal import LivePolicyProposal, sanitize_arguments
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.reaction import (
    clamp_reaction_intensity,
    clamp_reaction_tone,
    reaction_from_proposal,
)
from app.core.live.resolver import LiveBehaviorResolver
from tests.test_live_mode_pass5 import _impulse
from tests.test_live_mode_pass7 import _controller
from tests.test_live_mode_pass11 import _NOW, _coding_evidence, _snapshot
from tests.test_live_mode_pass22 import _coding_frame, _shared_frame


def _weak_chrome_frame():
    return assemble_live_situation(
        LiveAssembleInput(
            snapshot=_snapshot(user_active_app="Chrome"),
            impulses=(),
            now=_NOW,
            monotonic_ms=10_000.0,
            activity_evidence=_coding_evidence(
                app="Chrome", confidence=0.35, duration_seconds=12,
            ),
        ),
        generation=1,
    )


def _caps(**kwargs: bool) -> SemanticCapabilities:
    fields = dict(
        can_orient=True, can_express=True, can_breathe=True, can_motion=False,
    )
    fields.update(kwargs)
    return SemanticCapabilities(**fields)


class ReactionToneClampTests(unittest.TestCase):
    def test_unknown_tone_is_rejected(self) -> None:
        parsed = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "react_affectively",
            "arguments": {
                "reaction_tone": "furious",
                "reaction_intensity": "max",
                "title": "secret.md",
            },
            "reason_code": "idle",
            "context_refs": [],
        })
        self.assertNotIn("reaction_tone", parsed.arguments)
        self.assertNotIn("reaction_intensity", parsed.arguments)
        self.assertNotIn("title", parsed.arguments)
        frame = _shared_frame()
        self.assertEqual(clamp_reaction_tone("furious", frame), "neutral")

    def test_concern_cannot_come_from_weak_activity(self) -> None:
        frame = _weak_chrome_frame()
        self.assertEqual(clamp_reaction_tone("concerned", frame), "neutral")
        self.assertEqual(clamp_reaction_tone("warm", frame), "warm")

    def test_concern_ok_with_user_meaning(self) -> None:
        frame = _weak_chrome_frame()
        frame = replace(
            frame,
            interaction=replace(
                frame.interaction, last_user_meaning="that sounded bad",
            ),
        )
        self.assertEqual(clamp_reaction_tone("concerned", frame), "concerned")

    def test_sleep_and_dnd_collapse(self) -> None:
        asleep = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(sleep={"status": "asleep"}),
                impulses=(),
                now=_NOW,
                monotonic_ms=10_000.0,
            ),
            generation=1,
        )
        self.assertEqual(clamp_reaction_tone("amused", asleep), "drowsy")
        quiet = _coding_frame(quiet=True)
        self.assertEqual(clamp_reaction_tone("amused", quiet), "neutral")

    def test_intensity_only_lowers_to_cap(self) -> None:
        frame = _coding_frame()
        mods = modifiers_from_situation(frame)
        stamped = replace(
            frame,
            constraints=replace(
                frame.constraints,
                max_reaction_intensity=mods.max_reaction_intensity,
            ),
        )
        self.assertEqual(clamp_reaction_intensity("high", stamped), "mid")
        dnd = modifiers_from_situation(_coding_frame(quiet=True))
        quiet = replace(
            frame,
            constraints=replace(
                frame.constraints,
                max_reaction_intensity=dnd.max_reaction_intensity,
                dnd=True,
            ),
        )
        self.assertEqual(clamp_reaction_intensity("high", quiet), "low")


class ResolverToneTests(unittest.TestCase):
    def test_amused_uses_semantic_classes_not_rig_ids(self) -> None:
        frame = _shared_frame()
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(),
            policy_intent="react_affectively",
            reaction_tone="amused",
            reaction_intensity="high",
        )
        self.assertEqual(plan.expression_class, "amused")
        self.assertEqual(plan.body_class, "perk")
        self.assertEqual(plan.reaction_tone, "amused")
        blob = str(plan.to_payload())
        self.assertNotIn("Param", blob)
        self.assertNotIn(".exp3", blob)

    def test_capability_absent_noops_expression(self) -> None:
        frame = _shared_frame()
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(can_express=False, can_orient=False),
            policy_intent="react_affectively",
            reaction_tone="amused",
            reaction_intensity="high",
        )
        self.assertEqual(plan.expression_class, "none")
        self.assertEqual(plan.body_class, "none")

    def test_sleep_outranks_tone(self) -> None:
        frame = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(sleep={"status": "asleep"}),
                impulses=(),
                now=_NOW,
                monotonic_ms=10_000.0,
            ),
            generation=1,
        )
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(),
            policy_intent="react_affectively",
            reaction_tone="amused",
            reaction_intensity="high",
        )
        self.assertTrue(plan.degrade_to_sleep)
        self.assertEqual(plan.expression_class, "none")
        self.assertEqual(plan.body_class, "none")

    def test_low_intensity_keeps_body_from_tone(self) -> None:
        frame = _shared_frame()
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(),
            policy_intent="remain_present",
            reaction_tone="amused",
            reaction_intensity="low",
        )
        self.assertEqual(plan.expression_class, "amused")
        self.assertNotEqual(plan.body_class, "perk")


class SanitizeAndPromptTests(unittest.TestCase):
    def test_keeps_tone_and_drops_delivery_style(self) -> None:
        cleaned = sanitize_arguments(
            "react_affectively",
            {
                "reaction_tone": "amused",
                "reaction_intensity": "high",
                "delivery_style": "whisper",
                "title": "nope",
            },
        )
        self.assertEqual(
            cleaned,
            {"reaction_tone": "amused", "reaction_intensity": "high"},
        )

    def test_prompt_names_reaction_tone(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_shared_frame(),
            context_window=4096,
            max_tokens=512,
            prompt_ceiling=4000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("reaction_tone", text)
        self.assertIn("reaction_intensity", text)
        self.assertNotIn("delivery_style", text)


class ControllerToneTests(unittest.TestCase):
    def test_hold_records_clamped_tone(self) -> None:
        frame = _shared_frame()
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {
                "reaction_tone": "amused",
                "reaction_intensity": "high",
            },
            "reason_code": "delight",
            "context_refs": [],
        })
        controller = _controller()
        ok, extra = controller._maybe_execute(
            proposal, frame, trigger_kind="silence.wake", user_intent=False,
        )
        self.assertTrue(ok)
        self.assertEqual(extra.get("reaction_tone"), "amused")
        self.assertEqual(extra.get("reaction_intensity"), "high")
        self.assertEqual(
            controller.last_accepted_nonverbal.get("reaction_tone"), "amused",
        )
        self.assertEqual(
            controller.last_accepted_nonverbal.get("reaction_intensity"), "high",
        )

    def test_weak_chrome_concern_is_held_as_neutral(self) -> None:
        frame = _weak_chrome_frame()
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "react_affectively",
            "arguments": {"reaction_tone": "concerned", "reaction_intensity": "high"},
            "reason_code": "worry",
            "context_refs": [],
        })
        controller = _controller()
        ok, extra = controller._maybe_execute(
            proposal, frame, trigger_kind="heartbeat", user_intent=False,
        )
        self.assertTrue(ok)
        self.assertEqual(extra.get("reaction_tone"), "neutral")
        self.assertEqual(
            reaction_from_proposal(proposal.arguments, frame=frame)[0],
            "neutral",
        )


class TypingConcernTests(unittest.TestCase):
    def test_typing_is_user_meaning_for_concern(self) -> None:
        frame = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(user_active_app="Chrome"),
                impulses=(_impulse("user.typing_started", monotonic_ms=9_500.0),),
                now=_NOW,
                monotonic_ms=10_000.0,
                trigger_kind="user.typing_started",
                activity_evidence=_coding_evidence(
                    app="Chrome", confidence=0.35, duration_seconds=12,
                ),
            ),
            generation=1,
        )
        self.assertEqual(clamp_reaction_tone("concerned", frame), "concerned")


if __name__ == "__main__":
    unittest.main()

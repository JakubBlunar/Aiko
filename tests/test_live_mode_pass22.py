"""Live mode Pass 22: presence_style and attention_target (L2)."""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.capabilities import SemanticCapabilities
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.modifiers import (
    LiveBehaviorModifiers,
    apply_presence_style,
    modifiers_from_situation,
)
from app.core.live.presence import (
    QUIET_PRESENCE,
    admit_attention_target,
    clamp_presence_style,
)
from app.core.live.proposal import LivePolicyProposal, sanitize_arguments
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.resolver import LiveBehaviorResolver
from tests.test_live_mode_pass5 import _impulse
from tests.test_live_mode_pass7 import _FakePolicyClient, _controller
from tests.test_live_mode_pass11 import _NOW, _coding_evidence, _snapshot


def _coding_frame(*, typing: bool = False, quiet: bool = False):
    impulses = ()
    trigger = "heartbeat"
    if typing:
        impulses = (_impulse("user.typing_started", monotonic_ms=9_500.0),)
        trigger = "user.typing_started"
    return assemble_live_situation(
        LiveAssembleInput(
            snapshot=_snapshot(user_active_app="Cursor"),
            impulses=impulses,
            now=_NOW,
            monotonic_ms=10_000.0,
            trigger_kind=trigger,
            live_quiet=quiet,
            activity_evidence=_coding_evidence(),
        ),
        generation=1,
    )


def _shared_frame():
    inferred = SimpleNamespace(
        shared=True,
        shared_activity="watching anime together",
        evidence_message_ids=(),
        updated_at="2026-09-13T12:00:00Z",
    )
    return assemble_live_situation(
        LiveAssembleInput(
            snapshot=_snapshot(
                inferred=inferred,
                inferred_stale=False,
                shared_commitment_active=True,
                world_compatible=True,
            ),
            impulses=(),
            now=_NOW,
            monotonic_ms=10_000.0,
            trigger_kind="situation.shared_commitment_changed",
        ),
        generation=1,
    )


def _caps() -> SemanticCapabilities:
    return SemanticCapabilities(
        can_orient=True, can_express=True, can_breathe=True,
    )


class PresenceStyleClampTests(unittest.TestCase):
    def test_coding_interruption_resolves_to_cofocus(self) -> None:
        frame = _coding_frame(typing=True)
        self.assertEqual(
            clamp_presence_style("share_delight", frame), "cofocus",
        )
        self.assertEqual(clamp_presence_style("playful", frame), "cofocus")
        self.assertIn(
            clamp_presence_style("give_space", frame),
            {"cofocus", "give_space"},
        )

    def test_coding_without_interruption_blocks_shared_style(self) -> None:
        frame = _coding_frame()
        self.assertEqual(
            clamp_presence_style("share_delight", frame), QUIET_PRESENCE,
        )
        self.assertNotEqual(clamp_presence_style("soft_support", frame), "share_delight")

    def test_contradictory_coding_anime_cannot_share(self) -> None:
        frame = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(user_active_app="Cursor"),
                impulses=(),
                now=_NOW,
                monotonic_ms=10_000.0,
                ritual_priors=("watching anime together",),
                activity_evidence=_coding_evidence(),
            ),
            generation=1,
        )
        self.assertEqual(frame.shared.sharing, "user_only")
        self.assertNotEqual(
            clamp_presence_style("share_delight", frame), "share_delight",
        )

    def test_shared_media_may_share_delight(self) -> None:
        frame = _shared_frame()
        self.assertEqual(frame.shared.sharing, "shared")
        self.assertEqual(
            clamp_presence_style("share_delight", frame), "share_delight",
        )

    def test_dnd_collapses_to_quiet_presence(self) -> None:
        frame = _coding_frame(quiet=True)
        self.assertEqual(clamp_presence_style("playful", frame), QUIET_PRESENCE)
        self.assertEqual(
            clamp_presence_style("share_delight", frame), QUIET_PRESENCE,
        )
        self.assertEqual(clamp_presence_style("neutral", frame), QUIET_PRESENCE)

    def test_unknown_style_is_neutral(self) -> None:
        frame = _shared_frame()
        self.assertEqual(clamp_presence_style("hyped", frame), "neutral")
        self.assertEqual(clamp_presence_style("", frame), "neutral")


class AttentionAdmitTests(unittest.TestCase):
    def test_cannot_mint_shared_target_while_coding(self) -> None:
        frame = _coding_frame()
        admitted = admit_attention_target(
            "shared_activity", intent="remain_present", frame=frame,
        )
        self.assertNotEqual(admitted, "shared_activity")

    def test_hysteresis_keeps_shared_target(self) -> None:
        frame = _shared_frame()
        self.assertEqual(frame.attention.target, "shared_activity")
        self.assertGreater(frame.temporal.min_hold_remaining_ms, 0)
        admitted = admit_attention_target(
            "user", intent="remain_present", frame=frame,
        )
        self.assertEqual(admitted, "shared_activity")

    def test_wait_ignores_attention_target(self) -> None:
        frame = _shared_frame()
        admitted = admit_attention_target(
            "cursor", intent="wait", frame=frame,
        )
        self.assertEqual(admitted, frame.attention.target)


class StyleModifierTests(unittest.TestCase):
    def test_style_only_lowers_intensity_and_speech(self) -> None:
        open_mods = LiveBehaviorModifiers(
            speech_budget="open", max_reaction_intensity=1.0,
        )
        playful = apply_presence_style(open_mods, "playful")
        self.assertEqual(playful.speech_budget, "normal")
        self.assertLess(playful.max_reaction_intensity, 1.0)
        rare = LiveBehaviorModifiers(
            speech_budget="rare",
            max_reaction_intensity=0.4,
            attention_preference="user",
            questions_allowed=False,
            min_gap_after_speech_ms=12_000,
        )
        raised = apply_presence_style(rare, "playful")
        self.assertEqual(raised.speech_budget, "rare")
        self.assertEqual(raised.max_reaction_intensity, 0.4)
        self.assertEqual(raised.attention_preference, "user")
        self.assertFalse(raised.questions_allowed)
        self.assertEqual(raised.min_gap_after_speech_ms, 12_000)
        forbidden = modifiers_from_situation(_coding_frame(quiet=True))
        quieted = apply_presence_style(forbidden, "playful")
        self.assertEqual(quieted.speech_budget, "forbidden")
        self.assertLessEqual(quieted.max_reaction_intensity, 0.2)


class ArgumentSanitizeTests(unittest.TestCase):
    def test_style_kept_unknown_and_titles_dropped(self) -> None:
        cleaned = sanitize_arguments(
            "remain_present",
            {
                "presence_style": "cofocus",
                "attention_target": "user",
                "title": "secret.md",
                "delivery_style": "whisper",
            },
        )
        self.assertEqual(
            cleaned,
            {"presence_style": "cofocus", "attention_target": "user"},
        )
        wait = sanitize_arguments(
            "wait",
            {"presence_style": "give_space", "attention_target": "user"},
        )
        self.assertEqual(wait, {"presence_style": "give_space"})
        parsed = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {"presence_style": "hyped", "title": "nope"},
            "reason_code": "idle",
            "context_refs": [],
        })
        self.assertEqual(parsed.arguments, {})


class ResolverStyleTests(unittest.TestCase):
    def test_give_space_uses_quiet_classes(self) -> None:
        frame = _shared_frame()
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(),
            policy_intent="remain_present",
            presence_style="give_space",
            attention_target="shared_activity",
        )
        self.assertEqual(plan.gaze_class, "rest")
        self.assertEqual(plan.body_class, "settle")
        self.assertEqual(plan.attention_target, "shared_activity")

    def test_cofocus_leans_in(self) -> None:
        frame = _coding_frame(typing=True)
        plan = LiveBehaviorResolver().resolve(
            frame, _caps(),
            policy_intent="attend",
            presence_style="cofocus",
            attention_target="user",
        )
        self.assertEqual(plan.gaze_class, "user_eye_contact")
        self.assertEqual(plan.body_class, "lean_in")
        self.assertEqual(plan.expression_class, "attentive")

    def test_sleep_outranks_style(self) -> None:
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
            policy_intent="attend",
            presence_style="playful",
            attention_target="user",
        )
        self.assertTrue(plan.degrade_to_sleep)
        self.assertEqual(plan.attention_target, "none")
        self.assertEqual(plan.body_class, "none")


class PromptAndBudgetTests(unittest.TestCase):
    def test_prompt_names_presence_style(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_shared_frame(),
            context_window=4096,
            max_tokens=512,
            prompt_ceiling=4000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("presence_style", text)
        self.assertIn("attention_target", text)
        self.assertNotIn("title=", text)

    def test_attention_switch_budget_keeps_held_target(self) -> None:
        runtime = LiveInclinationRuntime()
        now_ms = time.monotonic() * 1000.0
        while runtime.budget.consume("attention_switch", now_mono_ms=now_ms):
            pass
        frame = _coding_frame()
        frame = replace(
            frame,
            temporal=replace(frame.temporal, min_hold_remaining_ms=0),
        )
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {"attention_target": "user", "presence_style": "cofocus"},
            "reason_code": "focus",
            "context_refs": [],
        })
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
        self.assertTrue(result.accepted)
        self.assertEqual(
            controller.last_proposal.get("attention_held"), "budget",
        )
        self.assertEqual(
            controller.last_accepted_nonverbal.get("attention_target"),
            frame.attention.target,
        )


if __name__ == "__main__":
    unittest.main()

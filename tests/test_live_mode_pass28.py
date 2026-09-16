"""Live mode Pass 28: affect/vitality inner-state notices (L12)."""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.core.live.allowed import allowed_actions_for
from app.core.live.arbiter import arbitrate_live_proposal
from app.core.live.epochs import classify_epoch
from app.core.live.main_wake import admit_main_wake, clamp_speech_act
from app.core.live.modifiers import LiveBehaviorModifiers, apply_presence_style
from app.core.live.notice import notices_from_trigger
from app.core.live.presence import STYLE_SPEECH_CAP, clamp_presence_style, quieter_speech
from app.core.live.proposal import LivePolicyProposal, sanitize_arguments
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.resolver import LiveBehaviorResolver
from app.core.live.urge import EXPRESSIVE_KINDS
from app.core.live.urge_store import LiveUrgeStore
from app.core.live.vitality_posture import (
    apply_vitality_posture,
    clamp_vitality_posture,
)
from tests.test_live_mode_pass3 import _frame as _notice_frame
from tests.test_live_mode_pass7 import (
    _FakePolicyClient,
    _controller,
    _frame,
    _proposal,
)
from tests.test_live_mode_pass10 import _delight_runtime
from tests.test_live_mode_pass13 import Pass13Host, _capture_spawns
from tests.test_live_mode_pass22 import _shared_frame
from tests.test_live_mode_pass23 import _caps


class EpochTests(unittest.TestCase):
    def test_inner_state_kinds_stay_data_only(self) -> None:
        self.assertEqual(classify_epoch("aiko.affect_changed"), "data_only")
        self.assertEqual(classify_epoch("aiko.vitality_changed"), "data_only")
        self.assertEqual(
            classify_epoch("aiko.affect_changed", situation_changed=True),
            "data_only",
        )
        self.assertEqual(
            classify_epoch("aiko.vitality_changed", situation_changed=True),
            "data_only",
        )

    def test_consider_does_not_spawn_on_inner_state(self) -> None:
        spawned: list[str] = []
        controller = _controller()

        def _spawn(
            frame: object,
            *,
            trigger_kind: str,
            prompt_input: object,
            user_intent: bool,
        ) -> None:
            del frame, prompt_input, user_intent
            spawned.append(trigger_kind)

        controller._spawn = _spawn  # type: ignore[method-assign]
        controller.consider(_frame(), trigger_kind="aiko.affect_changed")
        controller.consider(_frame(), trigger_kind="aiko.vitality_changed")
        self.assertEqual(spawned, [])


class InnerStateImpulseTests(unittest.TestCase):
    def test_heartbeat_publishes_without_refresh_or_4b(self) -> None:
        host = Pass13Host()
        spawned = _capture_spawns(host)
        refreshes: list[str] = []
        original = host.refresh_live_situation

        def _wrapped(*, trigger_kind: str = "heartbeat", **kwargs: object):
            refreshes.append(str(trigger_kind))
            return original(trigger_kind=trigger_kind, **kwargs)  # type: ignore[arg-type]

        host.refresh_live_situation = _wrapped  # type: ignore[method-assign]
        with patch("app.core.memory.memory_store.MemoryStore.add") as add:
            host._live_heartbeat_tick()
            add.assert_not_called()
        kinds = [
            item["kind"]
            for item in host._live_impulse_bus.snapshot()["tail"]
        ]
        self.assertIn("aiko.affect_changed", kinds)
        self.assertIn("aiko.vitality_changed", kinds)
        self.assertNotIn("aiko.affect_changed", refreshes)
        self.assertNotIn("aiko.vitality_changed", refreshes)
        self.assertEqual(spawned, [])
        for item in host._live_impulse_bus.snapshot()["tail"]:
            if str(item["kind"]) in {
                "aiko.affect_changed", "aiko.vitality_changed",
            }:
                payload = item["payload"]
                self.assertNotIn("title", payload)
                self.assertNotIn("text", payload)
                self.assertNotIn("content", payload)
        host._live_heartbeat_tick()
        kinds2 = [
            item["kind"]
            for item in host._live_impulse_bus.snapshot()["tail"]
        ]
        self.assertEqual(kinds2.count("aiko.affect_changed"), 1)
        self.assertEqual(kinds2.count("aiko.vitality_changed"), 1)
        host._affect_updater.tick_elapsed.assert_called()

    def test_mood_change_republishes_without_titles(self) -> None:
        host = Pass13Host()
        host._publish_live_inner_state_impulses()
        host._snapshot = replace(host._snapshot, mood_label="tired")
        host._publish_live_inner_state_impulses()
        payloads = [
            item["payload"]
            for item in host._live_impulse_bus.snapshot()["tail"]
            if item["kind"] == "aiko.affect_changed"
        ]
        self.assertEqual(payloads[-1], {"mood_label": "tired"})
        self.assertNotIn("title", payloads[-1])


class LowVitalityMenuTests(unittest.TestCase):
    def test_menu_drops_main_speech_keeps_backchannel(self) -> None:
        frame = _frame(vitality_band="low")
        allowed = allowed_actions_for(frame)
        self.assertNotIn("request_main_speech", allowed)
        self.assertIn("wait", allowed)
        self.assertIn("backchannel_user", allowed)
        self.assertEqual(clamp_presence_style("playful", frame), "give_space")
        self.assertEqual(clamp_speech_act("celebrate", frame), "")
        self.assertEqual(clamp_speech_act("share_observation", frame), "share_observation")
        result = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
            allowed_actions=allowed,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "not_allowed")

    def test_empty_allowed_still_rejects_unprompted_at_admit(self) -> None:
        runtime, urge_id = _delight_runtime()
        frame = replace(
            _frame(vitality_band="low"),
            constraints=replace(
                _frame(vitality_band="low").constraints, allowed_actions=(),
            ),
        )
        reason = admit_main_wake(
            intent="request_main_speech",
            user_intent=False,
            selected_urge_id=urge_id,
            urges=runtime.urges.active(),
            frame=frame,
            decided_generation=None,
            now_mono_ms=time.monotonic() * 1000.0,
            budget_remaining=3,
        )
        self.assertEqual(reason, "speech_rare")
        user = admit_main_wake(
            intent="request_main_speech",
            user_intent=True,
            selected_urge_id=urge_id,
            urges=runtime.urges.active(),
            frame=frame,
            decided_generation=None,
            now_mono_ms=time.monotonic() * 1000.0,
            budget_remaining=3,
        )
        self.assertEqual(user, "user_turn")
        empty = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id=urge_id),
            snapshot_generation=1,
            known_urge_ids=(urge_id,),
            allowed_actions=(),
        )
        self.assertTrue(empty.accepted)
        self.assertTrue(empty.talk_about)
        enqueued: list[dict] = []
        client = _FakePolicyClient({
            "snapshot_generation": int(frame.generation),
            "selected_urge_id": urge_id,
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "share_the_scene",
            "context_refs": [],
        })
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_main_wake=lambda payload: enqueued.append(payload) or True,
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
        self.assertTrue(result.accepted)
        self.assertEqual(enqueued, [])
        self.assertEqual(
            controller.last_proposal.get("main_wake_rejected"), "speech_rare",
        )


class PostureTests(unittest.TestCase):
    def test_unknown_becomes_keep(self) -> None:
        frame = _frame()
        self.assertEqual(clamp_vitality_posture("explode", frame), "keep")
        self.assertEqual(clamp_vitality_posture("", frame), "keep")

    def test_settle_cannot_mint_playful_or_raise_intensity(self) -> None:
        frame = _frame()
        style, tone, intensity = apply_vitality_posture(
            "settle",
            style="playful",
            tone="amused",
            intensity="low",
            frame=frame,
        )
        self.assertEqual(style, "give_space")
        self.assertEqual(tone, "drowsy")
        self.assertEqual(intensity, "low")

    def test_low_vitality_keep_cannot_stay_playful(self) -> None:
        frame = _frame(vitality_band="low")
        style, _, _ = apply_vitality_posture(
            "keep",
            style="playful",
            tone="amused",
            intensity="high",
            frame=frame,
        )
        self.assertEqual(style, "give_space")

    def test_soften_does_not_raise_rare_speech(self) -> None:
        frame = _frame(vitality_band="low")
        style, _, intensity = apply_vitality_posture(
            "soften",
            style="playful",
            tone="amused",
            intensity="high",
            frame=frame,
        )
        self.assertEqual(style, "give_space")
        self.assertEqual(intensity, "mid")
        budget = quieter_speech("rare", STYLE_SPEECH_CAP[style])
        self.assertEqual(budget, "rare")
        lowered = apply_presence_style(
            LiveBehaviorModifiers(speech_budget="rare"), style,
        )
        self.assertEqual(lowered.speech_budget, "rare")

    def test_sanitize_keeps_known_posture(self) -> None:
        cleaned = sanitize_arguments(
            "remain_present",
            {"vitality_posture": "settle", "title": "secret.md"},
        )
        self.assertEqual(cleaned, {"vitality_posture": "settle"})
        dropped = sanitize_arguments(
            "wait",
            {"vitality_posture": "explode"},
        )
        self.assertEqual(dropped, {})

    def test_controller_settle_does_not_rewrite_intent(self) -> None:
        frame = _frame(vitality_band="low")
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {
                "presence_style": "playful",
                "reaction_tone": "amused",
                "reaction_intensity": "high",
                "vitality_posture": "settle",
            },
            "reason_code": "quiet",
            "context_refs": [],
        })
        controller = _controller()
        ok, extra = controller._maybe_execute(
            proposal, frame, trigger_kind="silence.wake", user_intent=False,
        )
        self.assertTrue(ok)
        self.assertEqual(extra.get("presence_style"), "give_space")
        self.assertEqual(extra.get("reaction_tone"), "drowsy")
        self.assertEqual(extra.get("reaction_intensity"), "low")
        self.assertEqual(extra.get("vitality_posture"), "settle")
        self.assertEqual(
            controller.last_accepted_nonverbal.get("intent"), "remain_present",
        )


class CapabilityAndNoticeTests(unittest.TestCase):
    def test_capability_absent_still_noops(self) -> None:
        frame = _shared_frame()
        low = replace(
            frame,
            aiko=replace(frame.aiko, vitality_band="low"),
        )
        plan = LiveBehaviorResolver().resolve(
            low, _caps(can_express=False, can_orient=False),
            policy_intent="react_affectively",
            reaction_tone="amused",
            reaction_intensity="high",
            presence_style="playful",
        )
        self.assertEqual(plan.expression_class, "none")
        self.assertEqual(plan.body_class, "none")

    def test_affect_notice_is_not_expressive(self) -> None:
        notices = notices_from_trigger("aiko.affect_changed", _notice_frame())
        self.assertEqual(notices[0].kind, "affect_changed")
        created = LiveUrgeStore().ingest_notices(notices, now_mono_ms=5_000.0)
        self.assertEqual(created[0].kind, "remain_present")
        self.assertNotIn(created[0].kind, EXPRESSIVE_KINDS)
        self.assertEqual(notices_from_trigger("heartbeat", _notice_frame()), ())

    def test_prompt_names_vitality_posture(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            context_window=4096,
            max_tokens=64,
            prompt_ceiling=4000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("vitality_posture", text)
        self.assertIn("request_main_speech", text)
        self.assertNotIn("delivery_style", text)


if __name__ == "__main__":
    unittest.main()

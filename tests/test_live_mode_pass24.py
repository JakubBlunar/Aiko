"""Live mode Pass 24: main-wake speech_act routing (L6)."""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace

from app.core.conversation.stance import INITIATE, _OFFERS
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.main_wake import (
    clamp_speech_act,
    render_live_talk_about,
    speech_act_from_proposal,
)
from app.core.live.proposal import sanitize_arguments
from app.core.live.prompt import LivePolicyPromptAssembler
from tests.test_live_mode_pass1 import LiveModeMixinHost
from tests.test_live_mode_pass7 import _FakePolicyClient, _controller, _frame
from tests.test_live_mode_pass22 import _shared_frame


def _url_delight_runtime():
    runtime = LiveInclinationRuntime()
    now_ms = time.monotonic() * 1000.0
    urge = runtime.urges.propose(
        kind="share_delight",
        subject="https://intranet/secret.md",
        source="test",
        source_ids=("x",),
        repetition_key="delight:x",
        now_mono_ms=now_ms,
        cue_id=9,
    )
    assert urge is not None
    return runtime, urge.urge_id


class SpeechActClampTests(unittest.TestCase):
    def test_unknown_act_is_dropped(self) -> None:
        frame = _frame()
        cleaned = sanitize_arguments(
            "request_main_speech",
            {
                "speech_act": "lecture",
                "title": "secret.md",
                "delivery_style": "whisper",
            },
        )
        self.assertEqual(cleaned, {})
        self.assertEqual(clamp_speech_act("lecture", frame), "")
        self.assertEqual(clamp_speech_act("celebrate", frame), "celebrate")

    def test_gentle_question_removed_when_questions_forbidden(self) -> None:
        frame = replace(
            _frame(),
            constraints=replace(_frame().constraints, questions_allowed=False),
        )
        self.assertEqual(clamp_speech_act("gentle_question", frame), "")
        self.assertEqual(
            clamp_speech_act("share_observation", frame), "share_observation",
        )
        self.assertEqual(
            speech_act_from_proposal(
                {"speech_act": "gentle_question"}, frame=frame,
            ),
            "",
        )


class TalkAboutSpeechActTests(unittest.TestCase):
    def test_candidate_is_untrusted_and_privacy_safe(self) -> None:
        text = render_live_talk_about({
            "intent": "request_main_speech",
            "reason_code": "share_the_scene",
            "urge_kind": "share_delight",
            "urge_id": "u1",
            "speech_act": "celebrate",
            "cue_subject": "https://intranet/secret.md",
            "cue_id": 4,
            "situation_summary": "inferred watching_anime; sharing together",
            "concept_ids": (12,),
        })
        self.assertIn("speech_act=celebrate", text)
        self.assertIn("untrusted", text)
        self.assertIn("Cue id=4", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("secret.md", text)
        self.assertNotIn("delivery_style", text)

    def test_short_subject_is_kept(self) -> None:
        text = render_live_talk_about({
            "intent": "request_main_speech",
            "reason_code": "share_the_scene",
            "cue_subject": "shared_activity",
            "situation_summary": "sharing together",
        })
        self.assertIn("Subject: shared_activity", text)

    def test_talk_about_still_offers_initiate(self) -> None:
        self.assertIn("live_talk_about_block", _OFFERS[INITIATE])


class ControllerSpeechActTests(unittest.TestCase):
    def test_payload_keeps_act_and_drops_title_subject(self) -> None:
        runtime, urge_id = _url_delight_runtime()
        enqueued: list[dict] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": urge_id,
            "intent": "request_main_speech",
            "arguments": {"speech_act": "celebrate", "title": "nope.md"},
            "reason_code": "share_the_scene",
            "context_refs": [],
        })
        frame = _frame()
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
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0]["speech_act"], "celebrate")
        self.assertEqual(enqueued[0]["cue_id"], 9)
        self.assertEqual(enqueued[0]["cue_subject"], "")
        self.assertNotIn("secret.md", str(enqueued[0]))

    def test_k92_silence_is_success_not_fallback(self) -> None:
        controller = _controller()
        controller.note_main_wake_silence(1)
        self.assertIsNone(controller._pending_fallback)
        self.assertEqual(controller.diagnostics()["main_wake_silence"], 1)


class PromptSpeechActTests(unittest.TestCase):
    def test_prompt_names_speech_act(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_shared_frame(),
            context_window=4096,
            max_tokens=512,
            prompt_ceiling=4000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("speech_act", text)
        self.assertIn("gentle_question", text)
        self.assertNotIn("delivery_style", text)


class MixinEnqueueSpeechActTests(unittest.TestCase):
    def test_event_carries_speech_act(self) -> None:
        host = LiveModeMixinHost()
        ok = host._enqueue_live_main_wake({
            "generation": 7,
            "urge_id": "u1",
            "reason_code": "share_the_scene",
            "situation_summary": "inferred watching_anime",
            "concept_ids": (3,),
            "speech_act": "offer_support",
            "cue_subject": "shared_activity",
            "cue_id": 2,
        })
        self.assertTrue(ok)
        event = host.enqueued[0]
        self.assertEqual(event.speech_act, "offer_support")
        self.assertEqual(event.cue_subject, "shared_activity")
        self.assertEqual(event.cue_id, 2)


if __name__ == "__main__":
    unittest.main()

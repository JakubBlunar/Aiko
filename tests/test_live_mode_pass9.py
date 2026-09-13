"""Live mode Pass 9: validated micro-utterances. Main-wake stays shadow."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.micro_utterance import validate_micro_utterance
from app.core.live.proposal import LivePolicyProposal
from tests.test_chat_database import _TempDB
from tests.test_live_mode_pass1 import LiveModeMixinHost
from tests.test_live_mode_pass7 import _controller, _FakePolicyClient, _frame, _proposal


class ValidatorTests(unittest.TestCase):
    def test_allows_short_affective_lines(self) -> None:
        self.assertEqual(
            validate_micro_utterance("oh, that was beautiful"),
            "oh, that was beautiful",
        )
        self.assertEqual(validate_micro_utterance("I'm still here"), "I'm still here")

    def test_rejects_questions_facts_and_length(self) -> None:
        self.assertIsNone(validate_micro_utterance("what are you watching?"))
        self.assertIsNone(validate_micro_utterance("the score is 3 to 2"))
        self.assertIsNone(validate_micro_utterance("you should take a break"))
        self.assertIsNone(validate_micro_utterance("I remember last week"))
        self.assertIsNone(
            validate_micro_utterance(
                "this is far too many words for a tiny live line",
            ),
        )


class MicroExecuteTests(unittest.TestCase):
    def test_react_micro_speaks_and_is_not_shadow(self) -> None:
        spoken: list[str] = []
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "react_affectively",
            "arguments": {
                "text": "oh, that was beautiful",
                "delivery": "micro_utterance",
            },
            "reason_code": "shared_media_reaction",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
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
        self.assertTrue(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal.get("micro_spoken"))
        self.assertFalse(controller.last_proposal["shadow"])
        self.assertEqual(spoken, ["oh, that was beautiful"])
        self.assertNotIn("micro_text", controller.last_proposal)
        self.assertEqual(runtime.budget.remaining("micro_speech", now_mono_ms=0), 1)

    def test_request_main_speech_stays_shadow(self) -> None:
        spoken: list[str] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": "u1",
            "intent": "request_main_speech",
            "arguments": {
                "text": "want to talk",
                "delivery": "micro_utterance",
            },
            "reason_code": "want_to_talk",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": ("u1",)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.talk_about)
        self.assertFalse(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal["shadow"])
        self.assertEqual(spoken, [])

    def test_invalid_line_does_not_speak(self) -> None:
        spoken: list[str] = []
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "acknowledge_user",
            "arguments": {
                "text": "what are you watching?",
                "delivery": "micro_utterance",
            },
            "reason_code": "check",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
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
        self.assertTrue(controller.last_proposal["executed"])
        self.assertEqual(controller.last_proposal.get("micro_skipped"), "invalid")
        self.assertEqual(spoken, [])
        held = controller.accepted_nonverbal_for(int(frame.generation))
        assert held is not None
        self.assertEqual(held["intent"], "acknowledge_user")

    def test_skips_during_user_turn(self) -> None:
        spoken: list[str] = []
        runtime = LiveInclinationRuntime()
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
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
        )
        controller._infer(
            frame,
            trigger_kind="user.message_sent",
            prompt_input={},
            user_intent=True,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        self.assertEqual(controller.last_proposal.get("micro_skipped"), "user_turn")
        self.assertEqual(spoken, [])

    def test_skips_while_asleep(self) -> None:
        spoken: list[str] = []
        runtime = LiveInclinationRuntime()
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
            inclination_provider=lambda: runtime,
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
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
        self.assertEqual(controller.last_proposal.get("micro_skipped"), "sleep")
        self.assertEqual(spoken, [])


class PersistTests(unittest.TestCase):
    def test_persists_without_looking_like_a_turn(self) -> None:
        host = LiveModeMixinHost()
        host._turn_in_progress = False
        host.is_tts_playing = lambda: False
        host.notified: list[tuple] = []
        host.spoken: list[str] = []
        host._notify_message = lambda *args: host.notified.append(args)
        host.speak_text = lambda text: host.spoken.append(text) or True
        with _TempDB() as db:
            host._chat_db = db
            ok = host._deliver_live_micro_utterance(
                "oh, that was beautiful",
                _proposal(intent="react_affectively"),
                _frame(),
            )
            self.assertTrue(ok)
            rows = db.get_messages(host.session_key)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].role, "assistant")
            self.assertEqual(rows[0].content, "oh, that was beautiful")
            self.assertEqual(rows[0].dialogue_act, "live_micro")
        self.assertEqual(host.notified[0][0], "Assistant (live)")
        self.assertEqual(host.spoken, ["oh, that was beautiful"])

    def test_skips_when_turn_or_tts_owns_the_floor(self) -> None:
        host = LiveModeMixinHost()
        host._chat_db = MagicMock()
        host._turn_in_progress = True
        host.is_tts_playing = lambda: False
        host.speak_text = MagicMock()
        self.assertFalse(
            host._deliver_live_micro_utterance(
                "I'm still here",
                _proposal(),
                _frame(),
            ),
        )
        host._chat_db.add_message.assert_not_called()
        host.speak_text.assert_not_called()


class DirectExecuteTests(unittest.TestCase):
    def test_backchannel_without_delivery_stays_shadow(self) -> None:
        spoken: list[str] = []
        frame = _frame()
        controller = _controller(
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
        )
        executed, extra = controller._maybe_execute(
            LivePolicyProposal(
                snapshot_generation=int(frame.generation),
                selected_urge_id="u1",
                intent="backchannel_user",
                arguments={},
                reason_code="mm",
            ),
            frame,
            trigger_kind="silence.wake",
            user_intent=False,
        )
        self.assertFalse(executed)
        self.assertEqual(extra.get("micro_skipped"), "missing_text")
        self.assertEqual(spoken, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for the D3 spoken notice ahead of a slow brain tool
(``TurnRunner._announce_slow_tool`` + ``_SLOW_TOOLS``).

The synchronous ``web_search`` blocks the turn on a ~2.7s network
round-trip. Contracts pinned here:

1. **She says what she's doing** before the dispatch, through the turn's
   TTS callback — so several seconds of silence reads as a lookup rather
   than as a hang.
2. **Spoken only.** The notice never reaches ``on_token`` and never
   reaches the persisted message, so the transcript stays exactly the
   model's own words (the contract ``FillerInjector`` already keeps).
3. **Once per turn, slow tools only.** A fast tool dispatch is silent,
   and two searches in one turn don't produce two interjections.
"""
from __future__ import annotations

import types
import unittest
from typing import Any
from unittest.mock import MagicMock

from app.core.session.turn_runner import TurnRunner, _SLOW_TOOLS
from app.llm.ollama_client import OllamaUsage


def _tool_call(name: str, call_id: str = "c1") -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, arguments={}, call_id=call_id)


def _build_runner(tool_name: str) -> tuple[TurnRunner, MagicMock]:
    ollama = MagicMock()
    response = MagicMock()
    response.content = ""
    response.tool_calls = [_tool_call(tool_name)]
    ollama.chat_with_tools = MagicMock(return_value=response)
    ollama.last_usage = OllamaUsage()
    ollama.tool_pass_tool_choice = lambda _model, requested: requested

    registry = MagicMock()
    registry.to_ollama_tools = MagicMock(
        return_value=[{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )
    registry.dispatch = MagicMock(
        return_value=types.SimpleNamespace(
            name=tool_name, content="{}", ok=True,
        ),
    )
    runner = TurnRunner(
        ollama=ollama,
        db=MagicMock(),
        prompt_assembler=MagicMock(),
        model="gpt-5-mini",
        context_window=8192,
        max_tokens=512,
        temperature=0.7,
        filler_enabled=False,
        tool_registry=registry,
    )
    return runner, registry


class SlowToolMembershipTests(unittest.TestCase):
    def test_web_search_is_the_slow_tool(self) -> None:
        self.assertIn("web_search", _SLOW_TOOLS)

    def test_fast_tools_are_not_listed(self) -> None:
        self.assertNotIn("get_time", _SLOW_TOOLS)
        self.assertNotIn("recall", _SLOW_TOOLS)


class SpokenNoticeTests(unittest.TestCase):
    def test_speaks_before_dispatching_web_search(self) -> None:
        runner, registry = _build_runner("web_search")
        spoken: list[tuple[str, str]] = []
        order: list[str] = []
        registry.dispatch = MagicMock(
            side_effect=lambda *a, **k: (
                order.append("dispatch"),
                types.SimpleNamespace(name="web_search", content="{}", ok=True),
            )[1],
        )

        def _tts(text: str, reaction: str) -> None:
            order.append("speak")
            spoken.append((text, reaction))

        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "is there a season 3?"}],
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=_tts,
        )
        self.assertEqual(len(spoken), 1)
        # The wait has to be announced before it starts, not after.
        self.assertEqual(order, ["speak", "dispatch"])

    def test_notice_is_a_real_sentence(self) -> None:
        runner, _ = _build_runner("web_search")
        spoken: list[str] = []
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "look it up"}],
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=lambda text, _r: spoken.append(text),
        )
        # Short filler tokens ("Hmm,") come from the other injector; this
        # one has to say what she's doing.
        self.assertTrue(spoken[0].endswith("."))
        self.assertGreater(len(spoken[0]), 12)

    def test_fast_tool_dispatch_is_silent(self) -> None:
        runner, _ = _build_runner("get_time")
        spoken: list[str] = []
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "what time is it?"}],
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=lambda text, _r: spoken.append(text),
        )
        self.assertEqual(spoken, [])

    def test_only_announces_once_per_turn(self) -> None:
        runner, _ = _build_runner("web_search")
        spoken: list[str] = []
        # Two rounds, each picking web_search again.
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "look it up"}],
            stop_requested=None,
            max_rounds=2,
            on_tts_chunk=lambda text, _r: spoken.append(text),
        )
        self.assertEqual(len(spoken), 1)

    def test_no_tts_callback_is_a_no_op(self) -> None:
        # Typed-chat turns have no speech callback; the dispatch must
        # still happen (the UI shows the tool-activity chip instead).
        runner, registry = _build_runner("web_search")
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "look it up"}],
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=None,
        )
        registry.dispatch.assert_called_once()

    def test_notice_never_reaches_the_token_stream(self) -> None:
        # ``_maybe_run_tool_pass`` has no on_token parameter at all: the
        # notice is structurally incapable of entering the transcript.
        runner, _ = _build_runner("web_search")
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "look it up"},
        ]
        runner._maybe_run_tool_pass(
            messages,
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=lambda _t, _r: None,
        )
        spoken_texts = [
            str(m.get("content") or "") for m in messages
        ]
        self.assertFalse(
            any("hang on" in t.lower() for t in spoken_texts),
        )

    def test_failing_tts_callback_does_not_break_dispatch(self) -> None:
        runner, registry = _build_runner("web_search")

        def _boom(_t: str, _r: str) -> None:
            raise RuntimeError("tts down")

        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "look it up"}],
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=_boom,
        )
        registry.dispatch.assert_called_once()

    def test_flag_records_that_the_notice_fired(self) -> None:
        # The streaming pass reads this to skip the slow-first-token
        # filler, so a second interjection doesn't stack on the first.
        runner, _ = _build_runner("web_search")
        self.assertFalse(runner._spoke_slow_tool_notice)
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "look it up"}],
            stop_requested=None,
            max_rounds=1,
            on_tts_chunk=lambda _t, _r: None,
        )
        self.assertTrue(runner._spoke_slow_tool_notice)


class LookupFillerPoolTests(unittest.TestCase):
    def test_every_tone_has_phrases(self) -> None:
        from app.core.voice.filler_injector import (
            _LOOKUP_FILLERS,
            pick_lookup_filler,
        )

        for tone in _LOOKUP_FILLERS:
            self.assertTrue(_LOOKUP_FILLERS[tone])
        for reaction in ("playful", "warm", "curious", "neutral", None):
            phrase, tts_reaction = pick_lookup_filler(reaction)
            self.assertTrue(phrase.strip())
            self.assertTrue(tts_reaction)

    def test_unknown_reaction_falls_back_to_neutral(self) -> None:
        from app.core.voice.filler_injector import (
            _LOOKUP_FILLERS,
            pick_lookup_filler,
        )

        phrase, _ = pick_lookup_filler("wildly_unknown_mood")
        self.assertIn(phrase, _LOOKUP_FILLERS["neutral"])


if __name__ == "__main__":
    unittest.main()

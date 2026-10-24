"""K105 pause-only delivery across the real cleanup/cadence boundary."""
import random
from unittest.mock import MagicMock

import pytest

from app.core.services.response_text_service import strip_all_meta_tags
from app.core.session.session_text_utils import sanitize_assistant_text
from app.core.session.turn_runner import TurnRunner
from app.core.session.prompt_assembler import PromptTelemetry
from app.core.session.session_text_utils import drain_tts_stream_chunks
from app.core.voice.cadence import CadenceContext, ProsodyDispatcher, analyze_sentence
from app.core.voice.pause_stream import speech_prefix_with_pauses
from app.llm.ollama_client import OllamaUsage


def test_pause_adds_one_beat_without_changing_rate_gain_or_words():
    events = []
    dispatcher = ProsodyDispatcher(
        lambda text, reaction, **params: events.append((text, reaction, params)),
        rng=random.Random(0),
    )
    dispatcher.set_silence_provider(lambda ms: events.append(ms))
    TurnRunner._dispatch_chunk_with_earcons(
        "[[prosody:pause]] I mean that.", mood="warm",
        on_tts_chunk=dispatcher.dispatch, on_earcon=None,
    )
    assert events[0] == 180
    assert events[1] == ("I mean that.", "warm", {"speed": 1.0, "gain_db": 0.0})
    assert events[2] == 60
    assert len(events) == 3


def test_pause_preserves_existing_context_speed_and_does_not_invent_filler():
    ctx = CadenceContext(base_reaction="thoughtful", rng=random.Random(0))
    baseline = analyze_sentence("I mean that.", ctx)
    paused = analyze_sentence("[[prosody:pause]] I mean that.", ctx)
    assert paused.speed_hint == baseline.speed_hint
    assert paused.gain_db == baseline.gain_db
    assert paused.prefix_text == ""
    assert paused.pause_before_ms == 180


def test_blank_pause_is_not_enqueued_and_does_not_affect_next_sentence():
    calls = []
    TurnRunner._dispatch_chunk_with_earcons(
        "[[prosody:pause]]", mood="warm",
        on_tts_chunk=lambda *args: calls.append(args), on_earcon=None,
    )
    assert calls == []
    assert analyze_sentence("Next sentence.", CadenceContext()).pause_before_ms == 0


def test_legacy_speed_tags_keep_their_existing_stream_behavior():
    calls = []
    TurnRunner._dispatch_chunk_with_earcons(
        "[[prosody:fast]] I have an idea.", mood="warm",
        on_tts_chunk=lambda *args: calls.append(args), on_earcon=None,
    )
    assert calls == [("I have an idea.", "warm")]


def test_pause_tag_is_absent_from_transcript_and_display():
    text = "[[prosody:pause]] I mean that."
    assert sanitize_assistant_text(strip_all_meta_tags(text)) == "I mean that."
    assert strip_all_meta_tags(text).strip() == "I mean that."


@pytest.mark.parametrize("size", [1, 5, 200])
@pytest.mark.parametrize("reaction", ["", "[[reaction:warm]] "])
def test_real_stream_retains_one_pause_without_leaking_or_replaying_text(size, reaction):
    raw = reaction + "[[prosody:pause]] I mean that. [[prosody:pause]] You matter."
    llm = MagicMock()
    llm.chat_stream.return_value = iter(raw[i:i + size] for i in range(0, len(raw), size))
    llm.last_usage = OllamaUsage()
    prompt = MagicMock()
    prompt.assemble_with_budget.return_value = ([], PromptTelemetry())
    runner = TurnRunner(
        ollama=llm, db=MagicMock(), prompt_assembler=prompt, model="test",
        context_window=8192, max_tokens=512, temperature=0.7, filler_enabled=False,
    )
    events = []
    dispatcher = ProsodyDispatcher(
        lambda text, mood, **kwargs: events.append(text), rng=random.Random(0),
    )
    dispatcher.set_silence_provider(lambda ms: events.append(ms))
    displayed = []
    result = runner.run(
        session_key="test", user_text="Do you mean that?",
        on_token=displayed.append, on_tts_chunk=dispatcher.dispatch,
    )
    assert events.count(180) == 1
    spoken = " ".join(event for event in events if isinstance(event, str))
    assert spoken == "I mean that. You matter."
    assert "[[" not in "".join(displayed)
    assert "[[" not in result.text


def test_pause_in_hidden_content_is_not_performed():
    raw = "[[detail]][[prosody:pause]] hidden[[/detail]] Visible."
    assert "[[prosody:pause]]" not in speech_prefix_with_pauses(raw)
    assert speech_prefix_with_pauses("literal \ue105") == "literal \ue105"


def test_pause_after_a_short_sentence_keeps_its_utterance_boundary():
    chunks, tail = drain_tts_stream_chunks("Yes. [[prosody:pause]] I mean that.", flush=True)
    assert chunks == ["Yes.", "[[prosody:pause]] I mean that."]
    assert tail == ""

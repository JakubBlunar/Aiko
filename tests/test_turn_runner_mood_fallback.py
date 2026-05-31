"""Tests for the end-of-stream mood fallback in
:class:`app.core.session.turn_runner.TurnRunner`.

When the LLM forgets to emit ``[[reaction:X]]`` at the head of its
reply, the streaming branch's ``mood is not None`` gate suppressed
*every* TTS chunk and side-channel callback for the whole turn.
The user heard the slow-first-token filler, saw the chat transcript
populate, but the actual reply was silent and any embedded
``[[outfit:X]]`` / ``[[motion:X]]`` / ``[[overlay:X]]`` tags were
silently lost.

The fallback added in ``_run_inner`` defaults ``mood`` to
``"neutral"`` after the second-chance reaction parse so the
existing final-flush path still fires once over the full body —
recovering both TTS and the side-channel dispatchers in one pass.

These tests build a real ``TurnRunner`` with mocked dependencies so
the streaming/parsing/dispatch logic is exercised end-to-end against
a controlled token stream.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from app.core.session.prompt_assembler import PromptTelemetry
from app.core.session.turn_runner import TurnResult, TurnRunner
from app.llm.ollama_client import OllamaUsage


def _build_runner(
    *,
    stream_tokens: list[str],
    add_message_id: int = 1,
) -> TurnRunner:
    """Construct a TurnRunner whose dependencies are mocked but whose
    real ``_run_inner`` is exercised against a controlled token
    stream.

    The mocked ``OllamaClient.chat_stream`` yields ``stream_tokens``
    in order, simulating the LLM. ``last_usage`` returns a zeroed
    ``OllamaUsage`` so the post-turn log line reports nothing
    interesting (we only assert on callback invocations and the
    returned ``TurnResult``).
    """
    ollama = MagicMock()
    ollama.chat_stream = MagicMock(return_value=iter(stream_tokens))
    ollama.last_usage = OllamaUsage()

    db = MagicMock()
    db.add_message = MagicMock(return_value=add_message_id)

    prompt = MagicMock()
    # ``assemble_with_budget`` returns ``(messages, telemetry)``. An
    # empty message list keeps the prompt-token-pct branch dormant
    # (no compaction notify fires) and the tool-call counter at 0.
    prompt.assemble_with_budget = MagicMock(
        return_value=([], PromptTelemetry()),
    )

    return TurnRunner(
        ollama=ollama,
        db=db,
        prompt_assembler=prompt,
        model="test-model",
        context_window=8192,
        max_tokens=512,
        temperature=0.7,
        # Disable the slow-first-token filler so the unit tests don't
        # have to deal with its background timer thread.
        filler_enabled=False,
    )


class MoodFallbackTtsTests(unittest.TestCase):
    """When the LLM omits ``[[reaction:X]]`` the body should still be
    spoken via TTS exactly once at end-of-stream."""

    def test_response_spoken_when_no_reaction_tag(self) -> None:
        # Stream is plain prose with no opening reaction tag — the
        # historical bug silenced this path entirely.
        runner = _build_runner(
            stream_tokens=["Hello", " there!", " Glad to see you."],
        )
        tts_chunks: list[tuple[str, str]] = []

        result = runner.run(
            session_key="default:main",
            user_text="hi",
            on_tts_chunk=lambda text, mood: tts_chunks.append((text, mood)),
        )
        self.assertTrue(tts_chunks, "fallback path must reach the TTS callback")
        # Every chunk should carry the neutral fallback mood.
        for _text, mood in tts_chunks:
            self.assertEqual(mood, "neutral")
        # Concatenated TTS text should cover the body.
        spoken = " ".join(t for t, _ in tts_chunks)
        self.assertIn("Hello", spoken)
        self.assertIn("Glad to see you", spoken)
        self.assertTrue(result.mood_fallback)
        self.assertEqual(result.reaction, "neutral")

    def test_outfit_tag_dispatched_when_no_reaction_tag(self) -> None:
        # Embedding ``[[outfit:day]]`` mid-prose should still flip the
        # outfit even with the reaction tag missing.
        runner = _build_runner(
            stream_tokens=[
                "Sure thing! ", "[[outfit:", "day]] ", "feels better.",
            ],
        )
        outfits: list[str] = []
        tts_chunks: list[tuple[str, str]] = []

        result = runner.run(
            session_key="default:main",
            user_text="change clothes please",
            on_tts_chunk=lambda text, mood: tts_chunks.append((text, mood)),
            on_outfit=lambda name: outfits.append(name),
        )
        self.assertEqual(outfits, ["day"])
        self.assertTrue(result.mood_fallback)
        # Persisted body must have the outfit tag stripped.
        self.assertNotIn("[[outfit:", result.text)
        # And TTS should have spoken something (no silent path).
        spoken = " ".join(t for t, _ in tts_chunks)
        self.assertIn("Sure thing", spoken)
        self.assertIn("feels better", spoken)
        self.assertNotIn("[[outfit:", spoken)

    def test_motion_tag_dispatched_when_no_reaction_tag(self) -> None:
        runner = _build_runner(
            stream_tokens=["Yes ", "[[motion:nod]] ", "absolutely."],
        )
        motions: list[str] = []
        result = runner.run(
            session_key="default:main",
            user_text="agreed?",
            on_tts_chunk=lambda text, mood: None,
            on_motion=lambda name: motions.append(name),
        )
        self.assertEqual(motions, ["nod"])
        self.assertTrue(result.mood_fallback)
        self.assertNotIn("[[motion:", result.text)

    def test_overlay_tag_dispatched_when_no_reaction_tag(self) -> None:
        runner = _build_runner(
            stream_tokens=["mm ", "[[overlay:blush]] ", "thanks"],
        )
        overlays: list[str] = []
        result = runner.run(
            session_key="default:main",
            user_text="you look nice",
            on_tts_chunk=lambda text, mood: None,
            on_overlay=lambda name: overlays.append(name),
        )
        self.assertEqual(overlays, ["blush"])
        self.assertTrue(result.mood_fallback)

    def test_overlay_and_motion_both_dispatched_when_reaction_missing(
        self,
    ) -> None:
        """Regression for the 26-May turn that triggered this whole audit:
        the LLM emitted both ``[[overlay:stars]]`` (correct channel) and
        ``[[motion:tail_wag]]`` (wrong channel — tail_wag is an overlay)
        in the same reply without a leading ``[[reaction:X]]``. Both raw
        callbacks must fire at end-of-stream so the misroute even has a
        chance of being caught by the ``SessionController`` safety net.
        """
        runner = _build_runner(
            stream_tokens=[
                "oh hi! ",
                "[[overlay:stars]] ",
                "[[motion:tail_wag]] ",
                "missed you!",
            ],
        )
        overlays: list[str] = []
        motions: list[str] = []
        result = runner.run(
            session_key="default:main",
            user_text="hey",
            on_tts_chunk=lambda text, mood: None,
            on_overlay=lambda name: overlays.append(name),
            on_motion=lambda name: motions.append(name),
        )
        self.assertEqual(overlays, ["stars"])
        self.assertEqual(motions, ["tail_wag"])
        self.assertTrue(result.mood_fallback)
        # Both tags must be stripped from the persisted body.
        self.assertNotIn("[[overlay:", result.text)
        self.assertNotIn("[[motion:", result.text)


class MoodFallbackMetricTests(unittest.TestCase):
    """The ``mood_fallback`` flag should accurately mirror whether the
    fallback path fired so the MCP debug tool / log greps don't lie."""

    def test_mood_fallback_flag_set_on_turn_result(self) -> None:
        runner = _build_runner(stream_tokens=["plain text body"])
        result = runner.run(
            session_key="default:main",
            user_text="hi",
            on_tts_chunk=lambda text, mood: None,
        )
        self.assertIsInstance(result, TurnResult)
        self.assertTrue(result.mood_fallback)
        self.assertEqual(result.reaction, "neutral")

    def test_normal_reaction_flow_does_not_set_fallback_flag(self) -> None:
        # Happy path: the LLM emits ``[[reaction:cheerful]]`` first,
        # so the streaming branch handles dispatch and the fallback
        # never engages.
        runner = _build_runner(
            stream_tokens=[
                "[[reaction:", "cheerful]] ", "hi!", " how are you?",
            ],
        )
        tts_chunks: list[tuple[str, str]] = []
        result = runner.run(
            session_key="default:main",
            user_text="hello",
            on_tts_chunk=lambda text, mood: tts_chunks.append((text, mood)),
        )
        self.assertFalse(result.mood_fallback)
        self.assertEqual(result.reaction, "cheerful")
        self.assertTrue(tts_chunks, "happy path still feeds TTS")
        # All chunks should carry the parsed reaction, not "neutral".
        for _text, mood in tts_chunks:
            self.assertEqual(mood, "cheerful")
        # Reaction tag must be stripped from the persisted body.
        self.assertNotIn("[[reaction:", result.text)

    def test_normal_reaction_flow_dispatches_outfit_in_streaming_branch(
        self,
    ) -> None:
        # When reaction is present the per-chunk dispatch path handles
        # tags inline rather than the end-of-stream fallback. The
        # outfit callback must still fire exactly once.
        runner = _build_runner(
            stream_tokens=[
                "[[reaction:cheerful]] ",
                "Sure! [[outfit:day]] ",
                "all set.",
            ],
        )
        outfits: list[str] = []
        result = runner.run(
            session_key="default:main",
            user_text="change",
            on_tts_chunk=lambda text, mood: None,
            on_outfit=lambda name: outfits.append(name),
        )
        self.assertEqual(outfits, ["day"])
        self.assertFalse(result.mood_fallback)
        self.assertEqual(result.reaction, "cheerful")


class MoodFallbackAbortTests(unittest.TestCase):
    """The fallback must respect ``aborted`` — the existing
    ``not aborted`` guard at the final-flush keeps user-cancelled
    turns silent even when ``mood`` is forcibly populated."""

    def test_aborted_turn_does_not_speak_on_fallback(self) -> None:
        # Simulate the user pressing stop after the first delta — the
        # ``stop_requested`` predicate flips the ``aborted`` flag and
        # the loop breaks before any subsequent dispatch. The mood
        # fallback still sets ``mood='neutral'`` so the metrics log
        # works, but TTS must stay silent.
        stop_calls = {"count": 0}

        def stop_after_first_delta() -> bool:
            # First call returns False so the loop sees one delta;
            # second returns True so the loop aborts on the next
            # iteration.
            stop_calls["count"] += 1
            return stop_calls["count"] >= 2

        runner = _build_runner(
            stream_tokens=["partial ", "response ", "would have", " continued"],
        )
        tts_chunks: list[tuple[str, str]] = []
        outfits: list[str] = []

        result = runner.run(
            session_key="default:main",
            user_text="hi",
            on_tts_chunk=lambda text, mood: tts_chunks.append((text, mood)),
            on_outfit=lambda name: outfits.append(name),
            stop_requested=stop_after_first_delta,
        )
        self.assertTrue(result.aborted, "stop predicate must mark the turn aborted")
        self.assertEqual(
            tts_chunks, [],
            "aborted turn must not speak even when mood fallback engaged",
        )
        self.assertEqual(
            outfits, [],
            "aborted turn must not dispatch side-channel callbacks",
        )


class RawResponseLoggingTests(unittest.TestCase):
    """Turn-runner now emits an INFO ``llm raw response: ...`` line and
    a tag-presence summary so we can debug grammar compliance without
    a debugger."""

    def test_raw_response_log_emitted_once_per_turn(self) -> None:
        runner = _build_runner(stream_tokens=["hello", " world"])
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(
                session_key="default:main",
                user_text="hi",
                on_tts_chunk=lambda text, mood: None,
            )
        raw_lines = [r for r in cm.output if "llm raw response:" in r]
        self.assertEqual(
            len(raw_lines), 1,
            "exactly one raw-response log line per turn",
        )
        self.assertIn("hello world", raw_lines[0])

    def test_tag_summary_log_reflects_present_tags(self) -> None:
        runner = _build_runner(
            stream_tokens=[
                "[[reaction:cheerful]] ", "[[outfit:day]] ", "done!",
            ],
        )
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(
                session_key="default:main",
                user_text="hi",
                on_tts_chunk=lambda text, mood: None,
            )
        tag_lines = [r for r in cm.output if "llm tags:" in r]
        self.assertEqual(len(tag_lines), 1)
        line = tag_lines[0]
        self.assertIn("reaction=Y", line)
        self.assertIn("outfit=Y", line)
        self.assertIn("motion=n", line)
        self.assertIn("overlay=n", line)

    def test_tag_summary_log_reflects_absent_tags(self) -> None:
        runner = _build_runner(stream_tokens=["plain reply"])
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(
                session_key="default:main",
                user_text="hi",
                on_tts_chunk=lambda text, mood: None,
            )
        tag_lines = [r for r in cm.output if "llm tags:" in r]
        self.assertEqual(len(tag_lines), 1)
        line = tag_lines[0]
        for kind in ("reaction", "outfit", "motion", "overlay", "remember"):
            self.assertIn(f"{kind}=n", line)


if __name__ == "__main__":
    unittest.main()

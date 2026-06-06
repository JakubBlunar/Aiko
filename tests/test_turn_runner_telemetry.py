"""Tests for P1 + P2 telemetry on :class:`app.core.session.turn_runner.TurnRunner`.

P1 (perf backlog): ``TurnRunner`` wraps each turn in
``Embedder.begin_turn`` / ``end_turn`` and stamps the resulting
``(calls, ms)`` onto ``result.telemetry.embed_calls`` /
``embed_ms``. This test file pins that contract end-to-end against
a stub embedder so the data flow is verified without touching
Ollama.

P2 (perf backlog): the ``turn done:`` INFO log line gained four new
fields (``embed_calls`` / ``embed_ms`` / ``assemble_ms`` /
``rag_lookup_ms``); the ``prompt built:`` DEBUG line dropped the
hardcoded 10 inner-blocks counter in favour of live-provider
counts. We assert on both via ``self.assertLogs`` so a regression
surfaces here, not in a manual log dive.
"""
from __future__ import annotations

import logging
import time
import unittest
from typing import Any
from unittest.mock import MagicMock

import numpy as np

from app.core.session.prompt_assembler import PromptTelemetry
from app.core.session.turn_runner import TurnRunner
from app.llm.ollama_client import OllamaUsage


class _StubEmbedder:
    """Mimics the relevant surface of :class:`app.llm.embedder.Embedder`.

    Tracks how many ``begin_turn`` / ``end_turn`` pairs have been
    completed and what the last ``end_turn`` returned, so the test
    can assert turn boundaries are respected even if the prompt
    assembler is mocked out (i.e. no real embed calls happen).
    """

    def __init__(
        self,
        *,
        end_turn_result: tuple[int, float] = (3, 12.5),
    ) -> None:
        self._end_turn_result = end_turn_result
        self.begin_calls = 0
        self.end_calls = 0
        self._active = False

    def begin_turn(self) -> None:
        self.begin_calls += 1
        self._active = True

    def end_turn(self) -> tuple[int, float]:
        self.end_calls += 1
        if not self._active:
            return (0, 0.0)
        self._active = False
        return self._end_turn_result

    def embed(self, text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


def _build_runner(
    *,
    stream_tokens: list[str] | None = None,
    embedder: _StubEmbedder | None = None,
    telemetry: PromptTelemetry | None = None,
) -> tuple[TurnRunner, _StubEmbedder, MagicMock]:
    """Construct a ``TurnRunner`` with all real-world deps mocked.

    Returns the runner plus the embedder + prompt-assembler mocks
    so callers can assert on them directly.
    """
    if stream_tokens is None:
        stream_tokens = ["Hello there."]
    ollama = MagicMock()
    ollama.chat_stream = MagicMock(return_value=iter(stream_tokens))
    ollama.last_usage = OllamaUsage()

    db = MagicMock()
    db.add_message = MagicMock(return_value=1)

    prompt = MagicMock()
    prompt.assemble_with_budget = MagicMock(
        return_value=([], telemetry or PromptTelemetry()),
    )

    embedder = embedder or _StubEmbedder()
    runner = TurnRunner(
        ollama=ollama,
        db=db,
        prompt_assembler=prompt,
        model="test-model",
        context_window=8192,
        max_tokens=512,
        temperature=0.7,
        filler_enabled=False,
        embedder=embedder,  # type: ignore[arg-type]
    )
    return runner, embedder, prompt


class EmbedTurnBoundaryTests(unittest.TestCase):
    """P1: TurnRunner brackets each turn with begin/end_turn and
    stamps the resulting embed budget onto the telemetry."""

    def test_telemetry_is_stamped_with_embed_counters(self) -> None:
        embedder = _StubEmbedder(end_turn_result=(7, 42.5))
        runner, _, _ = _build_runner(embedder=embedder)
        result = runner.run(session_key="default:main", user_text="hi")
        self.assertIsNotNone(result.telemetry)
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.embed_calls, 7)
        self.assertAlmostEqual(result.telemetry.embed_ms, 42.5, places=2)

    def test_begin_and_end_called_exactly_once_per_turn(self) -> None:
        # ``run()`` calls ``end_turn`` defensively in its finally; the
        # actual stamping happens inside ``_run_inner`` so we expect
        # *two* end_turn calls per turn: one stamps the counters, the
        # second is a no-op (returns 0, 0.0 because the turn is no
        # longer active). This is the contract that keeps thread-local
        # state clean even if a future refactor moves the stamp
        # somewhere else.
        embedder = _StubEmbedder()
        runner, _, _ = _build_runner(embedder=embedder)
        runner.run(session_key="default:main", user_text="hi")
        self.assertEqual(embedder.begin_calls, 1)
        self.assertEqual(embedder.end_calls, 2)

    def test_end_turn_runs_even_when_assemble_raises(self) -> None:
        # If ``assemble_with_budget`` blows up the embedder must still
        # be released; otherwise the next turn on the same thread
        # double-counts an old turn's embeds. The stamp inside
        # ``_run_inner`` never runs (we crash before reaching it), so
        # only the public ``run()`` finally calls end_turn -- exactly
        # once.
        embedder = _StubEmbedder()
        runner, _embedder, prompt = _build_runner(embedder=embedder)
        prompt.assemble_with_budget.side_effect = RuntimeError("explode")
        with self.assertRaises(RuntimeError):
            runner.run(session_key="default:main", user_text="hi")
        self.assertEqual(embedder.begin_calls, 1)
        self.assertEqual(embedder.end_calls, 1)

    def test_no_embedder_means_zero_stamped_silently(self) -> None:
        # A SessionController that didn't construct an Embedder
        # (e.g. an ollama-less test environment) must still get a
        # telemetry with zero embed counters, not a crash.
        ollama = MagicMock()
        ollama.chat_stream = MagicMock(return_value=iter(["hi"]))
        ollama.last_usage = OllamaUsage()
        db = MagicMock()
        db.add_message = MagicMock(return_value=1)
        prompt = MagicMock()
        prompt.assemble_with_budget = MagicMock(
            return_value=([], PromptTelemetry()),
        )
        runner = TurnRunner(
            ollama=ollama,
            db=db,
            prompt_assembler=prompt,
            model="test-model",
            context_window=8192,
            max_tokens=512,
            temperature=0.7,
            filler_enabled=False,
            embedder=None,
        )
        result = runner.run(session_key="default:main", user_text="hi")
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.embed_calls, 0)
        self.assertEqual(result.telemetry.embed_ms, 0.0)

    def test_early_return_for_empty_user_text_leaves_telemetry_none(self) -> None:
        # ``run`` early-returns a ``TurnResult(text="", reaction="neutral")``
        # before assembling for empty-after-sanitize input. There's no
        # telemetry to stamp; the test pins that the runner doesn't try
        # to read attributes on a None telemetry.
        embedder = _StubEmbedder()
        runner, _, _ = _build_runner(embedder=embedder)
        result = runner.run(session_key="default:main", user_text="   ")
        # Sanitised user_text == "" -> early return, telemetry stays None.
        self.assertIsNone(result.telemetry)
        # The begin/end pair still ran (from the public ``run``)
        # so a future turn on the same thread starts cold.
        self.assertEqual(embedder.begin_calls, 1)
        self.assertEqual(embedder.end_calls, 1)


class TurnDoneLogFieldsTests(unittest.TestCase):
    """P1+P2: the ``turn done:`` INFO log line carries the new
    embed_calls / embed_ms / assemble_ms / rag_lookup_ms fields so
    a single grep over recent turns surfaces regressions."""

    def test_log_line_includes_new_fields(self) -> None:
        # Build telemetry pre-populated with the phase fields, then
        # have the embedder stamp embed_calls/embed_ms onto it.
        telem = PromptTelemetry(
            assemble_ms=12.34,
            rag_lookup_ms=5.67,
        )
        embedder = _StubEmbedder(end_turn_result=(4, 18.9))
        runner, _, _ = _build_runner(
            embedder=embedder,
            telemetry=telem,
        )
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(session_key="default:main", user_text="hi")
        haystack = "\n".join(cm.output)
        self.assertIn("turn done:", haystack)
        self.assertIn("embed_calls=4", haystack)
        # Numeric formatting may vary by locale; just check the prefix.
        self.assertIn("embed_ms=", haystack)
        self.assertIn("assemble_ms=", haystack)
        self.assertIn("rag_lookup_ms=", haystack)


class TurnDoneCachedFieldsTests(unittest.TestCase):
    """Prompt-caching observability: the ``turn done:`` INFO log line
    must carry both ``cached=`` (absolute prompt tokens that hit the
    provider prefix cache) and ``cached_pct=`` (hit-rate). See
    ``docs/prompt-caching.md`` for the prefix-stability contract that
    drives these numbers up on consecutive OpenAI turns.
    """

    def test_log_includes_cached_and_cached_pct_with_zero_default(self) -> None:
        # Cold turn (or non-OpenAI provider): cached_tokens stays at 0
        # but the FIELD must still appear in the log so a grep over
        # historical turns reads consistently.
        usage = OllamaUsage(prompt_tokens=1000, completion_tokens=42)
        runner, _, _ = _build_runner()
        runner._ollama.last_usage = usage  # type: ignore[attr-defined]
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(session_key="default:main", user_text="hi")
        haystack = "\n".join(cm.output)
        self.assertIn("turn done:", haystack)
        self.assertIn("cached=0", haystack)
        self.assertIn("cached_pct=0.0", haystack)

    def test_log_includes_cached_and_cached_pct_when_warm(self) -> None:
        # Warm OpenAI turn: 800 of 1000 prompt tokens hit the prefix
        # cache -> cached_pct = 80.0. Values come from
        # ``ChatUsage.cached_tokens_pct`` (which rounds to one
        # decimal); a regression in the formatter or the field
        # surfaces here before reaching prod.
        usage = OllamaUsage(
            prompt_tokens=1000,
            completion_tokens=42,
            cached_tokens=800,
        )
        runner, _, _ = _build_runner()
        runner._ollama.last_usage = usage  # type: ignore[attr-defined]
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(session_key="default:main", user_text="hi")
        haystack = "\n".join(cm.output)
        self.assertIn("turn done:", haystack)
        self.assertIn("cached=800", haystack)
        self.assertIn("cached_pct=80.0", haystack)


if __name__ == "__main__":
    unittest.main()

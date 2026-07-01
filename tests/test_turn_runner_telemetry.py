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
    summary_worker: Any | None = None,
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
        summary_worker=summary_worker,
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


class _StubSummaryWorker:
    """Records which compaction path TurnRunner took."""

    def __init__(self) -> None:
        self.compact_now_calls: list[str] = []
        self.notify_soon_calls: list[str] = []

    def compact_now(self, session_key: str) -> bool:
        self.compact_now_calls.append(session_key)
        return True

    def notify_compaction_soon(self, session_key: str) -> None:
        self.notify_soon_calls.append(session_key)

    def notify_turn_done(self, session_key: str) -> None:
        pass


class DeferredCompactionTests(unittest.TestCase):
    """P20: projected overflow no longer runs the summariser LLM inline.

    It must reassemble aggressively (so the turn still fits) and push the
    background-compaction deadline forward, never blocking first token on
    a synchronous ``compact_now``.
    """

    def test_overflow_schedules_async_and_skips_compact_now(self) -> None:
        summary = _StubSummaryWorker()
        # First assembly reports overflow; the aggressive reassembly is
        # the second return value (no overflow).
        overflow = PromptTelemetry(
            compaction_triggered=True,
            prompt_tokens_estimate=9000,
            budget_tokens=7000,
        )
        fitted = PromptTelemetry(compaction_triggered=False)
        runner, _, prompt = _build_runner(summary_worker=summary)
        prompt.assemble_with_budget = MagicMock(
            side_effect=[([], overflow), ([], fitted)],
        )
        runner.run(session_key="default:main", user_text="hi")
        # The summariser LLM (compact_now) must NOT have run on the hot path.
        self.assertEqual(summary.compact_now_calls, [])
        # The background compaction must have been scheduled.
        self.assertIn("default:main", summary.notify_soon_calls)
        # The aggressive reassembly must have run (second assemble call
        # with aggressive=True).
        self.assertEqual(prompt.assemble_with_budget.call_count, 2)
        _, kwargs = prompt.assemble_with_budget.call_args_list[1]
        self.assertTrue(kwargs.get("aggressive"))

    def test_no_overflow_does_not_touch_summary(self) -> None:
        summary = _StubSummaryWorker()
        runner, _, prompt = _build_runner(
            summary_worker=summary,
            telemetry=PromptTelemetry(compaction_triggered=False),
        )
        runner.run(session_key="default:main", user_text="hi")
        self.assertEqual(summary.compact_now_calls, [])
        self.assertEqual(summary.notify_soon_calls, [])
        # Only the initial assembly; no aggressive reassembly.
        self.assertEqual(prompt.assemble_with_budget.call_count, 1)

    def test_overflow_compactions_run_stays_zero(self) -> None:
        summary = _StubSummaryWorker()
        overflow = PromptTelemetry(
            compaction_triggered=True,
            prompt_tokens_estimate=9000,
            budget_tokens=7000,
        )
        fitted = PromptTelemetry(compaction_triggered=False)
        runner, _, prompt = _build_runner(summary_worker=summary)
        prompt.assemble_with_budget = MagicMock(
            side_effect=[([], overflow), ([], fitted)],
        )
        result = runner.run(session_key="default:main", user_text="hi")
        # No synchronous compaction happened, so the counter is 0.
        self.assertEqual(result.compactions_run, 0)

    def test_overflow_refits_even_without_summary_worker(self) -> None:
        """WS3 bug: the aggressive reassembly must run on projected overflow
        even when no SummaryWorker is attached — otherwise the overflowing
        prompt would be sent to the model un-trimmed.
        """
        overflow = PromptTelemetry(
            compaction_triggered=True,
            prompt_tokens_estimate=9000,
            budget_tokens=7000,
        )
        fitted = PromptTelemetry(compaction_triggered=False)
        # summary_worker=None (default).
        runner, _, prompt = _build_runner()
        prompt.assemble_with_budget = MagicMock(
            side_effect=[([], overflow), ([], fitted)],
        )
        runner.run(session_key="default:main", user_text="hi")
        # The aggressive reassembly still ran (second call, aggressive=True)
        # despite there being no summary worker to schedule.
        self.assertEqual(prompt.assemble_with_budget.call_count, 2)
        _, kwargs = prompt.assemble_with_budget.call_args_list[1]
        self.assertTrue(kwargs.get("aggressive"))


class ToolPassRetrimTests(unittest.TestCase):
    """WS3 hardening: after the tool pass appends tool-result messages the
    prompt can overflow again; ``_retrim_messages_to_budget`` drops the oldest
    raw history in place while preserving the system prompt, the current user
    message, and the whole tool exchange.
    """

    def _msg(self, role: str, content: str, **extra: Any) -> dict[str, Any]:
        m: dict[str, Any] = {"role": role, "content": content}
        m.update(extra)
        return m

    def test_retrim_drops_oldest_history_and_keeps_tool_exchange(self) -> None:
        big = "word " * 400  # ~570 chars each
        messages = [
            self._msg("system", "S"),
            self._msg("user", big),          # oldest history (droppable)
            self._msg("assistant", big),     # history (droppable)
            self._msg("user", "current turn"),  # current user message (keep)
            self._msg("assistant", "", tool_calls=[{"id": "1"}]),  # tool exchange
            self._msg("tool", big, tool_call_id="1"),
        ]
        total, dropped = TurnRunner._retrim_messages_to_budget(
            messages, budget_tokens=300,
        )
        # Something was dropped from the head of history.
        self.assertGreater(dropped, 0)
        # System prompt survives at index 0.
        self.assertEqual(messages[0]["role"], "system")
        # The current user message and the tool exchange are preserved.
        contents = [m["content"] for m in messages]
        self.assertIn("current turn", contents)
        self.assertTrue(any(m.get("role") == "tool" for m in messages))
        self.assertTrue(
            any(m.get("tool_calls") for m in messages),
        )

    def test_retrim_noop_when_within_budget(self) -> None:
        messages = [
            self._msg("system", "S"),
            self._msg("user", "hi"),
            self._msg("assistant", "", tool_calls=[{"id": "1"}]),
            self._msg("tool", "small", tool_call_id="1"),
        ]
        before = list(messages)
        total, dropped = TurnRunner._retrim_messages_to_budget(
            messages, budget_tokens=100000,
        )
        self.assertEqual(dropped, 0)
        self.assertEqual(messages, before)

    def test_retrim_never_drops_below_system_and_user(self) -> None:
        # Only system + current user + tool exchange, no droppable history.
        big = "word " * 400
        messages = [
            self._msg("system", big),
            self._msg("user", big),  # current user message
            self._msg("assistant", "", tool_calls=[{"id": "1"}]),
            self._msg("tool", big, tool_call_id="1"),
        ]
        _, dropped = TurnRunner._retrim_messages_to_budget(
            messages, budget_tokens=10,
        )
        # Nothing droppable (index 1 is the current user turn), so 0 dropped
        # and the message list is intact.
        self.assertEqual(dropped, 0)
        self.assertEqual(len(messages), 4)


if __name__ == "__main__":
    unittest.main()

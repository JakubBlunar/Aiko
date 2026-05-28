"""Truncation observability for :mod:`app.llm.ollama_client`.

Ollama signals a truncated response by setting ``done_reason="length"``
on the final chunk / non-streaming body — the response was cut off
because we hit the configured ``num_predict`` cap. The client now
captures this onto :class:`OllamaUsage` and emits a single ``WARNING``
log line so we can spot truncation in the wild without having to
diff token counts by hand.

These tests cover the three call paths (`chat_with_tools`,
`chat_stream`, `chat_json`) for both the truncated and clean-stop
cases.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from app.core.settings import load_settings
from app.llm.ollama_client import OllamaClient


def _stream_lines(chunks: list[dict[str, object]]) -> list[str]:
    """Render a list of dicts as Ollama-style JSON-per-line bytes."""
    return [json.dumps(chunk) for chunk in chunks]


class _StreamResponseStub:
    """Minimal stand-in for ``requests.Response`` returned by a
    streaming POST. The real client uses it as a context manager
    (``with requests.post(..., stream=True) as response:``) so we
    have to satisfy ``__enter__`` / ``__exit__`` plus
    ``iter_lines`` / ``ok`` / ``raise_for_status`` / ``close``.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.ok = True

    def __enter__(self) -> "_StreamResponseStub":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = True):
        for line in self._lines:
            yield line

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class ChatWithToolsTruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def _make_response(self, *, done_reason: str | None) -> Mock:
        body: dict[str, object] = {
            "message": {"content": "partial answer"},
            "prompt_eval_count": 100,
            "eval_count": 512,
            "total_duration": 1_000_000_000,
            "eval_duration": 800_000_000,
            "prompt_eval_duration": 200_000_000,
        }
        if done_reason is not None:
            body["done_reason"] = done_reason
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        return fake

    def test_length_sets_done_reason_and_warns(self) -> None:
        client = OllamaClient(self._ollama_settings)
        fake = self._make_response(done_reason="length")
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_with_tools([{"role": "user", "content": "hi"}])
        self.assertEqual(client.last_usage.done_reason, "length")
        self.assertTrue(
            any("truncated" in record.getMessage() for record in cap.records),
            f"expected a 'truncated' WARNING, got {[r.getMessage() for r in cap.records]}",
        )

    def test_stop_is_silent(self) -> None:
        client = OllamaClient(self._ollama_settings)
        fake = self._make_response(done_reason="stop")
        # ``assertNoLogs`` lets us pin "no WARNING fired" without
        # having to scaffold a custom handler.
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ), self.assertNoLogs("app.llm.ollama_client", level="WARNING"):
            client.chat_with_tools([{"role": "user", "content": "hi"}])
        self.assertEqual(client.last_usage.done_reason, "stop")

    def test_missing_done_reason_is_silent(self) -> None:
        # Older Ollama servers (or proxied responses) may simply omit
        # the field. We must NOT fire a "truncated" warning in that
        # case — silence is the safe default.
        client = OllamaClient(self._ollama_settings)
        fake = self._make_response(done_reason=None)
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ), self.assertNoLogs("app.llm.ollama_client", level="WARNING"):
            client.chat_with_tools([{"role": "user", "content": "hi"}])
        self.assertIsNone(client.last_usage.done_reason)


class ChatStreamTruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def _stream_chunks(self, *, done_reason: str | None) -> list[dict[str, object]]:
        chunks: list[dict[str, object]] = [
            {"message": {"content": "partial "}, "done": False},
            {"message": {"content": "answer"}, "done": False},
        ]
        final: dict[str, object] = {
            "message": {"content": ""},
            "done": True,
            "prompt_eval_count": 100,
            "eval_count": 512,
            "total_duration": 1_000_000_000,
            "eval_duration": 800_000_000,
            "prompt_eval_duration": 200_000_000,
        }
        if done_reason is not None:
            final["done_reason"] = done_reason
        chunks.append(final)
        return chunks

    def test_length_sets_done_reason_and_warns(self) -> None:
        client = OllamaClient(self._ollama_settings)
        stub = _StreamResponseStub(
            _stream_lines(self._stream_chunks(done_reason="length")),
        )
        # ``requests.post(..., stream=True)`` returns the stub
        # directly; the client uses it as a context manager.
        with patch(
            "app.llm.ollama_client.requests.post", return_value=stub,
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            tokens = list(
                client.chat_stream([{"role": "user", "content": "hi"}]),
            )
        self.assertEqual("".join(tokens), "partial answer")
        self.assertEqual(client.last_usage.done_reason, "length")
        self.assertTrue(
            any("truncated" in record.getMessage() for record in cap.records),
        )

    def test_stop_is_silent(self) -> None:
        client = OllamaClient(self._ollama_settings)
        stub = _StreamResponseStub(
            _stream_lines(self._stream_chunks(done_reason="stop")),
        )
        with patch(
            "app.llm.ollama_client.requests.post", return_value=stub,
        ), self.assertNoLogs("app.llm.ollama_client", level="WARNING"):
            tokens = list(
                client.chat_stream([{"role": "user", "content": "hi"}]),
            )
        self.assertEqual("".join(tokens), "partial answer")
        self.assertEqual(client.last_usage.done_reason, "stop")


class ChatJsonTruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def _make_response(self, *, done_reason: str | None) -> Mock:
        body: dict[str, object] = {
            "message": {"content": '{"foo": "bar"}'},
            "prompt_eval_count": 50,
            "eval_count": 512,
            "total_duration": 800_000_000,
            "eval_duration": 600_000_000,
            "prompt_eval_duration": 100_000_000,
        }
        if done_reason is not None:
            body["done_reason"] = done_reason
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        return fake

    def test_length_sets_done_reason_and_warns(self) -> None:
        client = OllamaClient(self._ollama_settings)
        fake = self._make_response(done_reason="length")
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            content, usage = client.chat_json(
                [{"role": "user", "content": "hi"}],
            )
        self.assertEqual(content, '{"foo": "bar"}')
        self.assertEqual(usage.done_reason, "length")
        self.assertTrue(
            any("truncated" in record.getMessage() for record in cap.records),
        )

    def test_stop_is_silent(self) -> None:
        client = OllamaClient(self._ollama_settings)
        fake = self._make_response(done_reason="stop")
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ), self.assertNoLogs("app.llm.ollama_client", level="WARNING"):
            _content, usage = client.chat_json(
                [{"role": "user", "content": "hi"}],
            )
        self.assertEqual(usage.done_reason, "stop")


class UsageMergeTruncationTests(unittest.TestCase):
    """``OllamaUsage.merge`` is used to combine the tool pre-pass and
    the streaming reply pass into a single per-turn telemetry record.
    Truncation must propagate so a turn that hit the cap on either
    leg surfaces in the merged usage.
    """

    def test_merge_keeps_length_when_either_pass_truncated(self) -> None:
        from app.llm.ollama_client import OllamaUsage

        a = OllamaUsage(completion_tokens=200, done_reason="stop")
        b = OllamaUsage(completion_tokens=512, done_reason="length")
        merged = a.merge(b)
        self.assertEqual(merged.done_reason, "length")
        self.assertEqual(merged.completion_tokens, 712)

    def test_merge_keeps_stop_when_neither_truncated(self) -> None:
        from app.llm.ollama_client import OllamaUsage

        a = OllamaUsage(done_reason=None)
        b = OllamaUsage(done_reason="stop")
        merged = a.merge(b)
        self.assertEqual(merged.done_reason, "stop")


class SurfaceTagPropagationTests(unittest.TestCase):
    """Background workers route through several entry points
    (`chat`, `chat_with_tools`, `chat_stream`, `chat_json`) but
    operators only see the warning. To debug truncation we need to
    know which caller actually fired it, so each entry point now
    accepts an explicit ``surface=`` tag the caller supplies. These
    tests pin that the tag flows through to the warning text.
    """

    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def _truncated_chat_response(self) -> Mock:
        body = {
            "message": {"content": "partial"},
            "prompt_eval_count": 10,
            "eval_count": 256,
            "total_duration": 0,
            "eval_duration": 0,
            "prompt_eval_duration": 0,
            "done_reason": "length",
        }
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        return fake

    def _assert_surface_in_warning(self, cap, expected: str) -> None:
        msg = next(
            (r.getMessage() for r in cap.records if "truncated" in r.getMessage()),
            "",
        )
        self.assertIn(f"surface={expected}", msg, f"warning was: {msg!r}")

    def test_chat_default_surface_is_chat(self) -> None:
        # The thin ``chat`` wrapper used to mislabel everything as
        # ``chat_with_tools`` because it just forwarded to that
        # method. The default for the wrapper is now ``chat`` and
        # callers can override.
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_chat_response(),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat([{"role": "user", "content": "hi"}])
        self._assert_surface_in_warning(cap, "chat")

    def test_chat_explicit_surface_propagates(self) -> None:
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_chat_response(),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat(
                [{"role": "user", "content": "hi"}],
                surface="summary_worker",
            )
        self._assert_surface_in_warning(cap, "summary_worker")

    def test_chat_with_tools_explicit_surface_propagates(self) -> None:
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_chat_response(),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="reflection_worker",
            )
        self._assert_surface_in_warning(cap, "reflection_worker")

    def test_chat_json_explicit_surface_propagates(self) -> None:
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_chat_response(),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_json(
                [{"role": "user", "content": "hi"}],
                surface="memory_extractor",
            )
        self._assert_surface_in_warning(cap, "memory_extractor")

    def test_chat_stream_explicit_surface_propagates(self) -> None:
        client = OllamaClient(self._ollama_settings)
        chunks = [
            {"message": {"content": "partial"}, "done": False},
            {
                "message": {"content": ""},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 256,
                "total_duration": 0,
                "eval_duration": 0,
                "prompt_eval_duration": 0,
                "done_reason": "length",
            },
        ]
        stub = _StreamResponseStub(_stream_lines(chunks))
        with patch(
            "app.llm.ollama_client.requests.post", return_value=stub,
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            list(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    surface="belief_worker",
                ),
            )
        self._assert_surface_in_warning(cap, "belief_worker")


class BenignTruncationSurfaceTests(unittest.TestCase):
    """``tool_pass`` deliberately caps ``num_predict`` short — we
    discard the prose and only consume tool calls — so a truncated
    tool pass is the *expected* shape, not a problem. The warning
    must stay silent for these benign-by-design surfaces but still
    fire for any other surface that hits the cap.
    """

    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def _make_response(self, *, done_reason: str | None) -> Mock:
        body: dict[str, object] = {
            "message": {"content": "ok"},
            "prompt_eval_count": 1,
            "eval_count": 256,
            "total_duration": 0,
            "eval_duration": 0,
            "prompt_eval_duration": 0,
        }
        if done_reason is not None:
            body["done_reason"] = done_reason
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        return fake

    def test_tool_pass_truncation_does_not_warn(self) -> None:
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._make_response(done_reason="length"),
        ), self.assertNoLogs("app.llm.ollama_client", level="WARNING"):
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="tool_pass",
            )
        # Truncation is still recorded on usage (so merge / metrics
        # see it); we only suppress the operator-facing warning.
        self.assertEqual(client.last_usage.done_reason, "length")

    def test_other_surface_still_warns(self) -> None:
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._make_response(done_reason="length"),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="self_image_worker",
            )
        msg = next((r.getMessage() for r in cap.records), "")
        self.assertIn("surface=self_image_worker", msg)


class StripThinkingBlocksTests(unittest.TestCase):
    """Reasoning models (qwen3.x, deepseek-r1, etc.) sometimes leak
    ``<think>...</think>`` blocks into ``message.content`` even when
    we pass ``think=False``. The non-streaming entry points strip
    those blocks before returning so workers don't have to do it
    themselves and don't waste budget storing the trace.
    """

    def test_strip_balanced_block(self) -> None:
        from app.llm.ollama_client import strip_thinking_blocks

        text = (
            "<think>weighing the options here, settling on a tone</think>"
            "Hi there, how are you?"
        )
        self.assertEqual(strip_thinking_blocks(text), "Hi there, how are you?")

    def test_strip_unclosed_block(self) -> None:
        # Truncation can chop off the closing tag. Drop the rest.
        from app.llm.ollama_client import strip_thinking_blocks

        text = (
            "Hello.\n<think>this is reasoning that never closes because we"
            " hit the cap"
        )
        self.assertEqual(strip_thinking_blocks(text), "Hello.")

    def test_strip_thinking_alias(self) -> None:
        from app.llm.ollama_client import strip_thinking_blocks

        text = "<thinking>plotting</thinking>Final answer here."
        self.assertEqual(strip_thinking_blocks(text), "Final answer here.")

    def test_no_change_when_no_marker(self) -> None:
        from app.llm.ollama_client import strip_thinking_blocks

        text = "A normal answer with no thinking trace."
        self.assertEqual(strip_thinking_blocks(text), text)

    def test_chat_with_tools_strips_when_think_false(self) -> None:
        client = OllamaClient(load_settings().ollama)
        body = {
            "message": {"content": "<think>plot</think>real reply"},
            "prompt_eval_count": 1,
            "eval_count": 5,
            "total_duration": 0,
            "eval_duration": 0,
            "prompt_eval_duration": 0,
        }
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ):
            response = client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
            )
        self.assertEqual(response.content, "real reply")

    def test_chat_with_tools_keeps_thinking_when_think_true(self) -> None:
        # When the caller explicitly asks for the trace, don't second-guess.
        client = OllamaClient(load_settings().ollama)
        original = "<think>plot</think>real reply"
        body = {
            "message": {"content": original},
            "prompt_eval_count": 1,
            "eval_count": 5,
            "total_duration": 0,
            "eval_duration": 0,
            "prompt_eval_duration": 0,
        }
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ):
            response = client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                think=True,
            )
        self.assertEqual(response.content, original)

    def test_chat_json_strips_when_think_false(self) -> None:
        client = OllamaClient(load_settings().ollama)
        body = {
            "message": {
                "content": '<think>compose JSON</think>{"ok": true}',
            },
            "prompt_eval_count": 1,
            "eval_count": 5,
            "total_duration": 0,
            "eval_duration": 0,
            "prompt_eval_duration": 0,
        }
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ):
            content, _usage = client.chat_json(
                [{"role": "user", "content": "hi"}],
            )
        self.assertEqual(content, '{"ok": true}')


class BenignThinkingTruncationTests(unittest.TestCase):
    """Reasoning models often hit ``num_predict`` on their hidden
    ``<think>...</think>`` trace while the visible answer reaches a
    natural stop. ``done_reason="length"`` then fires even though
    nothing is wrong with the saved content. The client recognises
    that pattern (``had_thinking AND content_looks_complete``) and
    downgrades the WARNING to a DEBUG line, leaving real truncations
    (cut-off content, no thinking trace) loud.
    """

    def setUp(self) -> None:
        self._ollama_settings = load_settings().ollama

    def _truncated_with_content(self, content: str) -> Mock:
        body: dict[str, object] = {
            "message": {"content": content},
            "prompt_eval_count": 1,
            "eval_count": 320,
            "total_duration": 0,
            "eval_duration": 0,
            "prompt_eval_duration": 0,
            "done_reason": "length",
        }
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        return fake

    def test_thinking_plus_complete_answer_does_not_warn(self) -> None:
        # The model burned the budget on a thinking trace but the
        # visible answer ends with a period — operator should not see
        # a WARNING for this; it's purely a tuning hint.
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_with_content(
                "<think>weighing how to phrase this neatly</think>"
                "I see myself as steady and curious right now."
            ),
        ), self.assertNoLogs("app.llm.ollama_client", level="WARNING"):
            response = client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="self_image_worker",
            )
        # Visible answer is the post-strip content, untouched.
        self.assertEqual(
            response.content,
            "I see myself as steady and curious right now.",
        )
        # Truncation is still recorded so merge / metrics see it.
        self.assertEqual(client.last_usage.done_reason, "length")

    def test_thinking_plus_cut_off_answer_still_warns(self) -> None:
        # Trace was stripped but the answer ends mid-clause — that's
        # a real truncation we want surfaced.
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_with_content(
                "<think>plotting</think>"
                "I think the most important thing is that we keep"
            ),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="self_image_worker",
            )
        msg = next((r.getMessage() for r in cap.records), "")
        self.assertIn("surface=self_image_worker", msg)

    def test_no_thinking_truncation_still_warns(self) -> None:
        # Plain truncation with no thinking trace at all — same loud
        # warning as before.
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_with_content(
                "the cap chopped this mid"
            ),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="reflection_worker",
            )
        msg = next((r.getMessage() for r in cap.records), "")
        self.assertIn("surface=reflection_worker", msg)

    def test_thinking_swallows_entire_response(self) -> None:
        # Pathological case: thinking ate the whole budget so the
        # visible answer is empty. That's still a real failure to warn
        # about — we never got a usable answer.
        client = OllamaClient(self._ollama_settings)
        with patch(
            "app.llm.ollama_client.requests.post",
            return_value=self._truncated_with_content(
                "<think>still thinking and never reaching an answer"
            ),
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                surface="dream_worker",
            )
        msg = next((r.getMessage() for r in cap.records), "")
        self.assertIn("surface=dream_worker", msg)


class ContentLooksCompleteTests(unittest.TestCase):
    """Pin the heuristic that decides "answer reached a natural stop"
    so the benign-truncation downgrade only fires when we're confident.
    """

    def test_terminator_punctuation_is_complete(self) -> None:
        from app.llm.ollama_client import _content_looks_complete

        for ending in (".", "!", "?", "…", '"', "'", ")", "]", "}"):
            self.assertTrue(
                _content_looks_complete(f"answer{ending}"),
                f"expected complete for ending={ending!r}",
            )

    def test_trailing_whitespace_tolerated(self) -> None:
        from app.llm.ollama_client import _content_looks_complete

        self.assertTrue(_content_looks_complete("answer.\n"))
        self.assertTrue(_content_looks_complete("answer.    "))

    def test_mid_clause_is_incomplete(self) -> None:
        from app.llm.ollama_client import _content_looks_complete

        self.assertFalse(_content_looks_complete("the cap chopped this mid"))
        self.assertFalse(_content_looks_complete("answer ending in,"))
        self.assertFalse(_content_looks_complete(""))


if __name__ == "__main__":
    unittest.main()

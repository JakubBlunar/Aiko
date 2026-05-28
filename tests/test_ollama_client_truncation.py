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


if __name__ == "__main__":
    unittest.main()

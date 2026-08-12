"""The no-think retry guard in :mod:`app.llm.ollama_client`.

Measured failure this pins: with ``think=True`` and a ``num_predict``
sized for the answer, a 27B reasoner can spend the *entire* budget on its
hidden trace and emit no answer token at all. Ollama reports
``done_reason="length"`` with an empty ``message.content``. 96 worker
calls across 10 surfaces did exactly this, and because an empty answer
looks like a well-formed "nothing to report" to most parsers, the affected
features reported success and did nothing.

``think_num_predict_headroom`` (raised to 8192) is the primary defence.
This is the backstop: when a call is starved anyway, re-issue it once with
thinking off rather than hand the caller an empty string.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from app.core.infra.settings import load_settings
from app.llm.ollama_client import OllamaClient, OllamaUsage, _thinking_starved


class _StreamStub:
    """Stand-in for a streaming ``requests.Response``."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.ok = True

    def __enter__(self) -> "_StreamStub":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = True):
        yield from self._lines

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


def _starved_stream() -> list[str]:
    """A stream that ends at the length cap having emitted no content."""
    return [
        json.dumps({"message": {"content": ""}}),
        json.dumps({
            "done": True,
            "done_reason": "length",
            "eval_count": 2448,
            "prompt_eval_count": 900,
        }),
    ]


def _good_stream(text: str) -> list[str]:
    return [
        json.dumps({"message": {"content": text}}),
        json.dumps({
            "done": True,
            "done_reason": "stop",
            "eval_count": 12,
            "prompt_eval_count": 900,
        }),
    ]


class PredicateTests(unittest.TestCase):
    def test_length_cap_with_empty_answer_is_starvation(self) -> None:
        usage = OllamaUsage(done_reason="length")
        self.assertTrue(_thinking_starved(think=True, content="", usage=usage))

    def test_whitespace_only_answer_counts_as_empty(self) -> None:
        usage = OllamaUsage(done_reason="length")
        self.assertTrue(
            _thinking_starved(think=True, content="  \n ", usage=usage),
        )

    def test_a_real_truncated_answer_is_not_starvation(self) -> None:
        # The answer started and got cut. That's ordinary truncation and
        # the caller's salvage/parse path owns it.
        usage = OllamaUsage(done_reason="length")
        self.assertFalse(
            _thinking_starved(think=True, content='{"promises": [{', usage=usage),
        )

    def test_clean_stop_is_never_starvation(self) -> None:
        usage = OllamaUsage(done_reason="stop")
        self.assertFalse(_thinking_starved(think=True, content="", usage=usage))

    def test_think_off_is_never_starvation(self) -> None:
        # Without a trace there is nothing to starve the answer; an empty
        # answer at the cap means something else entirely.
        usage = OllamaUsage(done_reason="length")
        self.assertFalse(_thinking_starved(think=False, content="", usage=usage))


class StreamRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._settings = load_settings().ollama

    def test_starved_stream_retries_without_thinking(self) -> None:
        client = OllamaClient(self._settings)
        responses = [
            _StreamStub(_starved_stream()),
            _StreamStub(_good_stream('{"promises": []}')),
        ]
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return responses.pop(0)

        with patch(
            "app.llm.ollama_client.requests.post", side_effect=fake_post,
        ), self.assertLogs("app.llm.ollama_client", level="WARNING") as cap:
            out = "".join(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    options={"num_predict": 400},
                    think=True,
                    surface="promise_worker",
                )
            )
        self.assertEqual(out, '{"promises": []}')
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["think"])
        self.assertFalse(calls[1]["think"])
        self.assertTrue(
            any("starved" in r.getMessage() for r in cap.records),
            f"expected a starvation WARNING, got {[r.getMessage() for r in cap.records]}",
        )

    def test_the_retry_keeps_the_json_format_and_model(self) -> None:
        client = OllamaClient(self._settings)
        responses = [
            _StreamStub(_starved_stream()),
            _StreamStub(_good_stream("{}")),
        ]
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return responses.pop(0)

        with patch("app.llm.ollama_client.requests.post", side_effect=fake_post):
            list(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    options={"num_predict": 400},
                    model="qwen3.6:27b",
                    format_json=True,
                    think=True,
                )
            )
        self.assertEqual(calls[1]["format"], "json")
        self.assertEqual(calls[1]["model"], "qwen3.6:27b")

    def test_a_partial_answer_is_never_retried(self) -> None:
        # Something was already yielded to the consumer, so a second pass
        # would duplicate output. Ordinary truncation handling applies.
        client = OllamaClient(self._settings)
        stream = [
            json.dumps({"message": {"content": '{"promises": [{'}}),
            json.dumps({"done": True, "done_reason": "length", "eval_count": 2448}),
        ]
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return _StreamStub(stream)

        with patch("app.llm.ollama_client.requests.post", side_effect=fake_post):
            out = "".join(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    options={"num_predict": 400},
                    think=True,
                )
            )
        self.assertEqual(out, '{"promises": [{')
        self.assertEqual(len(calls), 1)

    def test_a_clean_empty_stop_is_not_retried(self) -> None:
        # The model deliberately said nothing and stopped. Not our problem.
        client = OllamaClient(self._settings)
        stream = [
            json.dumps({"done": True, "done_reason": "stop", "eval_count": 5}),
        ]
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return _StreamStub(stream)

        with patch("app.llm.ollama_client.requests.post", side_effect=fake_post):
            list(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    options={"num_predict": 400},
                    think=True,
                )
            )
        self.assertEqual(len(calls), 1)

    def test_think_off_is_not_retried(self) -> None:
        client = OllamaClient(self._settings)
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return _StreamStub(_starved_stream())

        with patch("app.llm.ollama_client.requests.post", side_effect=fake_post):
            list(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    options={"num_predict": 400},
                    think=False,
                )
            )
        self.assertEqual(len(calls), 1)


class NonStreamingRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._settings = load_settings().ollama

    def _body(self, *, content: str, done_reason: str) -> dict:
        return {
            "message": {"content": content},
            "done_reason": done_reason,
            "eval_count": 2448,
            "prompt_eval_count": 900,
        }

    def _response(self, body: dict) -> Mock:
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = body
        return fake

    def test_chat_with_tools_retries_and_returns_the_answer(self) -> None:
        client = OllamaClient(self._settings)
        responses = [
            self._response(self._body(content="", done_reason="length")),
            self._response(self._body(content="the answer", done_reason="stop")),
        ]
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return responses.pop(0)

        with patch("app.llm.ollama_client.requests.post", side_effect=fake_post):
            out = client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                options={"num_predict": 400},
                think=True,
            )
        self.assertEqual(out.content, "the answer")
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1]["think"])
        self.assertEqual(client.last_usage.done_reason, "stop")

    def test_chat_json_retries(self) -> None:
        client = OllamaClient(self._settings)
        responses = [
            self._response(self._body(content="", done_reason="length")),
            self._response(
                self._body(content='{"memories": []}', done_reason="stop"),
            ),
        ]
        calls: list[dict] = []

        def fake_post(url: str, **kwargs: object):
            calls.append(dict(kwargs.get("json") or {}))
            return responses.pop(0)

        with patch("app.llm.ollama_client.requests.post", side_effect=fake_post):
            content, usage = client.chat_json(
                [{"role": "user", "content": "hi"}],
                options={"num_predict": 400},
                think=True,
            )
        self.assertEqual(content, '{"memories": []}')
        self.assertEqual(usage.done_reason, "stop")
        self.assertEqual(len(calls), 2)

    def test_a_failed_retry_leaves_the_original_empty_result(self) -> None:
        # The retry is best-effort. A transport failure must not raise on
        # top of the original problem.
        client = OllamaClient(self._settings)
        first = self._response(self._body(content="", done_reason="length"))
        failed = Mock()
        failed.ok = False
        failed.status_code = 500
        responses = [first, failed]

        with patch(
            "app.llm.ollama_client.requests.post",
            side_effect=lambda url, **kw: responses.pop(0),
        ):
            out = client.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                options={"num_predict": 400},
                think=True,
            )
        self.assertEqual(out.content, "")


class HeadroomTests(unittest.TestCase):
    def test_the_default_headroom_covers_an_observed_trace(self) -> None:
        # Traces measured at ~2.4k tokens blew the old 2048 default. The
        # new default must leave real room above that.
        settings = load_settings().ollama
        self.assertGreaterEqual(settings.think_num_predict_headroom, 8192)

    def test_headroom_is_added_to_the_answer_budget(self) -> None:
        client = OllamaClient(load_settings().ollama)
        options: dict[str, object] = {"num_predict": 400}
        client._apply_think_headroom(options, True, surface="promise_worker")
        self.assertEqual(
            options["num_predict"],
            400 + client._think_headroom,
        )


if __name__ == "__main__":
    unittest.main()

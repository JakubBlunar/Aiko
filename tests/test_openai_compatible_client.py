"""Tests for the OpenAI-compatible chat client.

Covers the wire-shape contract (request payload, SSE parsing, tool
calls, JSON mode, model listing), the truncation log gate, and the
Gemini system-role collapse quirk. The HTTP transport is mocked
throughout — these tests never hit a network.
"""

from __future__ import annotations

import json
import logging
import unittest
from collections.abc import Iterator
from unittest.mock import MagicMock, Mock, patch

from app.core.infra.settings import load_settings
from app.llm.openai_compatible_client import (
    OpenAICompatibleClient,
    _collapse_system_for_gemini,
    _is_gemini_model,
    _iter_sse_data_lines,
    _map_finish_reason,
)


def _fake_chat_response(
    *,
    content: str = "hello",
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
    usage: dict | None = None,
) -> Mock:
    """Build a ``requests.post`` mock for the non-streaming code path."""
    response = Mock()
    response.ok = True
    response.raise_for_status.return_value = None
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict = {
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    response.json.return_value = body
    return response


def _fake_models_response(model_ids: list[str]) -> Mock:
    response = Mock()
    response.ok = True
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": [{"id": m, "object": "model"} for m in model_ids],
    }
    return response


def _sse_response(lines: list[str]) -> Mock:
    response = MagicMock()
    response.ok = True
    response.raise_for_status.return_value = None
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    response.iter_lines.return_value = iter(lines)
    return response


class GeminiQuirkTests(unittest.TestCase):
    def test_is_gemini_model_matches_both_id_forms(self) -> None:
        self.assertTrue(_is_gemini_model("gemini-2.5-flash"))
        self.assertTrue(_is_gemini_model("models/gemini-2.5-flash-lite"))
        self.assertTrue(_is_gemini_model("GEMINI-2.5-PRO"))
        self.assertFalse(_is_gemini_model("gpt-4o"))
        self.assertFalse(_is_gemini_model(""))

    def test_system_role_collapse_folds_into_first_user(self) -> None:
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hi."},
        ]
        collapsed = _collapse_system_for_gemini(messages)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["role"], "user")
        self.assertIn("Be helpful.", collapsed[0]["content"])
        self.assertIn("Hi.", collapsed[0]["content"])

    def test_system_role_collapse_no_op_without_system(self) -> None:
        messages = [
            {"role": "user", "content": "Hi."},
            {"role": "assistant", "content": "Hello."},
        ]
        collapsed = _collapse_system_for_gemini(messages)
        self.assertEqual(collapsed, messages)
        # Defensive: ensure a fresh list is returned (caller may mutate).
        self.assertIsNot(collapsed, messages)

    def test_system_role_collapse_synthesises_user_when_missing(self) -> None:
        # System-only conversations happen in agent-tool bootstrap.
        messages = [{"role": "system", "content": "Just be."}]
        collapsed = _collapse_system_for_gemini(messages)
        self.assertEqual(collapsed[0]["role"], "user")
        self.assertIn("Just be.", collapsed[0]["content"])


class FinishReasonMappingTests(unittest.TestCase):
    def test_length_maps_to_length(self) -> None:
        self.assertEqual(_map_finish_reason("length"), "length")

    def test_stop_maps_to_stop(self) -> None:
        self.assertEqual(_map_finish_reason("stop"), "stop")

    def test_none_maps_to_none(self) -> None:
        self.assertIsNone(_map_finish_reason(None))

    def test_unknown_lowercases(self) -> None:
        self.assertEqual(_map_finish_reason("ContentFilter"), "contentfilter")


class SSEIteratorTests(unittest.TestCase):
    def test_yields_data_payloads_skips_done(self) -> None:
        response = _sse_response(
            [
                ": heartbeat",
                "event: message",
                'data: {"choices":[{"delta":{"content":"hi"}}]}',
                "",
                'data: {"choices":[{"delta":{"content":" there"}}]}',
                "data: [DONE]",
                'data: {"choices":[{"delta":{"content":"after-done"}}]}',
            ],
        )
        out = list(_iter_sse_data_lines(response))
        self.assertEqual(len(out), 2)
        self.assertIn("hi", out[0])
        self.assertIn("there", out[1])

    def test_malformed_lines_skipped(self) -> None:
        response = _sse_response(
            [
                "not a real sse line",
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
            ],
        )
        out = list(_iter_sse_data_lines(response))
        self.assertEqual(len(out), 1)


class ConstructorTests(unittest.TestCase):
    def test_missing_base_url_raises(self) -> None:
        settings = load_settings().ollama
        with self.assertRaises(ValueError):
            OpenAICompatibleClient(
                settings, base_url="", model="gpt-4o-mini",
            )

    def test_missing_model_raises(self) -> None:
        settings = load_settings().ollama
        with self.assertRaises(ValueError):
            OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model="",
            )

    def test_authorization_header_set_when_api_key_present(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-XXX",
            extra_headers={"HTTP-Referer": "https://my-app"},
        )
        headers = client._request_headers()
        self.assertEqual(headers["Authorization"], "Bearer sk-XXX")
        self.assertEqual(headers["HTTP-Referer"], "https://my-app")
        self.assertEqual(headers["Content-Type"], "application/json")


class ChatWithToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings().ollama
        self.client = OpenAICompatibleClient(
            self.settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )

    def test_payload_shape_matches_openai_contract(self) -> None:
        fake = _fake_chat_response(content="hello", usage={
            "prompt_tokens": 10,
            "completion_tokens": 2,
        })
        with patch(
            "app.llm.openai_compatible_client.requests.post", return_value=fake,
        ) as posted:
            result = self.client.chat_with_tools(
                [{"role": "user", "content": "Hi"}],
            )
        self.assertEqual(result.content, "hello")
        self.assertEqual(self.client.last_usage.prompt_tokens, 10)
        self.assertEqual(self.client.last_usage.completion_tokens, 2)
        posted.assert_called_once()
        call_kwargs = posted.call_args.kwargs
        payload = call_kwargs["json"]
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(payload["stream"], False)
        self.assertEqual(
            payload["messages"], [{"role": "user", "content": "Hi"}],
        )

    def test_tool_calls_parsed_from_message(self) -> None:
        fake = _fake_chat_response(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "calc.add",
                        "arguments": '{"a": 1, "b": 2}',
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "people.greet",
                        # Some providers return a dict directly.
                        "arguments": {"name": "John"},
                    },
                },
            ],
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ):
            result = self.client.chat_with_tools(
                [{"role": "user", "content": "Greet John and add 1 + 2"}],
                tools=[{
                    "type": "function",
                    "function": {"name": "calc.add"},
                }],
            )
        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(result.tool_calls[0].name, "calc.add")
        self.assertEqual(result.tool_calls[0].arguments, {"a": 1, "b": 2})
        self.assertEqual(result.tool_calls[1].arguments, {"name": "John"})

    def test_truncation_warning_fires_on_length(self) -> None:
        fake = _fake_chat_response(
            content="partial...", finish_reason="length",
            usage={"prompt_tokens": 5, "completion_tokens": 100},
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ), self.assertLogs(
            "app.llm.openai_compatible_client", level="WARNING",
        ) as captured:
            self.client.chat_with_tools(
                [{"role": "user", "content": "say a lot"}],
            )
        self.assertTrue(any(
            "response truncated" in rec.getMessage() for rec in captured.records
        ))

    def test_truncation_warning_suppressed_for_tool_pass_surface(self) -> None:
        fake = _fake_chat_response(
            content="", finish_reason="length",
            usage={"prompt_tokens": 5, "completion_tokens": 100},
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ):
            # No assertLogs context — the call must be silent.
            with self.assertLogs(
                "app.llm.openai_compatible_client", level="WARNING",
            ) as captured:
                self.client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    surface="tool_pass",
                )
                # Force the logger to emit at least once (debug level)
                # so assertLogs doesn't itself fail on "no log".
                logging.getLogger(
                    "app.llm.openai_compatible_client",
                ).warning("sentinel")
        # The sentinel is the *only* warning that landed.
        warns = [r for r in captured.records if r.levelname == "WARNING"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0].getMessage(), "sentinel")

    def test_gemini_system_collapse_applied_at_send_time(self) -> None:
        gemini = OpenAICompatibleClient(
            self.settings,
            base_url=(
                "https://generativelanguage.googleapis.com/v1beta/openai/"
            ),
            model="gemini-2.5-flash-lite",
            api_key="AIza-test",
        )
        fake = _fake_chat_response(content="ok")
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ) as posted:
            gemini.chat_with_tools(
                [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "Hi."},
                ],
            )
        sent = posted.call_args.kwargs["json"]["messages"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["role"], "user")
        self.assertIn("be brief", sent[0]["content"])

    def test_options_translate_to_openai_param_names(self) -> None:
        fake = _fake_chat_response(content="ok")
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ) as posted:
            self.client.chat_with_tools(
                [{"role": "user", "content": "x"}],
                options={
                    "temperature": 0.7,
                    "num_predict": 32,
                    "top_p": 0.9,
                },
            )
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["max_tokens"], 32)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertNotIn("num_predict", payload)


class ChatStreamTests(unittest.TestCase):
    def test_streams_content_and_captures_usage(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"hello "}}]}',
            'data: {"choices":[{"delta":{"content":"world"}}]}',
            (
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":4,"completion_tokens":2}}'
            ),
            "data: [DONE]",
        ]
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=_sse_response(sse_lines),
        ):
            tokens = list(
                client.chat_stream(
                    [{"role": "user", "content": "hi"}],
                ),
            )
        self.assertEqual("".join(tokens), "hello world")
        self.assertEqual(client.last_usage.prompt_tokens, 4)
        self.assertEqual(client.last_usage.completion_tokens, 2)
        self.assertEqual(client.last_usage.done_reason, "stop")

    def test_stream_truncation_warning_fires_on_length(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"part"}}]}',
            (
                'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
                '"usage":{"prompt_tokens":4,"completion_tokens":100}}'
            ),
            "data: [DONE]",
        ]
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=_sse_response(sse_lines),
        ), self.assertLogs(
            "app.llm.openai_compatible_client", level="WARNING",
        ) as captured:
            list(
                client.chat_stream(
                    [{"role": "user", "content": "x"}],
                ),
            )
        self.assertTrue(any(
            "response truncated" in rec.getMessage()
            for rec in captured.records
        ))


class ChatJsonTests(unittest.TestCase):
    def test_default_sets_response_format_json(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        fake = _fake_chat_response(
            content='{"ok": true}',
            usage={"prompt_tokens": 8, "completion_tokens": 6},
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ) as posted:
            content, usage = client.chat_json(
                [{"role": "user", "content": "make json"}],
            )
        self.assertEqual(content, '{"ok": true}')
        self.assertEqual(usage.completion_tokens, 6)
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["temperature"], 0.0)

    def test_format_json_false_omits_response_format(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        fake = _fake_chat_response(content="plain text")
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ) as posted:
            client.chat_json(
                [{"role": "user", "content": "summarise"}],
                format_json=False,
            )
        self.assertNotIn(
            "response_format", posted.call_args.kwargs["json"],
        )


class ListModelsTests(unittest.TestCase):
    def test_returns_ids_from_data_array(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        with patch(
            "app.llm.openai_compatible_client.requests.get",
            return_value=_fake_models_response(
                ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
            ),
        ):
            out = client.list_models()
        self.assertEqual(out, ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"])

    def test_returns_empty_on_transport_failure(self) -> None:
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        with patch(
            "app.llm.openai_compatible_client.requests.get",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(client.list_models(), [])

    def test_get_context_length_known_models(self) -> None:
        """Known model-id prefixes resolve to conservative caps."""
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )
        cases: list[tuple[str, int]] = [
            # GPT-5 family — all variants share the 128 k cap.
            ("gpt-5-mini", 131_072),
            ("gpt-5-nano", 131_072),
            ("gpt-5-pro", 131_072),
            ("gpt-5.4-mini", 131_072),
            ("gpt-5.5-pro", 131_072),
            ("GPT-5", 131_072),  # case-insensitive
            # GPT-4.1 family — 1 M native, capped at 128 k.
            ("gpt-4.1-mini", 131_072),
            ("gpt-4.1-nano", 131_072),
            ("gpt-4.1", 131_072),
            # GPT-4o / 4-turbo — native 128 k.
            ("gpt-4o-mini", 131_072),
            ("gpt-4o", 131_072),
            ("gpt-4-turbo", 131_072),
            # Older GPT-4 / 3.5 — smaller native windows.
            ("gpt-4", 8_192),
            ("gpt-3.5-turbo", 16_385),
            # Reasoning models — 200 k.
            ("o1", 200_000),
            ("o3", 200_000),
            ("o4-mini", 200_000),
            # Gemini family — capped at 128 k. Both bare and prefixed
            # forms accepted (`/v1/models` returns `models/gemini-...`).
            ("gemini-2.5-flash-lite", 131_072),
            ("gemini-2.5-flash", 131_072),
            ("gemini-2.5-pro", 131_072),
            ("models/gemini-2.5-pro", 131_072),
            # Groq llama family.
            ("llama-3.3-70b-versatile", 131_072),
            ("llama-3.1-8b-instant", 131_072),
            # Anthropic via OpenRouter (prefix-stripped + prefixed).
            ("claude-3.5-sonnet", 200_000),
            ("claude-3-opus", 200_000),
            ("claude-4", 200_000),
            ("anthropic/claude-3.5-sonnet", 200_000),
        ]
        for model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(
                    client.get_context_length(model),
                    expected,
                    f"{model!r} did not resolve to {expected}",
                )

    def test_get_context_length_unknown_returns_none(self) -> None:
        """Unrecognised model ids fall through to ``None`` so the
        controller's last-resort 8192 default kicks in."""
        settings = load_settings().ollama
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="unknown-model-xyz",
            api_key="sk-test",
        )
        for model in [
            "unknown-model-xyz",
            "totally-made-up-id",
            "",
            "   ",
            "ft:gpt-3.5-omg-fine-tuned",  # no prefix match without ``-turbo``
            "deepseek-v3",
            "mistral-large",
        ]:
            with self.subTest(model=model):
                self.assertIsNone(client.get_context_length(model))


if __name__ == "__main__":
    unittest.main()

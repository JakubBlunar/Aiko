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

    def test_max_tokens_field_name_picked_by_model_family(self) -> None:
        # Regression: OpenAI's GPT-5 family + o-series strict-reject
        # ``max_tokens`` (``Unsupported parameter: 'max_tokens' is not
        # supported with this model. Use 'max_completion_tokens'
        # instead.``). Older OpenAI models and every non-OpenAI
        # compat provider (Gemini, Groq, OpenRouter) still want
        # ``max_tokens``. The client switches the field name based on
        # model prefix; callers keep sending ``num_predict``.
        settings = load_settings().ollama
        fake = _fake_chat_response(content="ok")

        # GPT-5 family -> max_completion_tokens.
        for model in ("gpt-5", "gpt-5-mini", "gpt-5-nano"):
            client = OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model=model,
                api_key="sk-test",
            )
            with patch(
                "app.llm.openai_compatible_client.requests.post",
                return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    options={"num_predict": 256},
                )
            payload = posted.call_args.kwargs["json"]
            self.assertEqual(
                payload.get("max_completion_tokens"), 256,
                f"{model} should send max_completion_tokens",
            )
            self.assertNotIn(
                "max_tokens", payload,
                f"{model} must not send legacy max_tokens",
            )

        # o-series reasoning models -> max_completion_tokens.
        for model in ("o1", "o1-mini", "o3-mini", "o4-mini"):
            client = OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model=model,
                api_key="sk-test",
            )
            with patch(
                "app.llm.openai_compatible_client.requests.post",
                return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    options={"num_predict": 256},
                )
            payload = posted.call_args.kwargs["json"]
            self.assertEqual(
                payload.get("max_completion_tokens"), 256,
                f"{model} should send max_completion_tokens",
            )
            self.assertNotIn("max_tokens", payload)

        # Older OpenAI + non-OpenAI compat -> max_tokens (unchanged).
        for model in (
            "gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1",
            "gpt-4-turbo", "gemini-2.0-flash", "llama-3.1-70b",
        ):
            client = OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model=model,
                api_key="sk-test",
            )
            with patch(
                "app.llm.openai_compatible_client.requests.post",
                return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    options={"num_predict": 256, "temperature": 0.5},
                )
            payload = posted.call_args.kwargs["json"]
            self.assertEqual(
                payload.get("max_tokens"), 256,
                f"{model} should keep legacy max_tokens",
            )
            self.assertNotIn(
                "max_completion_tokens", payload,
                f"{model} must not send max_completion_tokens",
            )
            self.assertEqual(
                payload.get("temperature"), 0.5,
                f"{model} should pass temperature through",
            )

    def test_responses_api_family_drops_unsupported_sampling_knobs(self) -> None:
        # Regression: OpenAI's GPT-5 + o-series lock the sampling
        # knobs to defaults. ``temperature=0.6`` 400s with
        # ``Unsupported value: 'temperature' does not support 0.6 with
        # this model. Only the default (1) value is supported.``;
        # ``top_p`` / ``presence_penalty`` / ``frequency_penalty`` /
        # ``logprobs`` / ``top_logprobs`` / ``logit_bias`` are
        # rejected outright. The client drops them BEFORE posting so
        # ``TurnRunner``'s shared options dict can keep its
        # cross-provider shape.
        settings = load_settings().ollama
        fake = _fake_chat_response(content="ok")
        for model in (
            "gpt-5", "gpt-5-mini", "gpt-5-nano",
            "o1", "o1-mini", "o3-mini", "o4-mini",
        ):
            client = OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model=model,
                api_key="sk-test",
            )
            with patch(
                "app.llm.openai_compatible_client.requests.post",
                return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    options={
                        "num_predict": 256,
                        "temperature": 0.6,
                        "top_p": 0.9,
                        "presence_penalty": 0.5,
                        "frequency_penalty": 0.3,
                        "logprobs": True,
                        "top_logprobs": 5,
                        "logit_bias": {"50256": -100},
                        # Stays — both seed and stop are honoured by
                        # GPT-5 / o-series alongside the locked knobs.
                        "seed": 7,
                        "stop": ["END"],
                    },
                )
            payload = posted.call_args.kwargs["json"]
            for key in (
                "temperature", "top_p", "presence_penalty",
                "frequency_penalty", "logprobs", "top_logprobs",
                "logit_bias",
            ):
                self.assertNotIn(
                    key, payload,
                    f"{model} must drop unsupported sampling knob {key}",
                )
            # Still routes the token budget into the right field.
            self.assertEqual(
                payload.get("max_completion_tokens"), 256,
            )
            # Knobs not in the unsupported set still pass through.
            self.assertEqual(payload.get("seed"), 7)
            self.assertEqual(payload.get("stop"), ["END"])

    def test_responses_api_family_sets_minimal_reasoning_effort(self) -> None:
        # Regression: GPT-5 + o-series consume part of the
        # ``max_completion_tokens`` budget on internal reasoning
        # tokens before emitting visible content. With a tight
        # budget (``chat_llm.max_tokens=512``) and the default
        # ``reasoning_effort="medium"`` we saw the budget exhausted
        # entirely on reasoning, leaving Aiko's visible reply
        # empty (``chars=0``). Pinning ``reasoning_effort="minimal"``
        # makes the visible reply approximate the configured budget.
        settings = load_settings().ollama
        fake = _fake_chat_response(content="ok")
        for model in (
            "gpt-5", "gpt-5-mini", "gpt-5-nano",
            "o1", "o1-mini", "o3-mini", "o4-mini",
        ):
            client = OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model=model,
                api_key="sk-test",
            )
            with patch(
                "app.llm.openai_compatible_client.requests.post",
                return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    options={"num_predict": 512},
                )
            payload = posted.call_args.kwargs["json"]
            self.assertEqual(
                payload.get("reasoning_effort"), "minimal",
                f"{model} should inject reasoning_effort=minimal",
            )

        # Older OpenAI + non-OpenAI compat models must NOT get the
        # field (some providers 400 on unknown extras).
        for model in ("gpt-4o-mini", "gpt-4.1-mini", "gemini-2.0-flash"):
            client = OpenAICompatibleClient(
                settings,
                base_url="https://api.openai.com/v1",
                model=model,
                api_key="sk-test",
            )
            with patch(
                "app.llm.openai_compatible_client.requests.post",
                return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                    options={"num_predict": 512},
                )
            payload = posted.call_args.kwargs["json"]
            self.assertNotIn(
                "reasoning_effort", payload,
                f"{model} must not get reasoning_effort injection",
            )

    def test_responses_api_family_no_default_temperature_without_options(
        self,
    ) -> None:
        # When the caller passes no ``options`` dict at all, the
        # client previously injected a default temperature from
        # settings. That still 400s on the Responses-API family — so
        # the default-injection path must skip temperature when the
        # model belongs to that family.
        settings = load_settings().ollama
        fake = _fake_chat_response(content="ok")
        client = OpenAICompatibleClient(
            settings,
            base_url="https://api.openai.com/v1",
            model="gpt-5-mini",
            api_key="sk-test",
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ) as posted:
            client.chat_with_tools(
                [{"role": "user", "content": "x"}],
                # No options dict at all.
            )
        payload = posted.call_args.kwargs["json"]
        self.assertNotIn(
            "temperature", payload,
            "gpt-5-mini must not get an auto-injected temperature",
        )

    def test_ollama_only_options_are_dropped_from_payload(self) -> None:
        # Regression: ``TurnRunner`` builds the per-turn options dict
        # with ``num_ctx`` (an Ollama-only knob), and the rest of the
        # codebase routes that same dict through whichever
        # ``ChatClient`` is registered. OpenAI's
        # ``/chat/completions`` 400s on unknown params
        # (``Unknown parameter: 'num_ctx'``), so the OpenAI-compatible
        # client MUST strip every Ollama-host / Ollama-only-sampling
        # key before posting. Overlapping keys (``top_p``, ``top_k``,
        # ``seed``, …) must still pass through so Gemini's
        # OpenAI-compat layer keeps working.
        fake = _fake_chat_response(content="ok")
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=fake,
        ) as posted:
            self.client.chat_with_tools(
                [{"role": "user", "content": "x"}],
                options={
                    "temperature": 0.5,
                    "num_predict": 64,
                    # Ollama-only — MUST be stripped.
                    "num_ctx": 32768,
                    "num_keep": 24,
                    "num_thread": 8,
                    "num_batch": 512,
                    "num_gpu": 1,
                    "main_gpu": 0,
                    "low_vram": False,
                    "f16_kv": True,
                    "vocab_only": False,
                    "use_mmap": True,
                    "use_mlock": False,
                    "numa": False,
                    "mirostat": 0,
                    "mirostat_tau": 5.0,
                    "mirostat_eta": 0.1,
                    "tfs_z": 1.0,
                    "typical_p": 1.0,
                    "repeat_last_n": 64,
                    "penalize_newline": True,
                    # Shared with Gemini / Anthropic OpenAI-compat —
                    # MUST pass through.
                    "top_p": 0.9,
                    "top_k": 40,
                    "min_p": 0.05,
                    "repeat_penalty": 1.1,
                    "seed": 42,
                },
            )
        payload = posted.call_args.kwargs["json"]
        # Translated knobs landed in OpenAI vocabulary.
        self.assertEqual(payload["temperature"], 0.5)
        self.assertEqual(payload["max_tokens"], 64)
        # Ollama-only keys are gone.
        for key in (
            "num_ctx", "num_keep", "num_thread", "num_batch",
            "num_gpu", "main_gpu", "low_vram", "f16_kv",
            "vocab_only", "use_mmap", "use_mlock", "numa",
            "mirostat", "mirostat_tau", "mirostat_eta",
            "tfs_z", "typical_p", "repeat_last_n",
            "penalize_newline",
        ):
            self.assertNotIn(key, payload, f"{key} leaked into payload")
        # Cross-provider sampling knobs survived the filter so Gemini
        # / Anthropic OpenAI-compat routes still get them.
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["top_k"], 40)
        self.assertEqual(payload["min_p"], 0.05)
        self.assertEqual(payload["repeat_penalty"], 1.1)
        self.assertEqual(payload["seed"], 42)


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


class PromptCachingUsageTests(unittest.TestCase):
    """``prompt_tokens_details.cached_tokens`` parsing.

    OpenAI returns prompt-cache hits as a nested counter on the usage
    payload (see ``docs/prompt-caching.md``). Both the non-streaming
    and the streaming code paths must lift it onto ``ChatUsage``
    verbatim. Providers that omit the field (Ollama, most non-OpenAI
    OpenAI-compatible endpoints) must read back ``0``.
    """

    def setUp(self) -> None:
        self.settings = load_settings().ollama
        self.client = OpenAICompatibleClient(
            self.settings,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
        )

    def test_non_streaming_lifts_cached_tokens_onto_usage(self) -> None:
        fake = _fake_chat_response(
            content="hi",
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 1024},
            },
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post", return_value=fake,
        ):
            self.client.chat_with_tools(
                [{"role": "user", "content": "Hi"}],
            )
        usage = self.client.last_usage
        self.assertEqual(usage.prompt_tokens, 1200)
        self.assertEqual(usage.cached_tokens, 1024)
        # The derived hit-rate property is rounded to 1 decimal.
        self.assertAlmostEqual(usage.cached_tokens_pct, 85.3, places=1)

    def test_non_streaming_defaults_cached_tokens_when_absent(self) -> None:
        # Ollama / most non-OpenAI compatible providers don't ship the
        # nested ``prompt_tokens_details`` block at all. Default must
        # be 0, never an attribute error.
        fake = _fake_chat_response(
            content="hi",
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
        with patch(
            "app.llm.openai_compatible_client.requests.post", return_value=fake,
        ):
            self.client.chat_with_tools(
                [{"role": "user", "content": "Hi"}],
            )
        usage = self.client.last_usage
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.cached_tokens, 0)
        self.assertEqual(usage.cached_tokens_pct, 0.0)

    def test_streaming_lifts_cached_tokens_from_terminal_chunk(self) -> None:
        # The terminal SSE chunk carries the usage payload (because
        # the request opted in via ``stream_options.include_usage``).
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            (
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":1500,"completion_tokens":12,'
                '"prompt_tokens_details":{"cached_tokens":1408}}}'
            ),
            "data: [DONE]",
        ]
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=_sse_response(sse_lines),
        ):
            list(
                self.client.chat_stream(
                    [{"role": "user", "content": "go"}],
                ),
            )
        usage = self.client.last_usage
        self.assertEqual(usage.prompt_tokens, 1500)
        self.assertEqual(usage.completion_tokens, 12)
        self.assertEqual(usage.cached_tokens, 1408)
        # 1408 / 1500 ≈ 93.87 -> 93.9 after one-decimal rounding.
        self.assertAlmostEqual(usage.cached_tokens_pct, 93.9, places=1)

    def test_streaming_defaults_cached_tokens_when_absent(self) -> None:
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            (
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":42,"completion_tokens":3}}'
            ),
            "data: [DONE]",
        ]
        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=_sse_response(sse_lines),
        ):
            list(
                self.client.chat_stream(
                    [{"role": "user", "content": "x"}],
                ),
            )
        usage = self.client.last_usage
        self.assertEqual(usage.cached_tokens, 0)
        self.assertEqual(usage.cached_tokens_pct, 0.0)

    def test_chat_usage_merge_sums_cached_tokens(self) -> None:
        # The tool pre-pass + streaming reply pass both populate
        # ``last_usage``; ``TurnRunner`` calls ``ChatUsage.merge`` to
        # roll them into one row. Cached counts must add, not pick.
        from app.llm.chat_client import ChatUsage

        a = ChatUsage(prompt_tokens=900, cached_tokens=800)
        b = ChatUsage(prompt_tokens=1000, cached_tokens=900)
        merged = a.merge(b)
        self.assertEqual(merged.prompt_tokens, 1900)
        self.assertEqual(merged.cached_tokens, 1700)


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

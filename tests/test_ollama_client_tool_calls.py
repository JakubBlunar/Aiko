from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

from app.core.infra.settings import load_settings
from app.llm.ollama_client import OllamaClient


class OllamaClientToolCallsTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def test_chat_with_tools_parses_native_tool_calls(self) -> None:
        client = OllamaClient(self._ollama_settings)

        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "mcp.calc.add",
                            "arguments": {"a": 150, "b": 75},
                        },
                    },
                    {
                        "id": "call_2",
                        "function": {
                            "name": "mcp.people.greet",
                            "arguments": '{"name": "John"}',
                        },
                    },
                ],
            }
        }

        with patch("app.llm.ollama_client.requests.post", return_value=fake_response):
            result = client.chat_with_tools(
                [{"role": "user", "content": "Greet John and add 150 + 75"}],
                tools=[{"type": "function", "function": {"name": "mcp.calc.add"}}],
            )

        self.assertEqual(result.content, "")
        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(result.tool_calls[0].name, "mcp.calc.add")
        self.assertEqual(result.tool_calls[0].arguments, {"a": 150, "b": 75})
        self.assertEqual(result.tool_calls[1].name, "mcp.people.greet")
        self.assertEqual(result.tool_calls[1].arguments, {"name": "John"})

    def test_chat_still_returns_plain_content(self) -> None:
        client = OllamaClient(self._ollama_settings)

        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "message": {
                "content": "Hello there.",
            }
        }

        with patch("app.llm.ollama_client.requests.post", return_value=fake_response):
            result = client.chat([{"role": "user", "content": "Hi"}])

        self.assertEqual(result, "Hello there.")

    def test_chat_with_tools_captures_usage(self) -> None:
        client = OllamaClient(self._ollama_settings)
        fake_response = Mock()
        fake_response.ok = True
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "message": {"content": "ok"},
            "prompt_eval_count": 123,
            "eval_count": 45,
            "total_duration": 2_500_000_000,  # ns -> ms (2500ms)
            "eval_duration": 900_000_000,
            "prompt_eval_duration": 1_200_000_000,
        }

        with patch("app.llm.ollama_client.requests.post", return_value=fake_response):
            client.chat_with_tools([{"role": "user", "content": "hi"}])

        self.assertEqual(client.last_usage.prompt_tokens, 123)
        self.assertEqual(client.last_usage.completion_tokens, 45)
        self.assertEqual(client.last_usage.total_duration_ms, 2500.0)
        self.assertGreater(client.last_usage.tokens_per_second, 0.0)


class OllamaClientShowTests(unittest.TestCase):
    """Cover the new /api/show + get_context_length helpers."""

    def setUp(self) -> None:
        settings = load_settings()
        self._ollama_settings = settings.ollama

    def _make_client(self) -> OllamaClient:
        client = OllamaClient(self._ollama_settings)
        # Reset class-level cache so tests don't pollute each other.
        OllamaClient._show_cache.clear()
        return client

    def test_show_caches_results(self) -> None:
        client = self._make_client()
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "model_info": {"qwen2.context_length": 8192},
        }
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake_response,
        ) as posted:
            client.show("qwen2.5:3b")
            client.show("qwen2.5:3b")
            self.assertEqual(posted.call_count, 1)

    def test_show_returns_empty_dict_on_failure(self) -> None:
        client = self._make_client()
        fake_response = Mock()
        fake_response.raise_for_status.side_effect = Exception("404")
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake_response,
        ):
            self.assertEqual(client.show("missing-model"), {})

    def test_get_context_length_parses_qwen(self) -> None:
        client = self._make_client()
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "model_info": {
                "general.architecture": "qwen2",
                "qwen2.context_length": 32768,
                "qwen2.embedding_length": 4096,
            },
        }
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake_response,
        ):
            self.assertEqual(client.get_context_length("qwen2.5:3b"), 32768)

    def test_get_context_length_parses_llama(self) -> None:
        client = self._make_client()
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "model_info": {
                "llama.context_length": 131072,
            },
        }
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake_response,
        ):
            self.assertEqual(client.get_context_length("llama3.1:8b"), 131072)

    def test_get_context_length_returns_none_when_missing(self) -> None:
        client = self._make_client()
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"model_info": {"some.other.key": 1}}
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake_response,
        ):
            self.assertIsNone(client.get_context_length("weird-model"))


class OllamaClientNumCtxInjectionTests(unittest.TestCase):
    """Regression for the VRAM-spillover bug.

    Ollama allocates the kv-cache on the first call after a cold load
    based on whatever ``num_ctx`` is in that call's ``options`` (or
    the model's built-in default, often 256k for big models, if
    omitted). Without explicit injection a 30B model loads at 256k
    even when the user has configured ``ollama.context_window=32768``
    — and the load straddles VRAM + RAM with ~10x slower tokens/s
    (visible in ``ollama ps`` as a CPU/GPU split).

    These tests pin the default-injection contract for all three
    public call paths.
    """

    def setUp(self) -> None:
        settings = load_settings()
        self._base_settings = settings.ollama

    def _fake_chat_response(self) -> Mock:
        fake = Mock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.json.return_value = {"message": {"content": "ok"}}
        return fake

    def _fake_stream_response(self) -> MagicMock:
        fake = MagicMock()
        fake.ok = True
        fake.raise_for_status.return_value = None
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = None
        fake.iter_lines.return_value = iter([
            json.dumps({
                "message": {"content": "ok"},
                "done": True,
                "done_reason": "stop",
            }).encode("utf-8"),
        ])
        return fake

    # ── chat_with_tools ──────────────────────────────────────────────

    def test_chat_with_tools_injects_num_ctx_from_settings(self) -> None:
        settings = replace(self._base_settings, context_window=32768)
        client = OllamaClient(settings)
        fake = self._fake_chat_response()
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ) as posted:
            client.chat_with_tools([{"role": "user", "content": "x"}])
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["options"].get("num_ctx"), 32768)

    def test_chat_with_tools_omits_num_ctx_when_context_window_is_none(
        self,
    ) -> None:
        # ``None`` is the documented "auto-detect from Ollama" sentinel
        # — preserve the pre-fix behaviour and let Ollama pick.
        settings = replace(self._base_settings, context_window=None)
        client = OllamaClient(settings)
        fake = self._fake_chat_response()
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ) as posted:
            client.chat_with_tools([{"role": "user", "content": "x"}])
        payload = posted.call_args.kwargs["json"]
        self.assertNotIn("num_ctx", payload["options"])

    def test_chat_with_tools_caller_options_win_on_merge(self) -> None:
        # Explicit ``num_ctx`` in the caller's options dict must win
        # over the settings default. TurnRunner depends on this.
        settings = replace(self._base_settings, context_window=32768)
        client = OllamaClient(settings)
        fake = self._fake_chat_response()
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ) as posted:
            client.chat_with_tools(
                [{"role": "user", "content": "x"}],
                options={"num_ctx": 4096},
            )
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["options"]["num_ctx"], 4096)

    def test_chat_with_tools_omits_num_ctx_on_zero_or_negative(
        self,
    ) -> None:
        # Defensive: ``context_window=0`` is malformed config; treat
        # it the same as ``None`` rather than sending a nonsense value
        # to Ollama.
        for bad_value in (0, -1):
            settings = replace(
                self._base_settings, context_window=bad_value,
            )
            client = OllamaClient(settings)
            fake = self._fake_chat_response()
            with patch(
                "app.llm.ollama_client.requests.post", return_value=fake,
            ) as posted:
                client.chat_with_tools(
                    [{"role": "user", "content": "x"}],
                )
            payload = posted.call_args.kwargs["json"]
            self.assertNotIn(
                "num_ctx", payload["options"],
                f"num_ctx should be omitted for context_window={bad_value}",
            )

    # ── chat_stream ──────────────────────────────────────────────────

    def test_chat_stream_injects_num_ctx_from_settings(self) -> None:
        settings = replace(self._base_settings, context_window=8192)
        client = OllamaClient(settings)
        fake = self._fake_stream_response()
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ) as posted:
            stream = client.chat_stream([{"role": "user", "content": "x"}])
            list(stream)
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["options"].get("num_ctx"), 8192)

    # ── chat_json ────────────────────────────────────────────────────

    def test_chat_json_injects_num_ctx_from_settings(self) -> None:
        # Worker JSON calls (summary, learner profile, …) are the
        # specific code path that hit the cold-load pathology in
        # production — they never passed ``num_ctx`` themselves.
        settings = replace(self._base_settings, context_window=16384)
        client = OllamaClient(settings)
        fake = self._fake_chat_response()
        with patch(
            "app.llm.ollama_client.requests.post", return_value=fake,
        ) as posted:
            client.chat_json([{"role": "user", "content": "x"}])
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["options"].get("num_ctx"), 16384)
        # ``chat_json`` overrides temperature to 0.0 for determinism;
        # confirm we didn't accidentally regress that.
        self.assertEqual(payload["options"].get("temperature"), 0.0)


if __name__ == "__main__":
    unittest.main()

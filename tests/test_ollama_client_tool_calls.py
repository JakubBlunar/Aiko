from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.core.settings import load_settings
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


if __name__ == "__main__":
    unittest.main()

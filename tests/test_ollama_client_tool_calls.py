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


if __name__ == "__main__":
    unittest.main()

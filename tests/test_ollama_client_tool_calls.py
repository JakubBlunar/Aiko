from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.core.settings import AppSettings, load_settings
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


class SettingsSessionToolPoliciesTests(unittest.TestCase):
    def test_agentic_pre_execution_narration_policy_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            default_cfg = tmp_root / "default.json"
            user_cfg = tmp_root / "user.json"
            default_cfg.write_text(
                (
                    "{"
                    '"assistant":{"name":"Assistant","mode":"natural_chat","remember_history":true},'
                    '"autonomy":{"enabled":true,"mode":"automatic","session_tool_policies":{"agentic":{"native_tool_calls_enabled":true,"allowed_tool_prefixes":["mcp."],"pre_execution_narration_default":false}}},'
                    '"ollama":{"base_url":"http://127.0.0.1:11434","chat_model":"llama3.1:8b","temperature":0.6},'
                    '"audio":{"sample_rate":16000,"channels":1,"enable_microphone":true,"microphone_device":null},'
                    '"screen":{"enable_screen_context":false},'
                    '"actions":{"enabled":false},'
                    '"stt":{"provider":"faster_whisper","model":"base","language":"en"},'
                    '"tts":{"provider":"piper","voice":"en_US-lessac-medium","enabled":true},'
                    '"tooling":{}'
                    "}"
                ),
                encoding="utf-8",
            )
            user_cfg.write_text("{}", encoding="utf-8")

            with patch("app.core.settings.USER_CONFIG_PATH", user_cfg):
                loaded: AppSettings = load_settings(config_path=default_cfg)

        self.assertFalse(
            loaded.autonomy.session_tool_policies.agentic.pre_execution_narration_default
        )

    def test_force_agentic_and_session_tool_policies_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            default_cfg = tmp_root / "default.json"
            user_cfg = tmp_root / "user.json"
            default_cfg.write_text(
                (
                    "{"
                    '"assistant":{"name":"Assistant","mode":"natural_chat","remember_history":true},'
                    '"autonomy":{'
                    '"enabled":true,'
                    '"mode":"interactive",'
                    '"force_agentic_session":true,'
                    '"session_tool_policies":{'
                    '"chat":{"native_tool_calls_enabled":true,"allowed_tool_prefixes":["mcp."],"pre_execution_narration_default":false},'
                    '"reading":{"native_tool_calls_enabled":true,"allowed_tool_prefixes":["mcp.","tool."],"pre_execution_narration_default":false}'
                    '}'
                    "},"
                    '"ollama":{"base_url":"http://127.0.0.1:11434","chat_model":"llama3.1:8b","temperature":0.6},'
                    '"audio":{"sample_rate":16000,"channels":1,"enable_microphone":true,"microphone_device":null},'
                    '"screen":{"enable_screen_context":false},'
                    '"actions":{"enabled":false},'
                    '"stt":{"provider":"faster_whisper","model":"base","language":"en"},'
                    '"tts":{"provider":"piper","voice":"en_US-lessac-medium","enabled":true},'
                    '"tooling":{}'
                    "}"
                ),
                encoding="utf-8",
            )
            user_cfg.write_text("{}", encoding="utf-8")

            with patch("app.core.settings.USER_CONFIG_PATH", user_cfg):
                loaded: AppSettings = load_settings(config_path=default_cfg)

        self.assertTrue(loaded.autonomy.force_agentic_session)
        self.assertTrue(loaded.autonomy.session_tool_policies.chat.native_tool_calls_enabled)
        self.assertEqual(
            loaded.autonomy.session_tool_policies.chat.allowed_tool_prefixes,
            ("mcp.",),
        )
        self.assertTrue(loaded.autonomy.session_tool_policies.reading.native_tool_calls_enabled)
        self.assertEqual(
            loaded.autonomy.session_tool_policies.reading.allowed_tool_prefixes,
            ("mcp.", "tool."),
        )


if __name__ == "__main__":
    unittest.main()

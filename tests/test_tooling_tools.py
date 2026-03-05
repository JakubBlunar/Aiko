from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.conversation_memory import ConversationMemoryStore
from app.core.settings import (
    ActionSettings,
    AppSettings,
    AssistantSettings,
    AudioSettings,
    AutonomySettings,
    OllamaSettings,
    ScreenSettings,
    SttSettings,
    ToolingBridgeSettings,
    TtsSettings,
    UiSettings,
)
from app.core.tooling.tools import build_default_tools
from app.core.tooling.tools.history_tools import HistoryReadEntriesTool, HistoryReadMessagesTool, HistoryRuntime
from app.core.tooling.tools.ocr_tools import OcrExtractElementsTool, OcrRuntime
from app.core.tooling.tools.persona_tools import PersonaProfileRuntime, PersonaReadSnapshotTool, PersonaUpdateFromTextTool
from app.core.tooling.types import ToolContext


def _app_settings() -> AppSettings:
    return AppSettings(
        assistant=AssistantSettings(
            name="Assistant",
            mode="natural_chat",
            remember_history=True,
            personality="friendly",
            background="",
            thinking_model=None,
        ),
        autonomy=AutonomySettings(
            enabled=False,
            proactive_conversation=True,
            allow_action_suggestions=True,
            allow_proactive_actions=False,
            max_strategy_chars=180,
            auto_goal_switch=True,
            default_goal="general_conversation",
            goal_switch_min_confidence=0.75,
        ),
        ollama=OllamaSettings(base_url="http://127.0.0.1:11434", chat_model="llama3.1:8b", temperature=0.6),
        audio=AudioSettings(
            sample_rate=16000,
            channels=1,
            enable_microphone=True,
            enable_system_audio=False,
            microphone_device=None,
            loopback_device=None,
            vad_level_threshold=0.02,
            vad_silence_seconds=1.0,
        ),
        screen=ScreenSettings(
            enable_screen_context=True,
            ocr_profile="balanced",
            monitor_index=1,
            ocr_max_side_px=1280,
            capture_active_window_only=True,
            decision_mode="model",
            decision_cooldown_seconds=6,
            min_ocr_chars=20,
            unchanged_reuse_seconds=20,
            enable_uia=True,
        ),
        actions=ActionSettings(
            enabled=False,
            dry_run=True,
            require_confirmation=True,
            decision_mode="explicit_only",
            max_actions_per_turn=1,
            min_confidence=0.75,
            min_action_interval_seconds=1.0,
            emergency_hotkey="ctrl+alt+f12",
            allowlist_window_titles=[],
        ),
        stt=SttSettings(provider="faster_whisper", model="base", language="en"),
        tts=TtsSettings(
            provider="piper",
            voice="en_US-lessac-medium",
            enabled=True,
            llasa_model="NandemoGHS/Anime-Llasa-3B",
            llasa_codec_model="HKUSTAudio/xcodec2",
            llasa_device="cuda",
            llasa_temperature=0.8,
            llasa_top_p=0.95,
            llasa_max_length=2048,
            llasa_max_vram_mb=0,
        ),
        ui=UiSettings(window_x=None, window_y=None, window_width=None, window_height=None),
        tooling=ToolingBridgeSettings(
            config_default_path="config/tooling.default.yaml",
            config_user_path="config/tooling.user.yaml",
            enable_runtime_overrides=True,
        ),
    )


class ToolingToolsTests(unittest.TestCase):
    def test_build_default_tools_contains_expected_names(self) -> None:
        names = {tool.spec.name for tool in build_default_tools(_app_settings())}
        self.assertIn("history.read_messages", names)
        self.assertIn("history.read_entries", names)
        self.assertIn("ocr.extract_elements", names)
        self.assertIn("ocr.extract_details", names)
        self.assertIn("uia.get_foreground_elements", names)
        self.assertIn("uia.list_visible_windows", names)
        self.assertIn("uia.list_all_windows", names)
        self.assertIn("uia.focus_window", names)
        self.assertIn("persona.update_from_user_text", names)
        self.assertIn("persona.read_snapshot", names)

    def test_ocr_tool_missing_image_is_validation_error(self) -> None:
        tool = OcrExtractElementsTool(OcrRuntime(_app_settings().screen))
        result = tool.run(ToolContext(), {})
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "missing_image")

    def test_persona_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "persona.json"
            runtime = PersonaProfileRuntime(path=path, assistant_background="helper")
            updater = PersonaUpdateFromTextTool(runtime)
            reader = PersonaReadSnapshotTool(runtime)

            update_result = updater.run(ToolContext(), {"user_text": "my name is Alex"})
            self.assertTrue(update_result.success)

            snapshot = reader.run(ToolContext(), {"max_notes": 6})
            self.assertTrue(snapshot.success)
            self.assertEqual(snapshot.data["assistant_background"], "helper")
            self.assertGreaterEqual(len(snapshot.data["user_notes"]), 1)

    def test_history_tools_limit_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "memory.jsonl"
            store = ConversationMemoryStore(path)
            store.add(role="user", content="u1")
            store.add(role="assistant", content="a1")
            store.add(role="user", content="u2")
            store.add(role="assistant", content="a2")

            runtime = HistoryRuntime(store, default_limit=2, max_limit=3)
            read_messages = HistoryReadMessagesTool(runtime)
            read_entries = HistoryReadEntriesTool(runtime)

            latest = read_messages.run(ToolContext(), {})
            self.assertTrue(latest.success)
            self.assertEqual([m["content"] for m in latest.data["messages"]], ["u2", "a2"])

            lookback = read_messages.run(ToolContext(), {"limit": 2, "offset": 2})
            self.assertTrue(lookback.success)
            self.assertEqual([m["content"] for m in lookback.data["messages"]], ["u1", "a1"])

            clamped = read_entries.run(ToolContext(), {"limit": 99})
            self.assertTrue(clamped.success)
            self.assertEqual(clamped.data["count"], 3)
            self.assertEqual(clamped.data["entries"][-1]["content"], "a2")


if __name__ == "__main__":
    unittest.main()

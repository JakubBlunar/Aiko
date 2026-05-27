"""Tests for the typed-proactive + activity-awareness slice of
``/api/settings`` REST surface.

Uses a MagicMock-backed ``SessionController`` so we don't pay the full
``create_web_app`` startup cost. We only configure the
``_settings.agent`` / ``_settings.tools`` slots that the endpoint
reads, plus the small handful of properties / methods the GET helper
hits to compute the response body.

What we cover here:

  - GET surfaces the four new ``proactive`` keys + the ``activity``
    block.
  - PATCH applies typed-proactive knobs and forwards the typed
    cooldown to ``ProactiveDirector.update_runtime``.
  - PATCH applies the ``activity.awareness_enabled`` toggle and
    clears any cached ``_user_active_app`` when the toggle flips off.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


@dataclass
class _AgentBlock:
    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 240.0
    proactive_cooldown_seconds_typed: float = 600.0
    activity_awareness_enabled: bool = False


@dataclass
class _ToolsBlock:
    enabled: bool = True
    get_time: bool = True
    recall: bool = True
    web_search: bool = True
    world: bool = True


@dataclass
class _EndpointingBlock:
    enabled: bool = True
    use_partial_transcript: bool = True
    phrase_silence_seconds: float = 1.0
    turn_silence_seconds: float = 3.0
    fast_close_silence_seconds: float = 0.6
    hesitation_extend_to_turn: bool = True
    barge_in_min_speech_seconds: float = 0.7


@dataclass
class _OllamaBlock:
    temperature: float = 0.6


@dataclass
class _ChatLlmBlock:
    max_tokens: int = 512


@dataclass
class _SttBlock:
    language: str | None = None


@dataclass
class _TtsBlock:
    enabled: bool = True


@dataclass
class _AudioBlock:
    pass


@dataclass
class _SettingsStub:
    agent: _AgentBlock = field(default_factory=_AgentBlock)
    tools: _ToolsBlock = field(default_factory=_ToolsBlock)
    endpointing: _EndpointingBlock = field(default_factory=_EndpointingBlock)
    ollama: _OllamaBlock = field(default_factory=_OllamaBlock)
    chat_llm: _ChatLlmBlock = field(default_factory=_ChatLlmBlock)
    stt: _SttBlock = field(default_factory=_SttBlock)
    tts: _TtsBlock = field(default_factory=_TtsBlock)
    audio: _AudioBlock = field(default_factory=_AudioBlock)


def _build_client() -> tuple[TestClient, MagicMock, _SettingsStub]:
    settings = _SettingsStub()
    session = MagicMock()
    session._settings = settings
    session.session_key = "u:s"
    session.effective_chat_model = "test-model"
    session.context_window_size = 8192
    session.context_window_source = "fallback"
    session.tts_provider = "fake"
    session.tts_voice = "fake"
    session.stt_model = "fake"
    session.microphone_device = None
    session.output_device = None
    session.vad_level_threshold = 0.02
    session.vad_silence_seconds = 1.0
    session.barge_in_enabled.return_value = False
    session.available_tool_names.return_value = []
    # Track active-app state on the mock so the PATCH path can drop it.
    session._user_active_app = "Discord"

    def _set_active_app(app):
        if not settings.agent.activity_awareness_enabled:
            session._user_active_app = None
            return
        session._user_active_app = app

    session.set_user_active_app.side_effect = _set_active_app

    app = create_web_app(session)
    client = TestClient(app)
    return client, session, settings


class GetSettingsTests(unittest.TestCase):
    def test_get_surfaces_typed_proactive_and_activity(self) -> None:
        client, _session, _settings = _build_client()
        response = client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        proactive = body["proactive"]
        self.assertIn("typed_enabled", proactive)
        self.assertIn("silence_seconds_typed", proactive)
        self.assertIn("cooldown_seconds_typed", proactive)
        self.assertEqual(proactive["typed_enabled"], True)
        self.assertEqual(proactive["silence_seconds_typed"], 240.0)
        self.assertEqual(proactive["cooldown_seconds_typed"], 600.0)
        self.assertIn("activity", body)
        self.assertEqual(
            body["activity"], {"awareness_enabled": False},
        )


class PatchSettingsTests(unittest.TestCase):
    def test_patch_typed_proactive(self) -> None:
        client, session, settings = _build_client()
        response = client.patch(
            "/api/settings",
            json={
                "proactive": {
                    "typed_enabled": False,
                    "silence_seconds_typed": 360,
                    "cooldown_seconds_typed": 1200,
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(settings.agent.proactive_typed_enabled, False)
        self.assertEqual(settings.agent.proactive_silence_seconds_typed, 360.0)
        self.assertEqual(
            settings.agent.proactive_cooldown_seconds_typed, 1200.0,
        )
        # The PATCH path forwards the typed cooldown so an in-flight
        # director picks up the change without a restart.
        session._proactive.update_runtime.assert_called_with(
            cooldown_seconds_typed=1200.0,
        )

    def test_patch_typed_silence_clamps_floor(self) -> None:
        client, _session, settings = _build_client()
        response = client.patch(
            "/api/settings",
            json={"proactive": {"silence_seconds_typed": 5}},
        )
        self.assertEqual(response.status_code, 200)
        # 60 s minimum — anything shorter reads as nag-y at typed speed.
        self.assertEqual(settings.agent.proactive_silence_seconds_typed, 60.0)

    def test_patch_activity_enable_then_disable_clears_app(self) -> None:
        client, session, settings = _build_client()
        # Pretend the user enabled awareness earlier in the session and
        # an app was captured. Toggling off via PATCH must drop it.
        settings.agent.activity_awareness_enabled = True
        session._user_active_app = "Code"
        response = client.patch(
            "/api/settings",
            json={"activity": {"awareness_enabled": False}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(settings.agent.activity_awareness_enabled)
        session.set_user_active_app.assert_called_with(None)
        self.assertIsNone(session._user_active_app)

    def test_patch_activity_enable(self) -> None:
        client, _session, settings = _build_client()
        response = client.patch(
            "/api/settings",
            json={"activity": {"awareness_enabled": True}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(settings.agent.activity_awareness_enabled)


if __name__ == "__main__":
    unittest.main()

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
    proactive_typed_when_away: bool = False
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
class _LoggingBlock:
    ui_log_enabled: bool = False
    ui_log_categories: list[str] = field(
        default_factory=lambda: ["ws", "channel", "settings", "voice"],
    )
    ui_log_max_batch: int = 50
    ui_log_max_payload_bytes: int = 2048


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
    logging: _LoggingBlock = field(default_factory=_LoggingBlock)


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
    session.vad_level_threshold = 0.02
    session.vad_silence_seconds = 1.0
    session.barge_in_enabled.return_value = False
    session.available_tool_names.return_value = []
    # GET /api/settings now includes the masked chat_llm snapshot.
    # Return a real serialisable dict from the mocked accessor so
    # FastAPI's JSON encoder doesn't choke on a MagicMock placeholder.
    session._chat_llm_public_snapshot.return_value = {
        "provider": "ollama",
        "provider_preset": "",
        "model": "",
        "base_url": "",
        "has_api_key": False,
        "api_key_env": "",
        "max_tokens": 512,
        "temperature": None,
        "context_window": None,
        "keep_alive": "30m",
        "workers_use_local": True,
        "extra_headers": {},
    }
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


class LoggingSettingsRoundTripTests(unittest.TestCase):
    """``logging.ui_log_enabled`` round-trips through GET / PATCH.

    The Settings drawer's "Debug logging" toggle drives this. Flipping
    it must persist on the backend and surface back on the next GET so
    a freshly-opened tab sees the same state.
    """

    def test_get_exposes_logging_block_with_defaults(self) -> None:
        client, _session, _settings = _build_client()
        body = client.get("/api/settings").json()
        self.assertIn("logging", body)
        self.assertFalse(body["logging"]["ui_log_enabled"])
        self.assertEqual(
            body["logging"]["ui_log_categories"],
            ["ws", "channel", "settings", "voice"],
        )
        self.assertEqual(body["logging"]["ui_log_max_batch"], 50)
        self.assertEqual(body["logging"]["ui_log_max_payload_bytes"], 2048)

    def test_patch_flips_ui_log_enabled(self) -> None:
        client, _session, settings = _build_client()
        response = client.patch(
            "/api/settings",
            json={"logging": {"ui_log_enabled": True}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(settings.logging.ui_log_enabled)
        # And the response body should reflect it so the UI can sync
        # without a follow-up GET.
        self.assertTrue(response.json()["logging"]["ui_log_enabled"])

    def test_patch_clamps_batch_and_payload(self) -> None:
        client, _session, settings = _build_client()
        client.patch(
            "/api/settings",
            json={
                "logging": {
                    "ui_log_max_batch": 9999,
                    "ui_log_max_payload_bytes": 50,
                },
            },
        )
        # 500 ceiling on the batch, 256 floor on the payload — keep the
        # rotating log from being smothered by a misbehaving client.
        self.assertEqual(settings.logging.ui_log_max_batch, 500)
        self.assertEqual(settings.logging.ui_log_max_payload_bytes, 256)


if __name__ == "__main__":
    unittest.main()

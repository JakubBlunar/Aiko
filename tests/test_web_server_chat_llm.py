"""Tests for the chat-LLM provider REST surface.

Covers ``GET /api/settings`` masking, ``PATCH /api/settings`` chat_llm
branch, ``PUT /api/settings/llm-credentials``, ``GET /api/llm/presets``,
``GET /api/models?provider=``, and ``POST /api/llm/test-connection``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from app.web.server import create_web_app


# ── Settings stubs (minimum surface the GET handler reads) ─────────


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
    base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "llama3.1:8b"
    embedding_model: str = "qwen3-embedding:0.6b"
    timeout: int = 300


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
    ui_log_categories: list[str] = field(default_factory=list)
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


_SAMPLE_PRESETS = [
    {
        "id": "ollama",
        "label": "Local Ollama",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "recommended_models": ["llama3.1:8b"],
        "env_hint": "",
        "api_key_required": False,
        "free_tier": "Unlimited (local)",
        "docs_url": "https://ollama.com",
        "default_workers_use_local": False,
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "provider": "openai_compatible",
        "base_url": (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        "recommended_models": ["gemini-2.5-flash-lite"],
        "env_hint": "GEMINI_API_KEY",
        "api_key_required": True,
        "free_tier": "~1500 req/day free",
        "docs_url": "https://ai.google.dev",
        "default_workers_use_local": True,
    },
]


def _build_client() -> tuple[TestClient, MagicMock, _SettingsStub]:
    settings = _SettingsStub()
    session = MagicMock()
    session._settings = settings
    session.session_key = "u:s"
    session.effective_chat_model = "llama3.1:8b"
    session.context_window_size = 8192
    session.context_window_source = "fallback"
    session.tts_provider = "fake"
    session.tts_voice = "fake"
    session.stt_model = "fake"
    session.vad_level_threshold = 0.02
    session.vad_silence_seconds = 1.0
    session.barge_in_enabled.return_value = False
    session.available_tool_names.return_value = []
    # The masked snapshot — never includes the raw key.
    masked = {
        "provider": "ollama",
        "provider_preset": "",
        "model": "",
        "base_url": "http://127.0.0.1:11434",
        "has_api_key": False,
        "api_key_env": "",
        "max_tokens": 512,
        "temperature": None,
        "context_window": None,
        "keep_alive": "30m",
        "workers_use_local": True,
        "extra_headers": {},
    }
    session._chat_llm_public_snapshot.return_value = masked
    session.provider_presets.return_value = _SAMPLE_PRESETS
    session.list_chat_models.return_value = ["llama3.1:8b"]
    # ``reconfigure_chat_llm`` returns the updated snapshot.
    session.reconfigure_chat_llm.return_value = masked

    app = create_web_app(session)
    return TestClient(app), session, settings


class GetSettingsChatLlmTests(unittest.TestCase):
    def test_get_includes_masked_chat_llm_block(self) -> None:
        client, _session, _settings = _build_client()
        response = client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("chat_llm", body)
        self.assertIn("has_api_key", body["chat_llm"])
        self.assertNotIn("api_key", body["chat_llm"])
        self.assertEqual(body["chat_llm"]["provider"], "ollama")


class PatchChatLlmTests(unittest.TestCase):
    def test_patch_chat_llm_triggers_reconfigure_and_broadcast(
        self,
    ) -> None:
        client, session, _settings = _build_client()
        response = client.patch(
            "/api/settings",
            json={
                "chat_llm": {
                    "provider": "openai_compatible",
                    "model": "gemini-2.5-flash-lite",
                    "workers_use_local": True,
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        session.reconfigure_chat_llm.assert_called_once()
        called_payload = session.reconfigure_chat_llm.call_args.args[0]
        self.assertEqual(called_payload.get("model"), "gemini-2.5-flash-lite")
        self.assertTrue(called_payload.get("workers_use_local"))

    def test_patch_chat_llm_strips_api_key_from_payload(self) -> None:
        # Safety net: PATCH /api/settings is NOT the credentials path.
        # An api_key that slips into the payload is dropped before
        # reaching ``reconfigure_chat_llm``.
        client, session, _settings = _build_client()
        client.patch(
            "/api/settings",
            json={
                "chat_llm": {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "api_key": "should-be-dropped",
                },
            },
        )
        called_payload = session.reconfigure_chat_llm.call_args.args[0]
        self.assertNotIn("api_key", called_payload)

    def test_patch_chat_llm_context_window_round_trips(self) -> None:
        """The new Context window input on the Advanced panel saves
        through to ``reconfigure_chat_llm`` so the controller can rebuild
        the prompt-assembler budget. Both a positive integer and a
        ``null`` (= auto) value must be passed through unchanged."""
        client, session, _settings = _build_client()
        # Positive integer override.
        client.patch(
            "/api/settings",
            json={
                "chat_llm": {
                    "provider": "openai_compatible",
                    "model": "gpt-5-mini",
                    "context_window": 65_536,
                },
            },
        )
        called = session.reconfigure_chat_llm.call_args.args[0]
        self.assertEqual(called.get("context_window"), 65_536)
        self.assertEqual(called.get("model"), "gpt-5-mini")
        # ``null`` -> "auto" (controller falls back to client lookup).
        session.reconfigure_chat_llm.reset_mock()
        client.patch(
            "/api/settings",
            json={
                "chat_llm": {
                    "provider": "openai_compatible",
                    "model": "gpt-5-mini",
                    "context_window": None,
                },
            },
        )
        called = session.reconfigure_chat_llm.call_args.args[0]
        self.assertIsNone(called.get("context_window"))


class PutCredentialsTests(unittest.TestCase):
    def test_put_credentials_writes_via_reconfigure(self) -> None:
        client, session, _settings = _build_client()
        response = client.put(
            "/api/settings/llm-credentials",
            json={
                "api_key": "AIza-test",
                "base_url": (
                    "https://generativelanguage.googleapis.com/v1beta/openai/"
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Response is the masked snapshot — never echo the raw key.
        self.assertNotIn("api_key", body)
        self.assertIn("has_api_key", body)
        session.reconfigure_chat_llm.assert_called_once()
        called_payload = session.reconfigure_chat_llm.call_args.args[0]
        self.assertEqual(called_payload["api_key"], "AIza-test")

    def test_put_credentials_rejects_whitespace_in_api_key(self) -> None:
        client, session, _settings = _build_client()
        response = client.put(
            "/api/settings/llm-credentials",
            json={"api_key": "bad key with spaces"},
        )
        self.assertEqual(response.status_code, 400)
        session.reconfigure_chat_llm.assert_not_called()

    def test_put_credentials_rejects_invalid_base_url(self) -> None:
        client, session, _settings = _build_client()
        response = client.put(
            "/api/settings/llm-credentials",
            json={"base_url": "ftp://example.com"},
        )
        self.assertEqual(response.status_code, 400)
        session.reconfigure_chat_llm.assert_not_called()

    def test_put_credentials_with_empty_body_is_noop(self) -> None:
        client, session, _settings = _build_client()
        response = client.put(
            "/api/settings/llm-credentials", json={},
        )
        self.assertEqual(response.status_code, 200)
        session.reconfigure_chat_llm.assert_not_called()


class PresetsAndModelsTests(unittest.TestCase):
    def test_get_presets_returns_catalogue(self) -> None:
        client, _session, _settings = _build_client()
        response = client.get("/api/llm/presets")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("presets", body)
        ids = {p["id"] for p in body["presets"]}
        self.assertIn("ollama", ids)
        self.assertIn("gemini", ids)

    def test_models_provider_query_dispatches(self) -> None:
        client, session, _settings = _build_client()
        client.get("/api/models?provider=openai_compatible")
        session.list_chat_models.assert_called_with(
            provider="openai_compatible",
        )

    def test_models_no_provider_uses_default(self) -> None:
        client, session, _settings = _build_client()
        client.get("/api/models")
        session.list_chat_models.assert_called_with(refresh=False)


class TestConnectionTests(unittest.TestCase):
    """``POST /api/llm/test-connection`` happy + failure paths.

    Heart of the contract: the endpoint **never** calls
    ``persist_user_overrides`` (the candidate creds are dry-run only)
    and **never** mutates ``session._settings.chat_llm`` or any of the
    real client references on the controller.
    """

    def test_happy_path_returns_success(self) -> None:
        client, session, _settings = _build_client()
        # Build a stub ChatClient that responds with a fixed content.
        stub = MagicMock()
        stub.chat_with_tools.return_value = MagicMock(
            content="ok", tool_calls=[],
        )
        stub.last_usage = MagicMock(prompt_tokens=4, completion_tokens=1)
        with patch(
            "app.core.session.session_controller._build_chat_client",
            return_value=stub,
        ), patch(
            "app.core.session.session_controller.persist_user_overrides",
        ) as persist:
            response = client.post(
                "/api/llm/test-connection",
                json={
                    "provider": "openai_compatible",
                    "model": "gemini-2.5-flash-lite",
                    "base_url": (
                        "https://generativelanguage.googleapis.com/v1beta/openai/"
                    ),
                    "api_key": "AIza-test",
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["model_resolved"], "gemini-2.5-flash-lite")
        self.assertEqual(body["completion_tokens"], 1)
        self.assertIsNone(body["error_code"])
        self.assertIsNone(body["error_message"])
        # REGRESSION: test-connection must never persist credentials.
        persist.assert_not_called()
        # And must never call reconfigure_chat_llm either.
        session.reconfigure_chat_llm.assert_not_called()

    def test_unauthorized_returns_structured_error(self) -> None:
        client, _session, _settings = _build_client()
        stub = MagicMock()
        http_resp = MagicMock()
        http_resp.status_code = 401
        stub.chat_with_tools.side_effect = requests.HTTPError(
            "401 Unauthorized", response=http_resp,
        )
        with patch(
            "app.core.session.session_controller._build_chat_client",
            return_value=stub,
        ):
            response = client.post(
                "/api/llm/test-connection",
                json={
                    "provider": "openai_compatible",
                    "model": "gemini-2.5-flash-lite",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                    "api_key": "wrong",
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "unauthorized")
        self.assertIn("401", body["error_message"])

    def test_timeout_returns_timeout_code(self) -> None:
        client, _session, _settings = _build_client()
        stub = MagicMock()
        stub.chat_with_tools.side_effect = requests.exceptions.Timeout()
        with patch(
            "app.core.session.session_controller._build_chat_client",
            return_value=stub,
        ):
            response = client.post(
                "/api/llm/test-connection",
                json={
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "timeout")

    def test_malformed_payload_returns_400(self) -> None:
        client, _session, _settings = _build_client()
        response = client.post(
            "/api/llm/test-connection",
            json={"provider": "bogus"},
        )
        self.assertEqual(response.status_code, 400)

    def test_openai_compatible_requires_model(self) -> None:
        client, _session, _settings = _build_client()
        response = client.post(
            "/api/llm/test-connection",
            json={
                "provider": "openai_compatible",
                "model": "",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk",
            },
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

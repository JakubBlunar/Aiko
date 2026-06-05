"""Tests for the PR 2 catalogue REST surface + controller methods.

Covers:
- ``GET /api/llm/providers`` masking,
- ``POST /api/llm/providers`` add (template + custom),
- ``PATCH /api/llm/providers/{id}`` edit (api_key stripped),
- ``PUT /api/llm/providers/{id}/credentials``,
- ``DELETE /api/llm/providers/{id}`` (404 on unknown, 409 when referenced),
- ``POST /api/llm/providers/{id}/test``,
- ``GET /api/llm/routes``,
- ``PATCH /api/llm/routes/{role}`` (404 when provider missing).

Uses the same FastAPI ``TestClient`` + ``MagicMock`` session pattern
as :mod:`tests.test_web_server_chat_llm` so we exercise the route
handler logic without standing up a real :class:`SessionController`.
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


_SAMPLE_PROVIDERS = [
    {
        "id": "local_ollama",
        "name": "Local Ollama",
        "kind": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "has_api_key": False,
        "api_key_env": "",
        "extra_headers": {},
        "timeout_seconds": 300,
        "keep_alive": "30m",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "has_api_key": True,
        "api_key_env": "OPENAI_API_KEY",
        "extra_headers": {},
        "timeout_seconds": 300,
        "keep_alive": "30m",
    },
]

_SAMPLE_ROUTES = {
    "main_chat": {
        "provider_id": "openai",
        "model": "gpt-5-mini",
        "context_window": 131_072,
        "max_tokens": 512,
        "temperature": None,
    },
    "worker_default": {
        "provider_id": "local_ollama",
        "model": "llama3.1:8b",
        "context_window": None,
        "max_tokens": 512,
        "temperature": None,
    },
}


def _build_client() -> tuple[TestClient, MagicMock]:
    settings = _SettingsStub()
    session = MagicMock()
    session._settings = settings
    session.session_key = "u:s"
    session.effective_chat_model = "gpt-5-mini"
    session.context_window_size = 131_072
    session.context_window_source = "config"
    session.tts_provider = "fake"
    session.tts_voice = "fake"
    session.stt_model = "fake"
    session.vad_level_threshold = 0.02
    session.vad_silence_seconds = 1.0
    session.barge_in_enabled.return_value = False
    session.available_tool_names.return_value = []
    session._chat_llm_public_snapshot.return_value = {
        "provider": "openai_compatible",
        "provider_preset": "openai",
        "model": "gpt-5-mini",
        "base_url": "https://api.openai.com/v1",
        "has_api_key": True,
        "api_key_env": "OPENAI_API_KEY",
        "max_tokens": 512,
        "temperature": None,
        "context_window": 131_072,
        "keep_alive": "30m",
        "workers_use_local": True,
        "extra_headers": {},
    }
    session.list_providers.return_value = list(_SAMPLE_PROVIDERS)
    session.list_routes.return_value = dict(_SAMPLE_ROUTES)
    session.provider_presets.return_value = []
    return TestClient(create_web_app(session)), session


class GetProvidersTests(unittest.TestCase):
    def test_returns_masked_catalogue(self) -> None:
        client, _ = _build_client()
        resp = client.get("/api/llm/providers")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("providers", body)
        for entry in body["providers"]:
            self.assertNotIn("api_key", entry)
            self.assertIn("has_api_key", entry)


class PostProviderTests(unittest.TestCase):
    def test_template_seeded_add_calls_session(self) -> None:
        client, session = _build_client()
        session.add_provider.return_value = _SAMPLE_PROVIDERS[1]
        resp = client.post(
            "/api/llm/providers",
            json={"template_id": "openai", "draft": {"name": "OpenAI Team"}},
        )
        self.assertEqual(resp.status_code, 200)
        session.add_provider.assert_called_once_with(
            template_id="openai", draft={"name": "OpenAI Team"},
        )

    def test_conflict_returns_409(self) -> None:
        client, session = _build_client()
        session.add_provider.side_effect = ValueError("id already exists")
        resp = client.post(
            "/api/llm/providers",
            json={"draft": {"id": "openai"}},
        )
        self.assertEqual(resp.status_code, 409)


class PatchProviderTests(unittest.TestCase):
    def test_patch_strips_api_key(self) -> None:
        client, session = _build_client()
        session.update_provider.return_value = _SAMPLE_PROVIDERS[1]
        client.patch(
            "/api/llm/providers/openai",
            json={"name": "Renamed", "api_key": "sk-leak"},
        )
        called_payload = session.update_provider.call_args.args[1]
        self.assertEqual(called_payload.get("name"), "Renamed")
        self.assertNotIn("api_key", called_payload)

    def test_patch_unknown_returns_404(self) -> None:
        client, session = _build_client()
        session.update_provider.side_effect = KeyError("unknown")
        resp = client.patch("/api/llm/providers/missing", json={"name": "x"})
        self.assertEqual(resp.status_code, 404)


class PutCredentialsTests(unittest.TestCase):
    def test_credentials_round_trip(self) -> None:
        client, session = _build_client()
        session.update_provider_credentials.return_value = _SAMPLE_PROVIDERS[1]
        resp = client.put(
            "/api/llm/providers/openai/credentials",
            json={"api_key": "sk-new"},
        )
        self.assertEqual(resp.status_code, 200)
        # The response is masked: raw key never round-trips.
        self.assertNotIn("api_key", resp.json())
        self.assertIn("has_api_key", resp.json())
        called = session.update_provider_credentials.call_args.args[1]
        self.assertEqual(called.get("api_key"), "sk-new")

    def test_rejects_whitespace_in_api_key(self) -> None:
        client, _ = _build_client()
        resp = client.put(
            "/api/llm/providers/openai/credentials",
            json={"api_key": "bad key"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_body_rejected(self) -> None:
        client, _ = _build_client()
        resp = client.put(
            "/api/llm/providers/openai/credentials", json={},
        )
        self.assertEqual(resp.status_code, 400)


class DeleteProviderTests(unittest.TestCase):
    def test_delete_ok(self) -> None:
        client, session = _build_client()
        resp = client.delete("/api/llm/providers/openai")
        self.assertEqual(resp.status_code, 200)
        session.remove_provider.assert_called_once_with("openai")

    def test_delete_unknown_returns_404(self) -> None:
        client, session = _build_client()
        session.remove_provider.side_effect = KeyError("unknown")
        resp = client.delete("/api/llm/providers/missing")
        self.assertEqual(resp.status_code, 404)

    def test_delete_referenced_returns_409(self) -> None:
        client, session = _build_client()
        session.remove_provider.side_effect = ValueError(
            "provider is still referenced by main_chat",
        )
        resp = client.delete("/api/llm/providers/openai")
        self.assertEqual(resp.status_code, 409)


class TestProviderTests(unittest.TestCase):
    def test_test_provider_passes_overrides(self) -> None:
        client, session = _build_client()
        session.test_provider.return_value = {
            "success": True, "latency_ms": 42, "completion_tokens": 1,
        }
        resp = client.post(
            "/api/llm/providers/openai/test",
            json={"model": "gpt-5-mini", "context_window": 65_536},
        )
        self.assertEqual(resp.status_code, 200)
        # Positional + kw args mix — assert both.
        kwargs = session.test_provider.call_args.kwargs
        self.assertEqual(kwargs.get("override_model"), "gpt-5-mini")
        self.assertEqual(kwargs.get("override_context_window"), 65_536)

    def test_test_unknown_returns_404(self) -> None:
        client, session = _build_client()
        session.test_provider.side_effect = KeyError("unknown")
        resp = client.post("/api/llm/providers/missing/test", json={})
        self.assertEqual(resp.status_code, 404)


class GetRoutesTests(unittest.TestCase):
    def test_returns_routes(self) -> None:
        client, _ = _build_client()
        resp = client.get("/api/llm/routes")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("routes", body)
        self.assertIn("main_chat", body["routes"])
        self.assertIn("worker_default", body["routes"])


class PatchRouteTests(unittest.TestCase):
    def test_patch_route_calls_session(self) -> None:
        client, session = _build_client()
        session.update_route.return_value = _SAMPLE_ROUTES["main_chat"]
        resp = client.patch(
            "/api/llm/routes/main_chat",
            json={
                "provider_id": "openai",
                "model": "gpt-5-nano",
                "context_window": 65_536,
            },
        )
        self.assertEqual(resp.status_code, 200)
        session.update_route.assert_called_once()
        role, payload = session.update_route.call_args.args
        self.assertEqual(role, "main_chat")
        self.assertEqual(payload.get("model"), "gpt-5-nano")

    def test_patch_route_unknown_provider_returns_404(self) -> None:
        client, session = _build_client()
        session.update_route.side_effect = KeyError("provider not found")
        resp = client.patch(
            "/api/llm/routes/main_chat",
            json={"provider_id": "missing", "model": "x"},
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()

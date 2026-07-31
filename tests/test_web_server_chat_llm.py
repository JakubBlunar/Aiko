"""Tests for the LLM provider REST surface.

Covers ``GET /api/settings`` masking, ``GET /api/llm/presets``,
``GET /api/models?provider=``, ``GET /api/models/required``,
``POST /api/models/pull`` and ``POST /api/llm/test-connection``.

The legacy ``chat_llm`` block and its ``PUT /api/settings/llm-credentials``
endpoint are gone: credentials now live per provider in the catalogue
(``PUT /api/llm/providers/{id}/credentials``), and role assignments go
through ``PATCH /api/llm/routes/{role}``.
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
    """The derived transport template on ``AppSettings.ollama``."""

    temperature: float = 0.6
    base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3.5:9b"
    timeout: int = 300


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
        "recommended_models": ["qwen3.5:9b"],
        "env_hint": "",
        "api_key_required": False,
        "free_tier": "Unlimited (local)",
        "docs_url": "https://ollama.com",
        "default_context_window": 65_536,
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
        "default_context_window": 131_072,
    },
]


def _build_client() -> tuple[TestClient, MagicMock, _SettingsStub]:
    settings = _SettingsStub()
    session = MagicMock()
    session._settings = settings
    session.session_key = "u:s"
    session.effective_chat_model = "qwen3.5:9b"
    session.context_window_size = 65_536
    session.context_window_source = "route"
    session.max_tokens = 512
    session.tts_provider = "fake"
    session.tts_voice = "fake"
    session.stt_model = "fake"
    session.vad_level_threshold = 0.02
    session.vad_silence_seconds = 1.0
    session.barge_in_enabled.return_value = False
    session.available_tool_names.return_value = []
    # GET /api/settings also embeds the masked weather snapshot; return a
    # serialisable dict so FastAPI's JSON encoder doesn't choke on a mock.
    session._weather_public_snapshot.return_value = {
        "provider": "open_meteo",
        "location_name": "",
        "has_api_key": False,
    }
    session.provider_presets.return_value = _SAMPLE_PRESETS
    session.list_chat_models.return_value = ["qwen3.5:9b"]
    session.required_models.return_value = {
        "provider_id": "local_ollama",
        "base_url": "http://127.0.0.1:11434",
        "reachable": True,
        "installed": ["qwen3.5:9b"],
        "required": [
            {
                "role": "main_chat",
                "model": "qwen3.5:9b",
                "provider_id": "local_ollama",
                "installed": True,
            },
            {
                "role": "embedding",
                "model": "qwen3-embedding:0.6b",
                "provider_id": "local_ollama",
                "installed": False,
            },
        ],
    }

    app = create_web_app(session)
    return TestClient(app), session, settings


class GetSettingsTests(unittest.TestCase):
    def test_get_no_longer_exposes_a_chat_llm_block(self) -> None:
        # The block was retired; anything reading it would be reading a
        # value that no longer drives the chat client.
        client, _session, _settings = _build_client()
        response = client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("chat_llm", response.json())

    def test_get_includes_masked_search_block(self) -> None:
        client, _session, _settings = _build_client()
        body = client.get("/api/settings").json()
        self.assertIn("search", body)
        self.assertIn("has_api_key", body["search"])
        self.assertNotIn("api_key", body["search"])
        self.assertEqual(body["search"]["provider"], "duckduckgo")


class SearchCredentialsTests(unittest.TestCase):
    def test_put_search_credentials_reconfigures_and_broadcasts(self) -> None:
        client, session, _settings = _build_client()
        masked = {"provider": "langsearch", "has_api_key": True}
        session.reconfigure_search.return_value = masked
        response = client.put(
            "/api/settings/search-credentials",
            json={"api_key": "ls-secret-key"},
        )
        self.assertEqual(response.status_code, 200)
        session.reconfigure_search.assert_called_once()
        called = session.reconfigure_search.call_args[0][0]
        self.assertEqual(called["api_key"], "ls-secret-key")
        self.assertTrue(response.json()["has_api_key"])

    def test_put_search_credentials_rejects_whitespace_key(self) -> None:
        client, session, _settings = _build_client()
        response = client.put(
            "/api/settings/search-credentials",
            json={"api_key": "has space"},
        )
        self.assertEqual(response.status_code, 400)
        session.reconfigure_search.assert_not_called()

    def test_patch_search_strips_api_key(self) -> None:
        client, session, _settings = _build_client()
        session.reconfigure_search.return_value = {"provider": "langsearch"}
        client.patch(
            "/api/settings",
            json={"search": {"provider": "langsearch", "api_key": "leak"}},
        )
        session.reconfigure_search.assert_called_once()
        called = session.reconfigure_search.call_args[0][0]
        self.assertNotIn("api_key", called)
        self.assertEqual(called["provider"], "langsearch")


class RetiredEndpointTests(unittest.TestCase):
    def test_llm_credentials_endpoint_is_gone(self) -> None:
        # Per-provider credentials replaced it. A stale client hitting
        # the old path must fail loudly rather than silently no-op.
        client, _session, _settings = _build_client()
        response = client.put(
            "/api/settings/llm-credentials", json={"api_key": "sk-test"},
        )
        self.assertEqual(response.status_code, 405)

    def test_patch_settings_ignores_a_chat_llm_payload(self) -> None:
        client, session, _settings = _build_client()
        response = client.patch(
            "/api/settings",
            json={"chat_llm": {"model": "gpt-4o-mini", "api_key": "leak"}},
        )
        self.assertEqual(response.status_code, 200)
        session.update_route.assert_not_called()


class RouteUpdateTests(unittest.TestCase):
    """``PATCH /api/llm/routes/{role}`` is the single mutation path for
    "which model serves this role"."""

    def test_patch_route_delegates_to_update_route(self) -> None:
        client, session, _settings = _build_client()
        session.update_route.return_value = {
            "provider_id": "local_ollama",
            "model": "qwen3.5:9b",
            "context_window": 65_536,
            "max_tokens": 512,
            "temperature": None,
            "reasoning_effort": "",
        }
        response = client.patch(
            "/api/llm/routes/main_chat",
            json={"model": "qwen3.5:9b", "context_window": 65_536},
        )
        self.assertEqual(response.status_code, 200)
        session.update_route.assert_called_once()
        role, patch_payload = session.update_route.call_args.args
        self.assertEqual(role, "main_chat")
        self.assertEqual(patch_payload.get("model"), "qwen3.5:9b")
        self.assertEqual(patch_payload.get("context_window"), 65_536)

    def test_patch_unknown_route_returns_404(self) -> None:
        client, session, _settings = _build_client()
        session.update_route.side_effect = KeyError("unknown role")
        response = client.patch(
            "/api/llm/routes/does_not_exist", json={"model": "x"},
        )
        self.assertEqual(response.status_code, 404)


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
        client.get("/api/models?provider=local_ollama")
        session.list_chat_models.assert_called_with(provider="local_ollama")

    def test_models_no_provider_uses_default(self) -> None:
        client, session, _settings = _build_client()
        client.get("/api/models")
        session.list_chat_models.assert_called_with(refresh=False)

    def test_required_models_reports_install_state(self) -> None:
        # The first-run model step needs "configured but not pulled" to
        # be visible; /api/models deliberately blurs that.
        client, _session, _settings = _build_client()
        body = client.get("/api/models/required").json()
        self.assertTrue(body["reachable"])
        by_role = {row["role"]: row for row in body["required"]}
        self.assertTrue(by_role["main_chat"]["installed"])
        self.assertFalse(by_role["embedding"]["installed"])


class PullModelTests(unittest.TestCase):
    """``POST /api/models/pull`` returns 202 and streams progress over
    the WebSocket — a multi-GB download outlives any HTTP timeout."""

    def test_pull_returns_202_and_starts_a_worker(self) -> None:
        client, session, _settings = _build_client()
        response = client.post(
            "/api/models/pull", json={"model": "qwen3.5:9b"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["model"], "qwen3.5:9b")
        session.validate_pull_target.assert_called_once()

    def test_pull_requires_a_model(self) -> None:
        client, session, _settings = _build_client()
        response = client.post("/api/models/pull", json={})
        self.assertEqual(response.status_code, 400)
        session.validate_pull_target.assert_not_called()

    def test_unknown_provider_returns_404(self) -> None:
        client, session, _settings = _build_client()
        session.validate_pull_target.side_effect = KeyError("unknown provider")
        response = client.post(
            "/api/models/pull",
            json={"model": "qwen3.5:9b", "provider_id": "nope"},
        )
        self.assertEqual(response.status_code, 404)

    def test_hosted_provider_returns_400(self) -> None:
        # Nothing to download for an OpenAI-compatible endpoint.
        client, session, _settings = _build_client()
        session.validate_pull_target.side_effect = ValueError(
            "provider 'openai' is openai_compatible",
        )
        response = client.post(
            "/api/models/pull",
            json={"model": "gpt-5-mini", "provider_id": "openai"},
        )
        self.assertEqual(response.status_code, 400)


class TestConnectionTests(unittest.TestCase):
    """``POST /api/llm/test-connection`` happy + failure paths.

    Heart of the contract: the endpoint **never** persists the
    candidate creds and never touches the saved catalogue or the live
    clients — it builds a throwaway probe client instead.
    """

    _PROBE = "app.web.rest.sessions_settings_routes.build_probe_client"

    def _stub_probe(self, **attrs: Any) -> MagicMock:
        stub = MagicMock()
        for key, value in attrs.items():
            setattr(stub, key, value)
        return stub

    def test_happy_path_returns_success(self) -> None:
        client, session, _settings = _build_client()
        stub = MagicMock()
        stub.chat_with_tools.return_value = MagicMock(
            content="ok", tool_calls=[],
        )
        stub.last_usage = MagicMock(prompt_tokens=4, completion_tokens=1)
        with patch(self._PROBE, return_value=stub), patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
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
        session.update_route.assert_not_called()

    def test_probe_provider_carries_the_candidate_credentials(self) -> None:
        # The probe row is synthesised from the request body, not read
        # back from the catalogue — otherwise "test before saving"
        # would silently test the saved config instead.
        client, _session, _settings = _build_client()
        stub = MagicMock()
        stub.chat_with_tools.return_value = MagicMock(content="ok")
        stub.last_usage = MagicMock(prompt_tokens=1, completion_tokens=1)
        with patch(self._PROBE, return_value=stub) as build:
            client.post(
                "/api/llm/test-connection",
                json={
                    "provider": "openai_compatible",
                    "model": "gpt-5-mini",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-candidate",
                    "api_style": "responses",
                },
            )
        probe_provider = build.call_args.args[0]
        self.assertEqual(probe_provider.kind, "openai_compatible")
        self.assertEqual(probe_provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(probe_provider.api_key, "sk-candidate")
        self.assertEqual(probe_provider.api_style, "responses")

    def test_unauthorized_returns_structured_error(self) -> None:
        client, _session, _settings = _build_client()
        stub = MagicMock()
        http_resp = MagicMock()
        http_resp.status_code = 401
        stub.chat_with_tools.side_effect = requests.HTTPError(
            "401 Unauthorized", response=http_resp,
        )
        with patch(self._PROBE, return_value=stub):
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
        with patch(self._PROBE, return_value=stub):
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

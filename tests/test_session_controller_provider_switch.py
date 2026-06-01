"""Unit tests for the LLM-provider routing layer in SessionController.

Covers three surfaces:

1. ``_build_chat_client`` module-level factory — returns the right
   concrete client for the configured provider + handles the
   "openai_compatible but model is empty" fallback.
2. ``SessionController._chat_llm_public_snapshot`` — masks the saved
   API key behind a boolean.
3. ``SessionController.reconfigure_chat_llm`` — mutates the settings
   in place, rebuilds the clients, calls ``persist_user_overrides``
   exactly once, and rebinds TurnRunner + ProactiveDirector via their
   ``update_runtime(client=...)`` paths.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.infra.settings import ChatLlmSettings, load_settings
from app.core.session.session_controller import (
    SessionController,
    _build_chat_client,
)
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import OpenAICompatibleClient


@dataclass
class _ChatLlmStub:
    """Minimum surface that ``_build_chat_client`` reads.

    Real :class:`ChatLlmSettings` works just as well; using a stub
    here keeps the tests self-documenting about what fields matter.
    """

    provider: str = "ollama"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    keep_alive: str = "30m"
    workers_use_local: bool = True
    provider_preset: str = ""
    context_window: int | None = None
    temperature: float | None = None
    max_tokens: int = 512


class BuildChatClientFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ollama_settings = load_settings().ollama
        # Ensure no leftover env vars pollute "api_key resolution".
        for var in (
            "OLLAMA_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
            "GROQ_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY",
        ):
            os.environ.pop(var, None)

    def test_default_ollama_returns_ollama_client(self) -> None:
        cfg = _ChatLlmStub(provider="ollama")
        client = _build_chat_client(
            chat_llm=cfg, ollama_settings=self.ollama_settings, role="chat",
        )
        self.assertIsInstance(client, OllamaClient)

    def test_openai_compatible_with_model_returns_openai_client(self) -> None:
        cfg = _ChatLlmStub(
            provider="openai_compatible",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )
        client = _build_chat_client(
            chat_llm=cfg, ollama_settings=self.ollama_settings, role="chat",
        )
        self.assertIsInstance(client, OpenAICompatibleClient)

    def test_openai_compatible_with_empty_model_falls_back_to_ollama(
        self,
    ) -> None:
        cfg = _ChatLlmStub(
            provider="openai_compatible",
            model="",
            base_url="https://api.openai.com/v1",
        )
        client = _build_chat_client(
            chat_llm=cfg, ollama_settings=self.ollama_settings, role="chat",
        )
        self.assertIsInstance(client, OllamaClient)

    def test_explicit_api_key_wins_over_env(self) -> None:
        # Set both an env var and an explicit key; the explicit one
        # should be the one that lands on the client.
        os.environ["OPENAI_API_KEY"] = "env-key"
        try:
            cfg = _ChatLlmStub(
                provider="openai_compatible",
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="explicit-key",
            )
            client = _build_chat_client(
                chat_llm=cfg, ollama_settings=self.ollama_settings,
                role="chat",
            )
            assert isinstance(client, OpenAICompatibleClient)
            self.assertEqual(
                client._headers.get("Authorization"),
                "Bearer explicit-key",
            )
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_env_var_used_when_explicit_key_blank(self) -> None:
        os.environ["GEMINI_API_KEY"] = "AIza-env"
        try:
            cfg = _ChatLlmStub(
                provider="openai_compatible",
                model="gemini-2.5-flash-lite",
                base_url=(
                    "https://generativelanguage.googleapis.com/v1beta/openai/"
                ),
                api_key="",
            )
            client = _build_chat_client(
                chat_llm=cfg, ollama_settings=self.ollama_settings,
                role="chat",
            )
            assert isinstance(client, OpenAICompatibleClient)
            self.assertEqual(
                client._headers.get("Authorization"),
                "Bearer AIza-env",
            )
        finally:
            os.environ.pop("GEMINI_API_KEY", None)


class PublicSnapshotTests(unittest.TestCase):
    def test_snapshot_masks_api_key(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = MagicMock()
        controller._settings.chat_llm = ChatLlmSettings(
            provider="openai_compatible",
            provider_preset="gemini",
            model="gemini-2.5-flash-lite",
            base_url=(
                "https://generativelanguage.googleapis.com/v1beta/openai/"
            ),
            api_key="AIza-secret-123",
            api_key_env="",
            max_tokens=512,
            temperature=None,
            context_window=None,
            extra_headers={"X-Title": "Aiko"},
            keep_alive="30m",
            workers_use_local=True,
        )
        snap = controller._chat_llm_public_snapshot()
        self.assertNotIn("api_key", snap)
        self.assertTrue(snap["has_api_key"])
        self.assertEqual(snap["provider"], "openai_compatible")
        self.assertEqual(snap["provider_preset"], "gemini")
        self.assertEqual(snap["model"], "gemini-2.5-flash-lite")
        self.assertTrue(snap["workers_use_local"])
        self.assertEqual(snap["extra_headers"], {"X-Title": "Aiko"})

    def test_snapshot_has_api_key_false_when_unset(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = MagicMock()
        controller._settings.chat_llm = ChatLlmSettings(
            provider="ollama", api_key="",
        )
        snap = controller._chat_llm_public_snapshot()
        self.assertFalse(snap["has_api_key"])


class ProviderPresetsTests(unittest.TestCase):
    def test_presets_contain_curated_set(self) -> None:
        ids = {p["id"] for p in SessionController.provider_presets()}
        # The five anchor providers the docs reference must be in the
        # catalogue.
        for needed in ("ollama", "gemini", "openai", "groq", "openrouter"):
            self.assertIn(needed, ids)

    def test_presets_include_required_fields(self) -> None:
        for preset in SessionController.provider_presets():
            for required in (
                "id", "label", "provider", "base_url",
                "recommended_models", "api_key_required", "free_tier",
            ):
                self.assertIn(required, preset, f"missing {required} in {preset}")
            self.assertIsInstance(preset["recommended_models"], list)


class ReconfigureChatLlmTests(unittest.TestCase):
    """``reconfigure_chat_llm`` is the one chat-LLM mutation entry point.

    We stub out the heavy machinery (turn_runner, proactive, persist)
    and only verify the call sequence: settings mutated -> persist
    called once -> clients rebuilt -> set_chat_model cascade.
    """

    def _make_stub_controller(
        self,
        *,
        initial_provider: str = "ollama",
        initial_model: str = "llama3.1:8b",
    ) -> SessionController:
        controller = SessionController.__new__(SessionController)
        settings = load_settings()
        settings.chat_llm.provider = initial_provider
        settings.chat_llm.model = initial_model
        controller._settings = settings
        controller._chat_provider = initial_provider
        # Build a real initial Ollama client; reconfigure() will replace it.
        controller._chat_client = OllamaClient(settings.ollama)
        controller._worker_client = controller._chat_client
        controller._ollama = controller._chat_client
        controller._effective_chat_model = initial_model
        controller._context_window = 8192
        controller._context_source = "fallback"
        controller._models_cache = ["x"]
        # Stub the runtime objects that ``set_chat_model`` touches.
        controller._turn_runner = MagicMock()
        controller._proactive = MagicMock()
        controller._summary_worker = MagicMock()
        controller._memory_extractor = None
        controller._dialogue_act_tagger = None
        return controller

    def test_reconfigure_persists_and_rebuilds_clients(self) -> None:
        controller = self._make_stub_controller()
        with patch(
            "app.core.session.session_controller.persist_user_overrides",
        ) as persist, patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            snapshot = controller.reconfigure_chat_llm({
                "provider": "openai_compatible",
                "model": "gemini-2.5-flash-lite",
                "base_url": (
                    "https://generativelanguage.googleapis.com/v1beta/openai/"
                ),
                "api_key": "AIza-test",
                "workers_use_local": True,
                "provider_preset": "gemini",
            })
        # Settings mutated in place.
        cfg = controller._settings.chat_llm
        self.assertEqual(cfg.provider, "openai_compatible")
        self.assertEqual(cfg.model, "gemini-2.5-flash-lite")
        self.assertEqual(cfg.api_key, "AIza-test")
        self.assertEqual(cfg.provider_preset, "gemini")
        # persist_user_overrides called exactly once.
        persist.assert_called_once()
        # New chat client is the OpenAI-compatible variant.
        self.assertIsInstance(controller._chat_client, OpenAICompatibleClient)
        # Worker client points at a fresh local OllamaClient because
        # workers_use_local=True.
        self.assertIsInstance(controller._worker_client, OllamaClient)
        self.assertIsNot(controller._worker_client, controller._chat_client)
        # Back-compat alias.
        self.assertIs(controller._ollama, controller._worker_client)
        # TurnRunner + ProactiveDirector were pointed at the new client.
        controller._turn_runner.update_runtime.assert_any_call(
            client=controller._chat_client,
        )
        controller._proactive.update_runtime.assert_any_call(
            client=controller._chat_client,
        )
        # Models cache was invalidated.
        self.assertIsNone(controller._models_cache)
        # Snapshot doesn't leak the api_key.
        self.assertNotIn("api_key", snapshot)
        self.assertTrue(snapshot["has_api_key"])

    def test_reconfigure_workers_use_local_false_shares_client(self) -> None:
        controller = self._make_stub_controller()
        with patch(
            "app.core.session.session_controller.persist_user_overrides",
        ):
            controller.reconfigure_chat_llm({
                "provider": "openai_compatible",
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "workers_use_local": False,
            })
        # When ``workers_use_local=False`` the worker client is the
        # same instance as the chat client.
        self.assertIs(controller._worker_client, controller._chat_client)

    def test_reconfigure_back_to_ollama_resets_workers(self) -> None:
        controller = self._make_stub_controller(
            initial_provider="openai_compatible",
            initial_model="gpt-4o-mini",
        )
        with patch(
            "app.core.session.session_controller.persist_user_overrides",
        ):
            controller.reconfigure_chat_llm({
                "provider": "ollama",
                "model": "llama3.1:8b",
            })
        self.assertIsInstance(controller._chat_client, OllamaClient)
        # Both clients are now the same Ollama instance.
        self.assertIs(controller._worker_client, controller._chat_client)
        self.assertEqual(controller._chat_provider, "ollama")


if __name__ == "__main__":
    unittest.main()

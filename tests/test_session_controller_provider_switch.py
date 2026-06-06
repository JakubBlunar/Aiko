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
                "default_context_window",
            ):
                self.assertIn(required, preset, f"missing {required} in {preset}")
            self.assertIsInstance(preset["recommended_models"], list)
            # ``default_context_window`` is ``None`` for Ollama
            # presets (auto-detect via ``/api/show``) and a positive
            # int for OpenAI-compat cloud providers (conservative cap).
            ctx = preset["default_context_window"]
            self.assertTrue(ctx is None or (isinstance(ctx, int) and ctx > 0))

    def test_openai_preset_recommends_gpt5_family(self) -> None:
        """The OpenAI preset's recommended_models lead with the cost-conscious
        GPT-5 / GPT-4.1 mini-tier shortlist so the dropdown surfaces them
        even when ``/v1/models`` doesn't include them for an account."""
        openai = next(
            p for p in SessionController.provider_presets() if p["id"] == "openai"
        )
        self.assertIn("gpt-5-mini", openai["recommended_models"])
        self.assertIn("gpt-5-nano", openai["recommended_models"])
        self.assertIn("gpt-4.1-mini", openai["recommended_models"])
        self.assertIn("gpt-4.1-nano", openai["recommended_models"])
        self.assertEqual(openai["default_context_window"], 131_072)


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
        controller._effective_worker_model = initial_model
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
        # persist_user_overrides is now called twice: once for the
        # legacy ``chat_llm`` block (back-compat) and once for the new
        # ``llm.providers`` + ``llm.routes`` catalogue mirror (PR 2).
        self.assertEqual(persist.call_count, 2)
        legacy_call = persist.call_args_list[0]
        self.assertIn("chat_llm", legacy_call.args[0])
        catalogue_call = persist.call_args_list[1]
        self.assertIn("llm", catalogue_call.args[0])
        self.assertIn("providers", catalogue_call.args[0]["llm"])
        self.assertIn("routes", catalogue_call.args[0]["llm"])
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

    def test_remote_chat_keeps_worker_model_on_local_ollama(self) -> None:
        # Regression: when chat moves to a remote provider AND
        # ``workers_use_local=True``, the worker model must remain
        # pinned to ``ollama.chat_model`` — sending the remote
        # model name (``gpt-5-mini``) to local Ollama 404s with
        # ``model 'gpt-5-mini' not found``. Symptom in production
        # was the per-turn ``app.llm.ollama_client`` error cluster
        # right after a successful chat reply.
        controller = self._make_stub_controller()
        controller._settings.ollama.chat_model = "llama3.1:8b"
        with patch(
            "app.core.session.session_controller.persist_user_overrides",
        ), patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            controller.reconfigure_chat_llm({
                "provider": "openai_compatible",
                "model": "gpt-5-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "workers_use_local": True,
            })
        # Chat model follows the route; worker model stays on local.
        self.assertEqual(controller._effective_chat_model, "gpt-5-mini")
        self.assertEqual(controller._effective_worker_model, "llama3.1:8b")
        # Worker cascade propagates the WORKER model, not the chat one.
        controller._summary_worker._model = "leftover-old-name"
        controller.set_chat_model("gpt-5-nano")
        self.assertEqual(controller._effective_chat_model, "gpt-5-nano")
        # Worker model unchanged — gpt-5-nano is a remote-only name
        # and local Ollama doesn't have it.
        self.assertEqual(controller._effective_worker_model, "llama3.1:8b")
        # Legacy ``ollama.chat_model`` not stomped by the remote name.
        self.assertEqual(
            controller._settings.ollama.chat_model, "llama3.1:8b",
        )

    def test_pure_ollama_chat_model_change_cascades_to_workers(self) -> None:
        # Inverse of the regression above: when chat and workers share
        # the same Ollama client, a chat-model change MUST also flip
        # the worker model — the two are literally the same backend.
        controller = self._make_stub_controller(
            initial_provider="ollama",
            initial_model="llama3.1:8b",
        )
        self.assertIs(controller._worker_client, controller._chat_client)
        with patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            controller.set_chat_model("llama3.1:70b")
        self.assertEqual(controller._effective_chat_model, "llama3.1:70b")
        self.assertEqual(controller._effective_worker_model, "llama3.1:70b")
        # Pure-Ollama: legacy field tracks the chat model.
        self.assertEqual(
            controller._settings.ollama.chat_model, "llama3.1:70b",
        )


class ResolveContextWindowTests(unittest.TestCase):
    """``_resolve_context_window`` decides what budget the prompt assembler
    uses. The precedence is: explicit override > client lookup > 8192."""

    def _make_controller(
        self, *, chat_client: Any,
    ) -> SessionController:
        controller = SessionController.__new__(SessionController)
        controller._chat_client = chat_client
        return controller

    def test_explicit_override_wins(self) -> None:
        client = MagicMock()
        client.get_context_length.return_value = 999_999
        controller = self._make_controller(chat_client=client)
        window, source = controller._resolve_context_window(
            override=42_000, model="gpt-5-mini",
        )
        self.assertEqual(window, 42_000)
        self.assertEqual(source, "config")
        # Override path short-circuits — the client lookup is not called.
        client.get_context_length.assert_not_called()

    def test_zero_override_falls_through_to_client(self) -> None:
        client = MagicMock()
        client.get_context_length.return_value = 131_072
        controller = self._make_controller(chat_client=client)
        window, source = controller._resolve_context_window(
            override=0, model="gpt-5-mini",
        )
        self.assertEqual(window, 131_072)
        self.assertEqual(source, "client")
        client.get_context_length.assert_called_once_with("gpt-5-mini")

    def test_none_override_uses_client(self) -> None:
        """An OpenAI-compat client now returns a positive cap for
        known cloud models. The source label should be ``client``
        (not the legacy ``ollama_show``)."""
        client = MagicMock()
        client.get_context_length.return_value = 131_072
        controller = self._make_controller(chat_client=client)
        window, source = controller._resolve_context_window(
            override=None, model="gpt-4.1-mini",
        )
        self.assertEqual(window, 131_072)
        self.assertEqual(source, "client")

    def test_client_returns_none_falls_back_to_8192(self) -> None:
        client = MagicMock()
        client.get_context_length.return_value = None
        controller = self._make_controller(chat_client=client)
        window, source = controller._resolve_context_window(
            override=None, model="totally-unknown-model",
        )
        self.assertEqual(window, 8192)
        self.assertEqual(source, "fallback")

    def test_client_raises_falls_back_to_8192(self) -> None:
        """A misbehaving client (network glitch, bad JSON) must not
        crash the controller — we swallow the exception and fall
        back to the hardcoded default."""
        client = MagicMock()
        client.get_context_length.side_effect = RuntimeError("boom")
        controller = self._make_controller(chat_client=client)
        window, source = controller._resolve_context_window(
            override=None, model="any",
        )
        self.assertEqual(window, 8192)
        self.assertEqual(source, "fallback")

    def test_negative_override_treated_as_no_override(self) -> None:
        """Defensive: a negative integer override is invalid; we
        ignore it rather than echoing back a silly negative budget."""
        client = MagicMock()
        client.get_context_length.return_value = 131_072
        controller = self._make_controller(chat_client=client)
        window, source = controller._resolve_context_window(
            override=-100, model="gpt-5-mini",
        )
        self.assertEqual(window, 131_072)
        self.assertEqual(source, "client")


if __name__ == "__main__":
    unittest.main()

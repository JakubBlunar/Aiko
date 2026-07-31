"""Unit tests for the LLM-routing layer in SessionController.

Covers three surfaces:

1. :func:`app.llm.factory.build_client_for_route` — returns the right
   concrete client for the route's provider, and handles the
   "openai_compatible but model is empty" fallback.
2. ``SessionController.update_route`` — the single mutation path for
   role assignments: validates against the catalogue, persists once,
   rebuilds the live clients, and rebinds TurnRunner + ProactiveDirector
   via their ``update_runtime(client=...)`` paths.
3. ``_resolve_context_window`` — the budget the prompt assembler uses.
"""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.infra.settings import (
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    LlmProvider,
    LlmRoute,
    LlmSettings,
    load_settings,
)
from app.core.session.session_controller import SessionController
from app.llm.factory import ClientCache, build_client_for_route
from app.llm.llm_gate import (
    CONVERSATION_WORKER,
    MAINTENANCE_WORKER,
    TASK,
    GatedChatClient,
)
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import OpenAICompatibleClient


_REMOTE_ENV_VARS = (
    "OLLAMA_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY",
)


def _provider(
    provider_id: str, kind: str, base_url: str, **extra: Any,
) -> LlmProvider:
    return LlmProvider(
        id=provider_id,
        name=provider_id.replace("_", " ").title(),
        kind=kind,
        base_url=base_url,
        **extra,
    )


def _catalogue(*providers: LlmProvider, **routes: LlmRoute) -> LlmSettings:
    return LlmSettings(providers=list(providers), routes=dict(routes))


class BuildClientForRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        # Ensure no leftover env vars pollute "api_key resolution".
        for var in _REMOTE_ENV_VARS:
            os.environ.pop(var, None)
        self.cache = ClientCache()

    def _build(self, provider: LlmProvider, model: str) -> Any:
        settings = _catalogue(
            provider,
            main_chat=LlmRoute(provider_id=provider.id, model=model),
        )
        return build_client_for_route(
            self.cache,
            route=settings.routes[LLM_ROLE_MAIN_CHAT],
            settings=settings,
        )

    def test_ollama_provider_returns_ollama_client(self) -> None:
        provider = _provider(
            "local_ollama", "ollama", "http://127.0.0.1:11434",
        )
        self.assertIsInstance(self._build(provider, "qwen3.5:9b"), OllamaClient)

    def test_openai_compatible_with_model_returns_openai_client(self) -> None:
        provider = _provider(
            "openai", "openai_compatible", "https://api.openai.com/v1",
            api_key="sk-test",
        )
        self.assertIsInstance(
            self._build(provider, "gpt-4o-mini"), OpenAICompatibleClient,
        )

    def test_openai_compatible_with_empty_model_falls_back_to_ollama(
        self,
    ) -> None:
        # A half-configured route must not crash boot: the user gets a
        # meaningful error from the drawer when they try to chat.
        provider = _provider(
            "openai", "openai_compatible", "https://api.openai.com/v1",
        )
        self.assertIsInstance(self._build(provider, ""), OllamaClient)

    def test_unknown_provider_id_raises_key_error(self) -> None:
        settings = _catalogue(
            _provider("local_ollama", "ollama", "http://127.0.0.1:11434"),
            main_chat=LlmRoute(provider_id="ghost", model="x"),
        )
        with self.assertRaises(KeyError):
            build_client_for_route(
                self.cache,
                route=settings.routes[LLM_ROLE_MAIN_CHAT],
                settings=settings,
            )

    def test_explicit_api_key_wins_over_env(self) -> None:
        os.environ["OPENAI_API_KEY"] = "env-key"
        try:
            provider = _provider(
                "openai", "openai_compatible",
                "https://api.openai.com/v1", api_key="explicit-key",
            )
            client = self._build(provider, "gpt-4o-mini")
            assert isinstance(client, OpenAICompatibleClient)
            self.assertEqual(
                client._headers.get("Authorization"), "Bearer explicit-key",
            )
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_env_var_used_when_explicit_key_blank(self) -> None:
        os.environ["GEMINI_API_KEY"] = "AIza-env"
        try:
            provider = _provider(
                "gemini", "openai_compatible",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            )
            client = self._build(provider, "gemini-2.5-flash-lite")
            assert isinstance(client, OpenAICompatibleClient)
            self.assertEqual(
                client._headers.get("Authorization"), "Bearer AIza-env",
            )
        finally:
            os.environ.pop("GEMINI_API_KEY", None)


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
            ctx = preset["default_context_window"]
            self.assertTrue(ctx is None or (isinstance(ctx, int) and ctx > 0))

    def test_local_ollama_preset_caps_the_context_window(self) -> None:
        # Auto-detect asks Ollama for the model's advertised maximum,
        # and current Qwen tags advertise 256 k -- a KV cache that size
        # spills a 9B model out of any consumer GPU.
        ollama = next(
            p for p in SessionController.provider_presets()
            if p["id"] == "ollama"
        )
        self.assertEqual(ollama["default_context_window"], 65_536)

    def test_openai_preset_recommends_gpt5_family(self) -> None:
        openai = next(
            p for p in SessionController.provider_presets() if p["id"] == "openai"
        )
        self.assertIn("gpt-5-mini", openai["recommended_models"])
        self.assertIn("gpt-5-nano", openai["recommended_models"])
        self.assertIn("gpt-4.1-mini", openai["recommended_models"])
        self.assertIn("gpt-4.1-nano", openai["recommended_models"])
        self.assertEqual(openai["default_context_window"], 131_072)


class UpdateRouteTests(unittest.TestCase):
    """``update_route`` is the one role-assignment mutation entry point.

    We stub out the heavy machinery (turn_runner, proactive, persist)
    and only verify the call sequence: route mutated -> persist called
    -> clients rebuilt -> set_chat_model cascade.
    """

    def _make_stub_controller(self) -> SessionController:
        controller = SessionController.__new__(SessionController)
        settings = load_settings()
        settings.llm.providers = [
            _provider("local_ollama", "ollama", "http://127.0.0.1:11434"),
            _provider(
                "openai", "openai_compatible",
                "https://api.openai.com/v1", api_key="sk-test",
            ),
        ]
        settings.llm.routes = {
            LLM_ROLE_MAIN_CHAT: LlmRoute(
                provider_id="local_ollama", model="llama3.1:8b",
                context_window=32_768,
            ),
            LLM_ROLE_WORKER_DEFAULT: LlmRoute(
                provider_id="local_ollama", model="llama3.1:8b",
                context_window=32_768,
            ),
        }
        controller._settings = settings
        controller._chat_provider = "ollama"
        controller._chat_client = OllamaClient(settings.ollama)
        controller._client_cache = ClientCache(settings.ollama)
        # Mirror the real constructor's worker-client install: the
        # worker references are gate proxies around the inner client.
        controller._install_worker_clients(controller._chat_client)
        controller._effective_chat_model = "llama3.1:8b"
        controller._effective_worker_model = "llama3.1:8b"
        controller._context_window = 32_768
        controller._context_source = "config"
        controller._models_cache = ["x"]
        # Stub the runtime objects that ``set_chat_model`` touches.
        controller._turn_runner = MagicMock()
        controller._proactive = MagicMock()
        controller._summary_worker = MagicMock()
        controller._memory_extractor = None
        controller._dialogue_act_tagger = None
        return controller

    def test_worker_client_proxies_have_distinct_tiers(self) -> None:
        # The three shared-gate proxy views must carry the right
        # priority so the idle-scheduler LLM workers (wired to
        # ``_maintenance_client``) yield to the per-turn conversation
        # workers (``_worker_client``), and both beat workflow TASK work.
        controller = self._make_stub_controller()
        self.assertIsInstance(controller._worker_client, GatedChatClient)
        self.assertIsInstance(controller._maintenance_client, GatedChatClient)
        self.assertIsInstance(controller._workflow_client, GatedChatClient)
        self.assertEqual(controller._worker_client._priority, CONVERSATION_WORKER)
        self.assertEqual(
            controller._maintenance_client._priority, MAINTENANCE_WORKER
        )
        self.assertEqual(controller._workflow_client._priority, TASK)
        # All three share the SAME inner client + gate (one model, one
        # fair semaphore), differing only by priority.
        self.assertIs(
            controller._worker_client._inner,
            controller._maintenance_client._inner,
        )
        self.assertIs(
            controller._worker_client._gate,
            controller._maintenance_client._gate,
        )

    def _switch_main_chat_to_openai(
        self, controller: SessionController, model: str = "gpt-5-mini",
    ) -> Any:
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ) as persist, patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            result = controller.update_route(
                LLM_ROLE_MAIN_CHAT,
                {"provider_id": "openai", "model": model,
                 "context_window": 131_072},
            )
        return result, persist

    def test_route_switch_keeps_maintenance_tier_after_retarget(self) -> None:
        # A provider switch retargets the proxies in place; the
        # maintenance proxy must keep its tier so idle workers holding
        # the reference still yield correctly.
        controller = self._make_stub_controller()
        maint_before = controller._maintenance_client
        self._switch_main_chat_to_openai(controller)
        self.assertIs(controller._maintenance_client, maint_before)
        self.assertEqual(
            controller._maintenance_client._priority, MAINTENANCE_WORKER
        )

    def test_route_switch_persists_and_rebuilds_clients(self) -> None:
        controller = self._make_stub_controller()
        result, persist = self._switch_main_chat_to_openai(controller)
        # Route mutated in place.
        route = controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT]
        self.assertEqual(route.provider_id, "openai")
        self.assertEqual(route.model, "gpt-5-mini")
        self.assertEqual(route.context_window, 131_072)
        # The whole llm block is persisted -- one write, one shape.
        self.assertGreaterEqual(persist.call_count, 1)
        payload = persist.call_args_list[-1].args[0]
        self.assertIn("llm", payload)
        self.assertIn("providers", payload["llm"])
        self.assertIn("routes", payload["llm"])
        # New chat client is the OpenAI-compatible variant.
        self.assertIsInstance(controller._chat_client, OpenAICompatibleClient)
        # Worker route still points at local Ollama, so the workers get
        # their own client. The public ``_worker_client`` is a gate
        # proxy; the underlying client lives on ``_worker_client_inner``.
        self.assertIsInstance(controller._worker_client, GatedChatClient)
        self.assertIsInstance(controller._worker_client_inner, OllamaClient)
        self.assertIsNot(controller._worker_client_inner, controller._chat_client)
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
        # The returned row echoes the saved values.
        self.assertEqual(result["provider_id"], "openai")
        self.assertEqual(result["context_window"], 131_072)

    def test_matching_routes_share_one_client(self) -> None:
        # Chat and workers on the same provider + model must resolve to
        # one client: two would mean two copies of the weights resident.
        controller = self._make_stub_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ), patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            controller.update_route(
                LLM_ROLE_WORKER_DEFAULT,
                {"provider_id": "local_ollama", "model": "llama3.1:8b"},
            )
        self.assertIs(controller._worker_client_inner, controller._chat_client)

    def test_unknown_provider_raises_key_error(self) -> None:
        controller = self._make_stub_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            with self.assertRaises(KeyError):
                controller.update_route(
                    LLM_ROLE_MAIN_CHAT, {"provider_id": "ghost"},
                )

    def test_remote_chat_keeps_worker_model_on_local_ollama(self) -> None:
        # Regression: when chat moves to a remote provider, the worker
        # model must remain whatever the worker route says -- sending
        # the remote model name (``gpt-5-mini``) to local Ollama 404s
        # with ``model 'gpt-5-mini' not found``.
        controller = self._make_stub_controller()
        self._switch_main_chat_to_openai(controller)
        self.assertEqual(controller._effective_chat_model, "gpt-5-mini")
        self.assertEqual(controller._effective_worker_model, "llama3.1:8b")
        # Worker cascade propagates the WORKER model, not the chat one.
        controller._summary_worker._model = "leftover-old-name"
        controller.set_chat_model("gpt-5-nano")
        self.assertEqual(controller._effective_chat_model, "gpt-5-nano")
        self.assertEqual(controller._effective_worker_model, "llama3.1:8b")

    def test_pure_ollama_chat_model_change_cascades_to_workers(self) -> None:
        # Inverse of the regression above: when chat and workers share
        # the same Ollama client, a chat-model change MUST also flip
        # the worker model — the two are literally the same backend.
        controller = self._make_stub_controller()
        self.assertIs(controller._worker_client_inner, controller._chat_client)
        with patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            controller.set_chat_model("llama3.1:70b")
        self.assertEqual(controller._effective_chat_model, "llama3.1:70b")
        self.assertEqual(controller._effective_worker_model, "llama3.1:70b")
        # ``set_chat_model`` writes through to the route, not to a
        # separate legacy field.
        self.assertEqual(
            controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT].model,
            "llama3.1:70b",
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

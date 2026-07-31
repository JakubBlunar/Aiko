"""Unit tests for the provider-catalogue CRUD on :class:`SessionController`.

These tests build a *stub* controller via ``SessionController.__new__``
(same pattern as :mod:`tests.test_session_controller_provider_switch`)
and exercise the public methods directly:

- :meth:`SessionController.list_providers` / :meth:`list_routes`
- :meth:`add_provider` (template + custom + id collision)
- :meth:`update_provider` (cache invalidation + live rebuild)
- :meth:`update_provider_credentials`
- :meth:`remove_provider` (can't-delete-when-referenced)
- :meth:`update_route` (the single role-assignment mutation path)
- :meth:`required_models` / :meth:`validate_pull_target`
- :meth:`client_cache_stats`

The heavy machinery (turn_runner, proactive, persist_user_overrides) is
mocked out. We never touch a real LLM endpoint.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.core.infra.settings import (
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    LlmEmbedding,
    LlmProvider,
    LlmRoute,
    LlmSettings,
    load_settings,
)
from app.core.session.session_controller import SessionController
from app.llm.factory import ClientCache
from app.llm.ollama_client import OllamaClient


def _make_controller() -> SessionController:
    """Build a bare-bones controller with a known catalogue state.

    Catalogue:
    - ``local_ollama`` (kind=ollama)
    - ``openai`` (kind=openai_compatible, has api key)

    Routes:
    - main_chat -> openai
    - worker_default -> local_ollama
    """
    controller = SessionController.__new__(SessionController)
    settings = load_settings()
    settings.llm = LlmSettings(
        providers=[
            LlmProvider(
                id="local_ollama",
                name="Local Ollama",
                kind="ollama",
                base_url="http://127.0.0.1:11434",
            ),
            LlmProvider(
                id="openai",
                name="OpenAI",
                kind="openai_compatible",
                base_url="https://api.openai.com/v1",
                api_key="sk-existing",
                api_key_env="OPENAI_API_KEY",
            ),
        ],
        routes={
            LLM_ROLE_MAIN_CHAT: LlmRoute(
                provider_id="openai",
                model="gpt-5-mini",
                context_window=131_072,
                max_tokens=512,
            ),
            LLM_ROLE_WORKER_DEFAULT: LlmRoute(
                provider_id="local_ollama",
                model="llama3.1:8b",
                context_window=65_536,
                max_tokens=512,
            ),
        },
        embedding=LlmEmbedding(
            provider_id="local_ollama",
            model="qwen3-embedding:0.6b",
            num_ctx=2048,
            num_gpu=0,
        ),
    )
    controller._settings = settings
    controller._chat_provider = "openai_compatible"
    controller._chat_client = OllamaClient(settings.ollama)
    controller._install_worker_clients(controller._chat_client)
    controller._effective_chat_model = "gpt-5-mini"
    controller._effective_worker_model = "llama3.1:8b"
    controller._context_window = 131_072
    controller._context_source = "config"
    controller._models_cache = None
    controller._missing_chat_model = ""
    controller._client_cache = ClientCache(settings.ollama)
    # Stub the runtime objects a rebuild cascades into.
    controller._turn_runner = MagicMock()
    controller._proactive = MagicMock()
    controller._summary_worker = MagicMock()
    controller._memory_extractor = None
    controller._dialogue_act_tagger = None
    return controller


class ListProvidersTests(unittest.TestCase):
    def test_list_masks_api_keys(self) -> None:
        controller = _make_controller()
        rows = controller.list_providers()
        self.assertEqual({r["id"] for r in rows}, {"local_ollama", "openai"})
        for row in rows:
            self.assertNotIn("api_key", row)
            self.assertIn("has_api_key", row)
        openai_row = next(r for r in rows if r["id"] == "openai")
        self.assertTrue(openai_row["has_api_key"])
        local_row = next(r for r in rows if r["id"] == "local_ollama")
        self.assertFalse(local_row["has_api_key"])

    def test_list_routes_returns_full_table(self) -> None:
        controller = _make_controller()
        routes = controller.list_routes()
        self.assertIn(LLM_ROLE_MAIN_CHAT, routes)
        self.assertIn(LLM_ROLE_WORKER_DEFAULT, routes)
        main = routes[LLM_ROLE_MAIN_CHAT]
        self.assertEqual(main["provider_id"], "openai")
        self.assertEqual(main["model"], "gpt-5-mini")
        self.assertEqual(main["context_window"], 131_072)


class AddProviderTests(unittest.TestCase):
    def test_add_from_template_seeds_fields(self) -> None:
        controller = _make_controller()
        # Remove the existing openai entry so the template id is free.
        controller._settings.llm.providers = [
            p for p in controller._settings.llm.providers if p.id != "openai"
        ]
        # Drop the route too so the no-reference invariant holds.
        controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT] = LlmRoute(
            provider_id="local_ollama", model="llama3.1:8b",
        )
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            entry = controller.add_provider(
                template_id="openai",
                draft={"name": "OpenAI personal"},
            )
        self.assertEqual(entry["id"], "openai")
        self.assertEqual(entry["kind"], "openai_compatible")
        self.assertEqual(entry["name"], "OpenAI personal")
        self.assertEqual(entry["base_url"], "https://api.openai.com/v1")
        # The api_key_env hint from the preset is carried.
        self.assertEqual(entry["api_key_env"], "OPENAI_API_KEY")

    def test_add_with_id_collision_raises(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ), self.assertRaises(ValueError):
            controller.add_provider(
                template_id="openai",
                draft={"id": "openai"},  # already exists
            )

    def test_add_auto_generates_id_when_template_taken(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            entry = controller.add_provider(
                template_id="openai",
                draft={"name": "OpenAI team"},
            )
        # `openai` is already taken, so the generator picks `openai_2`.
        self.assertEqual(entry["id"], "openai_2")
        ids = [p.id for p in controller._settings.llm.providers]
        self.assertIn("openai_2", ids)


class UpdateProviderTests(unittest.TestCase):
    def test_patch_non_credential_field(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            entry = controller.update_provider("openai", {"name": "Renamed"})
        self.assertEqual(entry["name"], "Renamed")
        self.assertEqual(
            controller._settings.llm.providers[1].name, "Renamed",
        )

    def test_patch_base_url_rebuilds_the_live_client(self) -> None:
        """``main_chat`` points at the patched provider, so the cached
        client is stale the moment the endpoint changes."""
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ), patch.object(
            controller._client_cache, "invalidate",
        ) as invalidate:
            controller.update_provider(
                "openai", {"base_url": "https://example.com/v1"},
            )
        self.assertEqual(
            controller._settings.llm.providers[1].base_url,
            "https://example.com/v1",
        )
        invalidate.assert_called_with("openai")
        # turn_runner + proactive were re-bound.
        controller._turn_runner.update_runtime.assert_called()
        controller._proactive.update_runtime.assert_called()

    def test_patch_unknown_raises_key_error(self) -> None:
        controller = _make_controller()
        with self.assertRaises(KeyError):
            controller.update_provider("missing", {"name": "x"})


class UpdateProviderCredentialsTests(unittest.TestCase):
    def test_credentials_rebuild_client(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ), patch.object(
            controller._client_cache, "invalidate",
        ) as invalidate:
            entry = controller.update_provider_credentials(
                "openai", {"api_key": "sk-rotated"},
            )
        # Saved key was updated.
        self.assertTrue(entry["has_api_key"])
        self.assertEqual(
            controller._settings.llm.providers[1].api_key, "sk-rotated",
        )
        # The masked row never echoes the raw key back.
        self.assertNotIn("api_key", entry)
        invalidate.assert_called_with("openai")
        controller._turn_runner.update_runtime.assert_called()

    def test_credentials_unknown_raises_key_error(self) -> None:
        controller = _make_controller()
        with self.assertRaises(KeyError):
            controller.update_provider_credentials(
                "missing", {"api_key": "x"},
            )


class RemoveProviderTests(unittest.TestCase):
    def test_remove_unreferenced_provider(self) -> None:
        controller = _make_controller()
        # Add a third provider that no route references.
        controller._settings.llm.providers.append(LlmProvider(
            id="extra", name="Extra", kind="ollama",
            base_url="http://x:11434",
        ))
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            controller.remove_provider("extra")
        ids = [p.id for p in controller._settings.llm.providers]
        self.assertNotIn("extra", ids)

    def test_remove_referenced_raises_value_error(self) -> None:
        controller = _make_controller()
        # openai is referenced by main_chat -> must refuse.
        with self.assertRaises(ValueError) as ctx:
            controller.remove_provider("openai")
        self.assertIn("main_chat", str(ctx.exception))
        # Catalogue untouched.
        ids = [p.id for p in controller._settings.llm.providers]
        self.assertIn("openai", ids)

    def test_remove_unknown_raises_key_error(self) -> None:
        controller = _make_controller()
        with self.assertRaises(KeyError):
            controller.remove_provider("does-not-exist")


class UpdateRouteTests(unittest.TestCase):
    """``update_route`` owns both the write and the live rebuild."""

    def _update(self, controller, role, draft):
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ) as persist, patch(
            "app.llm.ollama_client.OllamaClient.get_context_length",
            return_value=None,
        ):
            result = controller.update_route(role, draft)
        return result, persist

    def test_main_chat_update_rebuilds_and_cascades(self) -> None:
        controller = _make_controller()
        result, persist = self._update(
            controller,
            LLM_ROLE_MAIN_CHAT,
            {
                "provider_id": "local_ollama",
                "model": "llama3.1:70b",
                "context_window": 8192,
            },
        )
        route = controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT]
        self.assertEqual(route.provider_id, "local_ollama")
        self.assertEqual(route.model, "llama3.1:70b")
        self.assertEqual(route.context_window, 8192)
        self.assertEqual(result["model"], "llama3.1:70b")
        persist.assert_called()
        # The chat model cascade reached the turn runner.
        self.assertEqual(controller._effective_chat_model, "llama3.1:70b")
        controller._turn_runner.update_runtime.assert_called()

    def test_partial_draft_keeps_untouched_fields(self) -> None:
        # A model-only edit must not silently reset the context window
        # back to auto-detect.
        controller = _make_controller()
        self._update(controller, LLM_ROLE_MAIN_CHAT, {"model": "gpt-5-nano"})
        route = controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT]
        self.assertEqual(route.model, "gpt-5-nano")
        self.assertEqual(route.context_window, 131_072)
        self.assertEqual(route.provider_id, "openai")

    def test_worker_default_update_persists_and_retargets(self) -> None:
        controller = _make_controller()
        _result, persist = self._update(
            controller,
            LLM_ROLE_WORKER_DEFAULT,
            {"provider_id": "openai", "model": "gpt-5-nano"},
        )
        persist.assert_called()
        worker_route = controller._settings.llm.routes[LLM_ROLE_WORKER_DEFAULT]
        self.assertEqual(worker_route.provider_id, "openai")
        self.assertEqual(worker_route.model, "gpt-5-nano")
        # Chat and workers now agree, so they share one client rather
        # than holding two connections to the same endpoint.
        self.assertIs(controller._worker_client_inner, controller._chat_client)

    def test_route_unknown_provider_raises_key_error(self) -> None:
        controller = _make_controller()
        with self.assertRaises(KeyError):
            controller.update_route(
                LLM_ROLE_MAIN_CHAT,
                {"provider_id": "ghost", "model": "x"},
            )

    def test_switching_to_an_installed_model_clears_the_missing_flag(
        self,
    ) -> None:
        # The flag is a boot-time snapshot; without a refresh here the
        # onboarding banner would keep naming the model the user just
        # moved away from.
        controller = _make_controller()
        controller._missing_chat_model = "qwen3.5:9b"
        with patch.object(
            OllamaClient, "list_models", return_value=["llama3.1:8b"],
        ):
            self._update(
                controller,
                LLM_ROLE_MAIN_CHAT,
                {"provider_id": "local_ollama", "model": "llama3.1:8b"},
            )
        self.assertEqual(controller._missing_chat_model, "")

    def test_switching_to_an_absent_model_sets_the_missing_flag(self) -> None:
        controller = _make_controller()
        with patch.object(
            OllamaClient, "list_models", return_value=["llama3.1:8b"],
        ):
            self._update(
                controller,
                LLM_ROLE_MAIN_CHAT,
                {"provider_id": "local_ollama", "model": "qwen3.5:9b"},
            )
        self.assertEqual(controller._missing_chat_model, "qwen3.5:9b")

    def test_unreachable_provider_leaves_the_missing_flag_alone(self) -> None:
        # No verdict is better than a wrong one: a transient outage
        # shouldn't flip the banner on for a model that is installed.
        controller = _make_controller()
        controller._missing_chat_model = "qwen3.5:9b"
        with patch.object(
            OllamaClient, "list_models", side_effect=OSError("refused"),
        ):
            self._update(
                controller,
                LLM_ROLE_MAIN_CHAT,
                {"provider_id": "local_ollama", "model": "qwen3.5:9b"},
            )
        self.assertEqual(controller._missing_chat_model, "qwen3.5:9b")

    def test_hosted_provider_clears_the_missing_flag(self) -> None:
        # Nothing to download for a remote endpoint.
        controller = _make_controller()
        controller._missing_chat_model = "qwen3.5:9b"
        self._update(controller, LLM_ROLE_MAIN_CHAT, {"model": "gpt-5-nano"})
        self.assertEqual(controller._missing_chat_model, "")


class ApiStyleRoundTripTests(unittest.TestCase):
    """``api_style`` survives add / patch / mask."""

    def test_default_provider_masks_auto(self) -> None:
        controller = _make_controller()
        row = next(
            r for r in controller.list_providers() if r["id"] == "openai"
        )
        self.assertEqual(row.get("api_style"), "auto")

    def test_add_from_xai_template_seeds_responses(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            entry = controller.add_provider(
                template_id="xai",
                draft={"name": "Grok"},
            )
        self.assertEqual(entry["kind"], "openai_compatible")
        self.assertEqual(entry["base_url"], "https://api.x.ai/v1")
        self.assertEqual(entry["api_style"], "responses")
        # The preset's default reasoning-effort is carried too.
        self.assertEqual(entry["reasoning_effort"], "low")

    def test_patch_api_style_normalises_and_persists(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            entry = controller.update_provider(
                "openai", {"api_style": "RESPONSES"},
            )
        self.assertEqual(entry["api_style"], "responses")
        self.assertEqual(
            controller._settings.llm.providers[1].api_style, "responses",
        )

    def test_patch_bad_api_style_falls_back_to_auto(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ):
            entry = controller.update_provider(
                "openai", {"api_style": "banana"},
            )
        self.assertEqual(entry["api_style"], "auto")


class RequiredModelsTests(unittest.TestCase):
    """``required_models`` backs the first-run model step: it has to
    report what's actually downloaded, not what's configured."""

    def _with_installed(self, controller, installed: list[str]):
        probe = MagicMock()
        probe.list_models.return_value = installed
        return patch(
            "app.core.session.llm_settings_mixin.build_probe_client",
            return_value=probe,
        )

    def test_reports_missing_local_models(self) -> None:
        controller = _make_controller()
        with self._with_installed(controller, ["llama3.1:8b"]):
            report = controller.required_models()
        self.assertTrue(report["reachable"])
        by_role = {row["role"]: row for row in report["required"]}
        # main_chat is on a hosted provider -> nothing to download.
        self.assertNotIn(LLM_ROLE_MAIN_CHAT, by_role)
        self.assertTrue(by_role[LLM_ROLE_WORKER_DEFAULT]["installed"])
        self.assertFalse(by_role["embedding"]["installed"])

    def test_unreachable_ollama_reports_everything_missing(self) -> None:
        controller = _make_controller()
        probe = MagicMock()
        probe.list_models.side_effect = OSError("connection refused")
        with patch(
            "app.core.session.llm_settings_mixin.build_probe_client",
            return_value=probe,
        ):
            report = controller.required_models()
        self.assertFalse(report["reachable"])
        self.assertEqual(report["installed"], [])
        self.assertTrue(
            all(not row["installed"] for row in report["required"]),
        )


class ValidatePullTargetTests(unittest.TestCase):
    def test_defaults_to_the_local_ollama_provider(self) -> None:
        controller = _make_controller()
        provider = controller.validate_pull_target("qwen3.5:9b")
        self.assertEqual(provider.id, "local_ollama")

    def test_hosted_provider_rejected(self) -> None:
        # There's nothing to download for an OpenAI-compatible endpoint.
        controller = _make_controller()
        with self.assertRaises(ValueError):
            controller.validate_pull_target(
                "gpt-5-mini", provider_id="openai",
            )

    def test_unknown_provider_raises_key_error(self) -> None:
        controller = _make_controller()
        with self.assertRaises(KeyError):
            controller.validate_pull_target("x", provider_id="ghost")

    def test_blank_model_rejected(self) -> None:
        controller = _make_controller()
        with self.assertRaises(ValueError):
            controller.validate_pull_target("   ")


class ClientCacheStatsTests(unittest.TestCase):
    def test_stats_delegates_to_cache(self) -> None:
        controller = _make_controller()
        # Touch the cache so it has at least one entry.
        controller._client_cache.get(controller._settings.llm.providers[0])
        stats = controller.client_cache_stats()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["providers"], 1)


if __name__ == "__main__":
    unittest.main()

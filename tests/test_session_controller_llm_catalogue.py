"""Unit tests for the PR 2 catalogue CRUD on :class:`SessionController`.

These tests build a *stub* controller via ``SessionController.__new__``
(same pattern as :mod:`tests.test_session_controller_provider_switch`)
and exercise the new public methods directly:

- :meth:`SessionController.list_providers` / :meth:`list_routes`
- :meth:`add_provider` (template + custom + id collision)
- :meth:`update_provider` (cache invalidation + chat_llm mirror)
- :meth:`update_provider_credentials`
- :meth:`remove_provider` (can't-delete-when-referenced)
- :meth:`update_route` (main_chat cascades via reconfigure_chat_llm,
  worker_default is recorded but doesn't rebuild the client)
- :meth:`client_cache_stats`

The heavy machinery (turn_runner, proactive, persist_user_overrides) is
mocked out. We never touch a real LLM endpoint.
"""

from __future__ import annotations

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
from app.llm.factory import ClientCache
from app.llm.ollama_client import OllamaClient


def _make_controller() -> SessionController:
    """Build a bare-bones controller with the legacy + catalogue
    blocks pre-populated with a known starting state.

    Catalogue starts with:
    - ``local_ollama`` (kind=ollama)
    - ``openai`` (kind=openai_compatible, has api key)

    Routes:
    - main_chat -> openai
    - worker_default -> local_ollama
    """
    controller = SessionController.__new__(SessionController)
    settings = load_settings()
    # Force a known catalogue state.
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
                max_tokens=512,
            ),
        },
    )
    # Also align the legacy block with the catalogue so the
    # mirror-write logic is exercised from a sane starting point.
    settings.chat_llm.provider = "openai_compatible"
    settings.chat_llm.provider_preset = "openai"
    settings.chat_llm.model = "gpt-5-mini"
    settings.chat_llm.base_url = "https://api.openai.com/v1"
    settings.chat_llm.api_key = "sk-existing"
    settings.chat_llm.api_key_env = "OPENAI_API_KEY"
    settings.chat_llm.context_window = 131_072
    settings.chat_llm.max_tokens = 512
    settings.chat_llm.workers_use_local = True
    controller._settings = settings
    controller._chat_provider = "openai_compatible"
    controller._chat_client = OllamaClient(settings.ollama)
    controller._worker_client = controller._chat_client
    controller._ollama = controller._chat_client
    controller._effective_chat_model = "gpt-5-mini"
    controller._context_window = 131_072
    controller._context_source = "client"
    controller._models_cache = None
    controller._client_cache = ClientCache(settings.ollama)
    # Stub the runtime objects ``reconfigure_chat_llm`` cascades into.
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

    def test_patch_base_url_mirrors_to_chat_llm(self) -> None:
        """When main_chat points at the patched provider, the legacy
        ``chat_llm.base_url`` is kept in sync (so the rebuilt client
        hits the new endpoint)."""
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ), patch.object(
            controller._client_cache, "invalidate",
        ) as invalidate:
            controller.update_provider(
                "openai", {"base_url": "https://example.com/v1"},
            )
        # Legacy block mirrored.
        self.assertEqual(
            controller._settings.chat_llm.base_url,
            "https://example.com/v1",
        )
        # Cache slot invalidated.
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
        # Legacy block mirrored.
        self.assertEqual(
            controller._settings.chat_llm.api_key, "sk-rotated",
        )
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
    def test_main_chat_cascades_through_reconfigure(self) -> None:
        """A main_chat update routes through ``reconfigure_chat_llm``
        so all the legacy cascades (TurnRunner, ProactiveDirector,
        SummaryWorker) still fire."""
        controller = _make_controller()
        with patch.object(
            controller, "reconfigure_chat_llm", return_value={"ok": True},
        ) as reconfig:
            controller.update_route(
                LLM_ROLE_MAIN_CHAT,
                {
                    "provider_id": "local_ollama",
                    "model": "llama3.1:70b",
                    "context_window": 8192,
                },
            )
        reconfig.assert_called_once()
        payload = reconfig.call_args.args[0]
        self.assertEqual(payload["provider"], "ollama")
        self.assertEqual(payload["model"], "llama3.1:70b")
        self.assertEqual(payload["context_window"], 8192)
        # The catalogue row was mutated in place.
        self.assertEqual(
            controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT].provider_id,
            "local_ollama",
        )

    def test_worker_default_persists_without_rebuild(self) -> None:
        """For non-main_chat roles, the route is recorded but the
        chat client is NOT rebuilt (workers pick it up on restart)."""
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ) as persist, patch.object(
            controller, "reconfigure_chat_llm",
        ) as reconfig:
            controller.update_route(
                LLM_ROLE_WORKER_DEFAULT,
                {"provider_id": "openai", "model": "gpt-5-nano"},
            )
        # No reconfigure happened.
        reconfig.assert_not_called()
        # But the catalogue was persisted and mutated.
        persist.assert_called_once()
        worker_route = controller._settings.llm.routes[
            LLM_ROLE_WORKER_DEFAULT
        ]
        self.assertEqual(worker_route.provider_id, "openai")
        self.assertEqual(worker_route.model, "gpt-5-nano")

    def test_route_unknown_provider_raises_key_error(self) -> None:
        controller = _make_controller()
        with self.assertRaises(KeyError):
            controller.update_route(
                LLM_ROLE_MAIN_CHAT,
                {"provider_id": "ghost", "model": "x"},
            )


class ClientCacheStatsTests(unittest.TestCase):
    def test_stats_delegates_to_cache(self) -> None:
        controller = _make_controller()
        # Touch the cache so it has at least one entry.
        controller._client_cache.get(controller._settings.llm.providers[0])
        stats = controller.client_cache_stats()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["providers"], 1)


class ReconfigureMirrorTests(unittest.TestCase):
    """End-to-end: legacy ``reconfigure_chat_llm`` -> ``llm.routes``
    mirror. The reverse direction (``update_route`` -> ``chat_llm``)
    is covered in :class:`UpdateRouteTests`."""

    def test_legacy_reconfigure_updates_catalogue(self) -> None:
        controller = _make_controller()
        with patch(
            "app.core.session.llm_settings_mixin.persist_user_overrides",
        ), patch(
            "app.core.session.session_controller.OllamaClient.get_context_length",
            return_value=None,
        ):
            controller.reconfigure_chat_llm({
                "provider": "openai_compatible",
                "model": "gpt-5-nano",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-existing",
                "workers_use_local": True,
                "provider_preset": "openai",
            })
        # The legacy block changed (existing behaviour).
        self.assertEqual(
            controller._settings.chat_llm.model, "gpt-5-nano",
        )
        # The catalogue's main_chat route reflects the new model.
        main_route = controller._settings.llm.routes[LLM_ROLE_MAIN_CHAT]
        self.assertEqual(main_route.model, "gpt-5-nano")


if __name__ == "__main__":
    unittest.main()

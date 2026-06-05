"""Tests for the legacy ``chat_llm`` + ``ollama`` -> ``llm.providers`` migration.

The migration runs in :func:`app.core.infra.settings.load_settings` when
``llm.providers`` is empty at boot. We verify:

1. **First-run synthesis** — local Ollama and (when set) the remote
   chat provider both land in the catalogue, with routes wired to
   them.
2. **Idempotency** — calling the migration helper a second time on
   the already-populated state is a no-op (preserves user edits).
3. **Local-only setup** — when ``chat_llm.provider == "ollama"`` and
   the base_url matches the local Ollama, only one provider entry is
   synthesised.

The actual ``load_settings`` path is exercised indirectly; here we
call :func:`_migrate_legacy_llm` directly so the test doesn't depend
on disk state.
"""

from __future__ import annotations

import unittest

from app.core.infra.settings import (
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    ChatLlmSettings,
    OllamaSettings,
    _migrate_legacy_llm,
)


def _ollama(
    base_url: str = "http://127.0.0.1:11434",
    chat_model: str = "llama3.1:8b",
) -> OllamaSettings:
    return OllamaSettings(
        base_url=base_url,
        chat_model=chat_model,
        temperature=0.6,
    )


class FirstRunSynthesisTests(unittest.TestCase):
    def test_pure_local_ollama_synthesises_one_provider(self) -> None:
        """Default state: local Ollama, no chat_llm overrides. Only
        ``local_ollama`` should appear; both routes point at it."""
        settings = _migrate_legacy_llm(
            chat_llm=ChatLlmSettings(),
            ollama=_ollama(),
            timeout=300,
        )
        self.assertEqual(
            [p.id for p in settings.providers], ["local_ollama"],
        )
        local = settings.providers[0]
        self.assertEqual(local.kind, "ollama")
        self.assertEqual(local.base_url, "http://127.0.0.1:11434")
        # Both roles point at the local provider.
        main_route = settings.routes[LLM_ROLE_MAIN_CHAT]
        worker_route = settings.routes[LLM_ROLE_WORKER_DEFAULT]
        self.assertEqual(main_route.provider_id, "local_ollama")
        self.assertEqual(worker_route.provider_id, "local_ollama")

    def test_openai_chat_llm_synthesises_two_providers(self) -> None:
        """When chat_llm is on a remote provider, the migration adds a
        second catalogue entry and routes ``main_chat`` to it; the
        worker route stays on local Ollama."""
        chat_llm = ChatLlmSettings(
            provider="openai_compatible",
            provider_preset="openai",
            model="gpt-5-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-real",
            context_window=131_072,
            max_tokens=512,
        )
        settings = _migrate_legacy_llm(
            chat_llm=chat_llm,
            ollama=_ollama(),
            timeout=300,
        )
        ids = [p.id for p in settings.providers]
        self.assertIn("local_ollama", ids)
        self.assertIn("openai", ids)
        openai = next(p for p in settings.providers if p.id == "openai")
        self.assertEqual(openai.kind, "openai_compatible")
        self.assertEqual(openai.base_url, "https://api.openai.com/v1")
        # Credentials carried across.
        self.assertEqual(openai.api_key, "sk-real")
        # main_chat -> openai, worker_default -> local.
        self.assertEqual(
            settings.routes[LLM_ROLE_MAIN_CHAT].provider_id, "openai",
        )
        self.assertEqual(
            settings.routes[LLM_ROLE_MAIN_CHAT].model, "gpt-5-mini",
        )
        self.assertEqual(
            settings.routes[LLM_ROLE_MAIN_CHAT].context_window, 131_072,
        )
        self.assertEqual(
            settings.routes[LLM_ROLE_WORKER_DEFAULT].provider_id,
            "local_ollama",
        )
        # Worker route picks up the legacy ollama.chat_model.
        self.assertEqual(
            settings.routes[LLM_ROLE_WORKER_DEFAULT].model, "llama3.1:8b",
        )

    def test_ollama_cloud_synthesises_separate_provider(self) -> None:
        """When chat_llm.provider == "ollama" but base_url is the cloud
        host, the migration still creates a separate provider entry
        because the URL doesn't match the local Ollama."""
        chat_llm = ChatLlmSettings(
            provider="ollama",
            provider_preset="ollama_cloud",
            model="llama3.1:70b",
            base_url="https://ollama.com",
            api_key="key-cloud",
        )
        settings = _migrate_legacy_llm(
            chat_llm=chat_llm,
            ollama=_ollama(),
            timeout=300,
        )
        ids = {p.id for p in settings.providers}
        self.assertIn("local_ollama", ids)
        self.assertIn("ollama_cloud", ids)
        cloud = next(p for p in settings.providers if p.id == "ollama_cloud")
        self.assertEqual(cloud.kind, "ollama")
        self.assertEqual(cloud.api_key, "key-cloud")
        self.assertEqual(
            settings.routes[LLM_ROLE_MAIN_CHAT].provider_id, "ollama_cloud",
        )

    def test_trailing_slash_url_matches_local(self) -> None:
        """URL match must be slash-insensitive — otherwise a user-typed
        ``http://127.0.0.1:11434/`` would be treated as a new provider."""
        chat_llm = ChatLlmSettings(
            provider="ollama",
            base_url="http://127.0.0.1:11434/",
        )
        settings = _migrate_legacy_llm(
            chat_llm=chat_llm,
            ollama=_ollama(),
            timeout=300,
        )
        self.assertEqual(
            [p.id for p in settings.providers], ["local_ollama"],
        )


class IdempotencyTests(unittest.TestCase):
    def test_migration_output_is_deterministic(self) -> None:
        """Running the migration twice produces identical catalogues."""
        chat_llm = ChatLlmSettings(
            provider="openai_compatible",
            provider_preset="openai",
            model="gpt-4.1-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )
        first = _migrate_legacy_llm(
            chat_llm=chat_llm,
            ollama=_ollama(),
            timeout=300,
        )
        second = _migrate_legacy_llm(
            chat_llm=chat_llm,
            ollama=_ollama(),
            timeout=300,
        )
        self.assertEqual(
            [p.id for p in first.providers],
            [p.id for p in second.providers],
        )
        self.assertEqual(
            set(first.routes.keys()), set(second.routes.keys()),
        )
        for role in first.routes:
            self.assertEqual(
                first.routes[role].provider_id,
                second.routes[role].provider_id,
            )
            self.assertEqual(
                first.routes[role].model,
                second.routes[role].model,
            )


class ProviderIdCollisionTests(unittest.TestCase):
    def test_chat_provider_avoids_local_ollama_collision(self) -> None:
        """Defensive: if the user picked the preset id ``"local_ollama"``
        as their chat preset (e.g. via hand-edited JSON), the migration
        must NOT clobber the legacy ollama row — it falls back to a
        synthetic ``"chat_migrated"`` id."""
        chat_llm = ChatLlmSettings(
            provider="ollama",
            provider_preset="local_ollama",
            model="llama3.1:70b",
            base_url="https://different.example.com",
            api_key="weird-key",
        )
        settings = _migrate_legacy_llm(
            chat_llm=chat_llm,
            ollama=_ollama(),
            timeout=300,
        )
        ids = {p.id for p in settings.providers}
        self.assertIn("local_ollama", ids)
        # The chat provider was renamed to avoid the collision.
        self.assertIn("chat_migrated", ids)
        self.assertEqual(
            settings.routes[LLM_ROLE_MAIN_CHAT].provider_id, "chat_migrated",
        )


if __name__ == "__main__":
    unittest.main()

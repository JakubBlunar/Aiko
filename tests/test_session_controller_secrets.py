"""Controller-level tests for keychain migration + hydration.

Exercises ``SessionController._migrate_and_hydrate_secrets`` against a
fake in-memory keyring, asserting:

* plaintext keys in config are pushed into the keychain and blanked on
  disk, while the in-memory dataclasses keep holding the key (so the
  live read / cache paths are untouched);
* a blank on-disk key is hydrated from the keychain into memory;
* the legacy ``chat_llm`` key is bound to its ``main_chat`` provider's
  keychain account (no second, drift-prone copy).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.infra import secret_store
from app.core.infra.settings import (
    ChatLlmSettings,
    LlmProvider,
    LlmRoute,
    LlmSettings,
)
from app.core.session.session_controller import SessionController


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self  # any non-"fail" class name -> available

    def get_password(self, service: str, account: str):
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


def _settings(*, provider_key: str = "", chat_key: str = "") -> SimpleNamespace:
    provider = LlmProvider(
        id="openai",
        name="OpenAI",
        kind="openai_compatible",
        base_url="https://api.openai.com/v1",
        api_key=provider_key,
        api_key_env="",
    )
    routes = {
        "main_chat": LlmRoute(
            provider_id="openai", model="gpt-5-mini", context_window=50000,
        ),
    }
    llm = LlmSettings(providers=[provider], routes=routes)
    chat_llm = ChatLlmSettings(
        provider="openai_compatible",
        model="gpt-5-mini",
        base_url="https://api.openai.com/v1",
        api_key=chat_key,
    )
    return SimpleNamespace(llm=llm, chat_llm=chat_llm)


class SecretMigrationTests(unittest.TestCase):
    def _run(self, settings, fake, persisted: list[dict]) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = settings

        def _capture(patch_dict):
            persisted.append(patch_dict)

        with patch(
            "app.core.session.session_controller.persist_user_overrides",
            side_effect=_capture,
        ), patch.object(secret_store, "running_under_test", return_value=False), \
                patch.object(secret_store, "_keyring", return_value=fake):
            controller._migrate_and_hydrate_secrets()

    def test_plaintext_key_moves_to_keychain_and_blanks_disk(self) -> None:
        settings = _settings(provider_key="sk-shared", chat_key="sk-shared")
        fake = _FakeKeyring()
        persisted: list[dict] = []
        self._run(settings, fake, persisted)

        # Stored under the provider account (chat_llm shares it).
        self.assertEqual(
            fake.store[(secret_store.SERVICE_NAME, "provider:openai")], "sk-shared",
        )
        # In-memory keys are retained for the live session.
        self.assertEqual(settings.llm.providers[0].api_key, "sk-shared")
        self.assertEqual(settings.chat_llm.api_key, "sk-shared")
        # Disk was rewritten with both keys blanked.
        llm_patch = next(p for p in persisted if "llm" in p)
        self.assertEqual(llm_patch["llm"]["providers"][0]["api_key"], "")
        chat_patch = next(p for p in persisted if "chat_llm" in p)
        self.assertEqual(chat_patch["chat_llm"]["api_key"], "")

    def test_blank_key_is_hydrated_from_keychain(self) -> None:
        settings = _settings(provider_key="", chat_key="")
        fake = _FakeKeyring()
        fake.store[(secret_store.SERVICE_NAME, "provider:openai")] = "sk-stored"
        persisted: list[dict] = []
        self._run(settings, fake, persisted)

        # Pulled into memory from the keychain.
        self.assertEqual(settings.llm.providers[0].api_key, "sk-stored")
        self.assertEqual(settings.chat_llm.api_key, "sk-stored")
        # Nothing migrated -> no disk rewrite.
        self.assertEqual(persisted, [])

    def test_no_backend_keeps_plaintext_on_disk(self) -> None:
        settings = _settings(provider_key="sk-prov", chat_key="sk-prov")
        persisted: list[dict] = []
        controller = SessionController.__new__(SessionController)
        controller._settings = settings
        with patch(
            "app.core.session.session_controller.persist_user_overrides",
            side_effect=lambda p: persisted.append(p),
        ), patch.object(secret_store, "running_under_test", return_value=False), \
                patch.object(secret_store, "_keyring", return_value=None):
            controller._migrate_and_hydrate_secrets()

        # No backend -> set_secret failed -> nothing migrated, no rewrite,
        # in-memory key untouched.
        self.assertEqual(persisted, [])
        self.assertEqual(settings.llm.providers[0].api_key, "sk-prov")

    def test_init_is_inert_under_pytest(self) -> None:
        # _init_secret_storage early-returns under pytest; verify it does
        # not touch the (real) keychain or persist anything.
        settings = _settings(provider_key="sk-prov", chat_key="sk-prov")
        controller = SessionController.__new__(SessionController)
        controller._settings = settings
        with patch(
            "app.core.session.session_controller.persist_user_overrides",
        ) as persist, patch.object(secret_store, "set_secret") as set_secret:
            controller._init_secret_storage()
        persist.assert_not_called()
        set_secret.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

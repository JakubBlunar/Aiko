"""Tests for the legacy ``chat_llm`` + ``ollama`` -> ``llm`` migration.

Both legacy blocks were retired: the ``llm`` block (providers, routes,
embedding) is now the only LLM config surface. Installs that predate
the consolidation still have the old keys in ``user.json``, so
:func:`load_settings` runs :func:`_migrate_legacy_llm` once when
``llm.providers`` is empty, then :func:`_migrate_legacy_llm_config`
persists the result and deletes the old keys.

Covered here:

1. **Synthesis** — the raw legacy payloads produce the right catalogue,
   routes and embedding block.
2. **Persistence + pruning** — the migrated block lands in
   ``user.json`` and the retired keys are gone afterwards, so they
   can't shadow it on the next boot.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.settings import (
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    LLM_ROLE_WORKFLOW,
    _migrate_legacy_llm,
    _migrate_legacy_llm_config,
    prune_user_override_keys,
)


def _ollama_block(**overrides) -> dict:
    """The legacy ``ollama`` block as it appeared in ``user.json``."""
    block = {
        "base_url": "http://127.0.0.1:11434",
        "chat_model": "llama3.1:8b",
        "timeout": 300,
        "embedding_model": "qwen3-embedding:0.6b",
        "embedding_num_ctx": 2048,
        "embedding_num_gpu": 0,
    }
    block.update(overrides)
    return block


class FirstRunSynthesisTests(unittest.TestCase):
    def test_pure_local_ollama_synthesises_one_provider(self) -> None:
        """Default state: local Ollama, no chat_llm overrides. Only
        ``local_ollama`` should appear; every route points at it."""
        llm = _migrate_legacy_llm(chat_llm={}, ollama=_ollama_block())
        self.assertEqual([p.id for p in llm.providers], ["local_ollama"])
        local = llm.providers[0]
        self.assertEqual(local.kind, "ollama")
        self.assertEqual(local.base_url, "http://127.0.0.1:11434")
        for role in (
            LLM_ROLE_MAIN_CHAT, LLM_ROLE_WORKER_DEFAULT, LLM_ROLE_WORKFLOW,
        ):
            self.assertEqual(llm.routes[role].provider_id, "local_ollama")

    def test_embedding_block_folds_in_the_legacy_keys(self) -> None:
        # The ``ollama.embedding_*`` keys had no home after the ollama
        # block was retired; they become ``llm.embedding``.
        llm = _migrate_legacy_llm(chat_llm={}, ollama=_ollama_block())
        self.assertEqual(llm.embedding.provider_id, "local_ollama")
        self.assertEqual(llm.embedding.model, "qwen3-embedding:0.6b")
        self.assertEqual(llm.embedding.num_ctx, 2048)
        self.assertEqual(llm.embedding.num_gpu, 0)

    def test_openai_chat_llm_synthesises_two_providers(self) -> None:
        """When chat_llm is on a remote provider, the migration adds a
        second catalogue entry and routes ``main_chat`` to it; the
        worker routes stay on local Ollama."""
        llm = _migrate_legacy_llm(
            chat_llm={
                "provider": "openai_compatible",
                "provider_preset": "openai",
                "model": "gpt-5-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-real",
                "context_window": 131_072,
                "max_tokens": 512,
            },
            ollama=_ollama_block(),
        )
        ids = [p.id for p in llm.providers]
        self.assertIn("local_ollama", ids)
        self.assertIn("openai", ids)
        openai = next(p for p in llm.providers if p.id == "openai")
        self.assertEqual(openai.kind, "openai_compatible")
        self.assertEqual(openai.base_url, "https://api.openai.com/v1")
        self.assertEqual(openai.api_key, "sk-real")
        main = llm.routes[LLM_ROLE_MAIN_CHAT]
        self.assertEqual(main.provider_id, "openai")
        self.assertEqual(main.model, "gpt-5-mini")
        self.assertEqual(main.context_window, 131_072)
        worker = llm.routes[LLM_ROLE_WORKER_DEFAULT]
        self.assertEqual(worker.provider_id, "local_ollama")
        # Worker route picks up the legacy ollama.chat_model.
        self.assertEqual(worker.model, "llama3.1:8b")

    def test_reasoning_effort_carried_to_remote_provider_and_route(
        self,
    ) -> None:
        """A configured ``chat_llm.reasoning_effort`` lands on both the
        synthesised remote provider and the ``main_chat`` route so the
        chat client is built with it."""
        llm = _migrate_legacy_llm(
            chat_llm={
                "provider": "openai_compatible",
                "provider_preset": "openai",
                "model": "gpt-5.4-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-real",
                "reasoning_effort": "low",
            },
            ollama=_ollama_block(),
        )
        openai = next(p for p in llm.providers if p.id == "openai")
        self.assertEqual(openai.reasoning_effort, "low")
        self.assertEqual(
            llm.routes[LLM_ROLE_MAIN_CHAT].reasoning_effort, "low",
        )

    def test_ollama_cloud_synthesises_separate_provider(self) -> None:
        """``chat_llm.provider == "ollama"`` with the cloud host still
        creates a separate entry — the URL doesn't match the local one."""
        llm = _migrate_legacy_llm(
            chat_llm={
                "provider": "ollama",
                "provider_preset": "ollama_cloud",
                "model": "llama3.1:70b",
                "base_url": "https://ollama.com",
                "api_key": "key-cloud",
            },
            ollama=_ollama_block(),
        )
        ids = {p.id for p in llm.providers}
        self.assertIn("local_ollama", ids)
        self.assertIn("ollama_cloud", ids)
        cloud = next(p for p in llm.providers if p.id == "ollama_cloud")
        self.assertEqual(cloud.kind, "ollama")
        self.assertEqual(cloud.api_key, "key-cloud")
        self.assertEqual(
            llm.routes[LLM_ROLE_MAIN_CHAT].provider_id, "ollama_cloud",
        )

    def test_trailing_slash_url_matches_local(self) -> None:
        """URL match must be slash-insensitive — otherwise a user-typed
        ``http://127.0.0.1:11434/`` would be treated as a new provider."""
        llm = _migrate_legacy_llm(
            chat_llm={
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434/",
            },
            ollama=_ollama_block(),
        )
        self.assertEqual([p.id for p in llm.providers], ["local_ollama"])

    def test_chat_provider_avoids_local_ollama_collision(self) -> None:
        """Defensive: a hand-edited ``provider_preset: "local_ollama"``
        must not clobber the migrated ollama row — the remote entry
        falls back to the synthetic ``chat_migrated`` id."""
        llm = _migrate_legacy_llm(
            chat_llm={
                "provider": "ollama",
                "provider_preset": "local_ollama",
                "model": "llama3.1:70b",
                "base_url": "https://different.example.com",
                "api_key": "weird-key",
            },
            ollama=_ollama_block(),
        )
        ids = {p.id for p in llm.providers}
        self.assertIn("local_ollama", ids)
        self.assertIn("chat_migrated", ids)
        self.assertEqual(
            llm.routes[LLM_ROLE_MAIN_CHAT].provider_id, "chat_migrated",
        )

    def test_migration_output_is_deterministic(self) -> None:
        chat_llm = {
            "provider": "openai_compatible",
            "provider_preset": "openai",
            "model": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        }
        first = _migrate_legacy_llm(chat_llm=chat_llm, ollama=_ollama_block())
        second = _migrate_legacy_llm(chat_llm=chat_llm, ollama=_ollama_block())
        self.assertEqual(
            [p.id for p in first.providers], [p.id for p in second.providers],
        )
        self.assertEqual(set(first.routes), set(second.routes))
        for role in first.routes:
            self.assertEqual(
                first.routes[role].provider_id,
                second.routes[role].provider_id,
            )
            self.assertEqual(first.routes[role].model, second.routes[role].model)


class PersistAndPruneTests(unittest.TestCase):
    """The migration is one-shot: it has to leave ``user.json`` in a
    state where it never runs again."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"

    def _write(self, payload: dict) -> None:
        self.user_json.write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )

    def _read(self) -> dict:
        return json.loads(self.user_json.read_text(encoding="utf-8"))

    def test_persists_llm_block_and_drops_retired_keys(self) -> None:
        self._write({
            "chat_llm": {"provider": "ollama", "model": "llama3.1:8b"},
            "ollama": _ollama_block(),
            "voice": {"tts_enabled": True},
        })
        llm = _migrate_legacy_llm(chat_llm={}, ollama=_ollama_block())
        _migrate_legacy_llm_config(llm, path=self.user_json)
        written = self._read()
        self.assertNotIn("chat_llm", written)
        self.assertNotIn("ollama", written)
        self.assertIn("llm", written)
        self.assertEqual(
            [p["id"] for p in written["llm"]["providers"]], ["local_ollama"],
        )
        self.assertEqual(
            written["llm"]["embedding"]["model"], "qwen3-embedding:0.6b",
        )
        # Unrelated overrides survive untouched.
        self.assertEqual(written["voice"], {"tts_enabled": True})

    def test_noop_when_no_legacy_keys_present(self) -> None:
        # A fresh install running on the shipped defaults has nothing to
        # migrate; writing an ``llm`` block there would freeze today's
        # defaults into the user's overrides.
        self._write({"voice": {"tts_enabled": True}})
        llm = _migrate_legacy_llm(chat_llm={}, ollama=_ollama_block())
        _migrate_legacy_llm_config(llm, path=self.user_json)
        self.assertEqual(self._read(), {"voice": {"tts_enabled": True}})

    def test_prune_reports_only_keys_it_removed(self) -> None:
        self._write({"ollama": {"base_url": "x"}, "voice": {}})
        removed = prune_user_override_keys(
            "chat_llm", "ollama", path=self.user_json,
        )
        self.assertEqual(removed, ["ollama"])
        self.assertEqual(self._read(), {"voice": {}})

    def test_prune_missing_file_is_harmless(self) -> None:
        self.assertEqual(
            prune_user_override_keys("ollama", path=self.user_json), [],
        )


if __name__ == "__main__":
    unittest.main()

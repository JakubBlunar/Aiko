"""Tests for ``app.llm.factory.ClientCache``.

The cache key is ``(kind, base_url, resolved_api_key)`` so two
provider rows that match on all three share one underlying
:class:`ChatClient`. The tests exercise:

1. Identical keys -> shared client instance.
2. Different keys -> distinct client instances.
3. Credential invalidation drops the cached entry.
4. ``shutdown`` clears the cache (subsequent ``get`` rebuilds).
5. Two routes pointing at the same provider share one client.
"""

from __future__ import annotations

import os
import unittest

from app.core.infra.settings import LlmProvider, OllamaSettings
from app.llm.factory import ClientCache


def _ollama_settings() -> OllamaSettings:
    return OllamaSettings(
        base_url="http://127.0.0.1:11434",
        chat_model="llama3.1:8b",
        temperature=0.6,
    )


def _make_provider(
    *,
    pid: str = "p",
    kind: str = "ollama",
    base_url: str = "http://127.0.0.1:11434",
    api_key: str = "",
) -> LlmProvider:
    return LlmProvider(
        id=pid,
        name=f"Provider {pid}",
        kind=kind,
        base_url=base_url,
        api_key=api_key,
    )


class IdentityTests(unittest.TestCase):
    def test_same_provider_returns_same_client(self) -> None:
        cache = ClientCache(_ollama_settings())
        provider = _make_provider()
        c1 = cache.get(provider)
        c2 = cache.get(provider)
        self.assertIs(c1, c2)

    def test_different_provider_ids_share_when_keys_match(self) -> None:
        """The whole point of the cache: two different LlmProvider
        rows that resolve to the same (kind, base_url, key) tuple
        share one underlying client."""
        cache = ClientCache(_ollama_settings())
        p1 = _make_provider(pid="ollama_a")
        p2 = _make_provider(pid="ollama_b")  # same base_url, same kind
        self.assertIs(cache.get(p1), cache.get(p2))

    def test_different_base_urls_get_different_clients(self) -> None:
        cache = ClientCache(_ollama_settings())
        p1 = _make_provider(pid="p1", base_url="http://127.0.0.1:11434")
        p2 = _make_provider(pid="p2", base_url="http://10.0.0.5:11434")
        self.assertIsNot(cache.get(p1), cache.get(p2))

    def test_different_kinds_get_different_clients(self) -> None:
        cache = ClientCache(_ollama_settings())
        p1 = _make_provider(pid="ollama", kind="ollama")
        p2 = _make_provider(
            pid="openai",
            kind="openai_compatible",
            base_url="http://127.0.0.1:11434",
        )
        # Different kinds -> different cache slots even though the
        # base_url happens to match.
        self.assertIsNot(
            cache.get(p1, model="x"),
            cache.get(p2, model="x"),
        )

    def test_trailing_slash_normalised(self) -> None:
        """``http://x:1234`` and ``http://x:1234/`` are the same key."""
        cache = ClientCache(_ollama_settings())
        p1 = _make_provider(pid="a", base_url="http://x:1234")
        p2 = _make_provider(pid="b", base_url="http://x:1234/")
        self.assertIs(cache.get(p1), cache.get(p2))


class InvalidationTests(unittest.TestCase):
    def test_invalidate_drops_entry(self) -> None:
        cache = ClientCache(_ollama_settings())
        provider = _make_provider(pid="x")
        first = cache.get(provider)
        cache.invalidate("x")
        second = cache.get(provider)
        self.assertIsNot(first, second)

    def test_invalidate_only_drops_referenced_id(self) -> None:
        """When two providers share a slot, ``invalidate`` only
        removes the slot when no provider id references it any more."""
        cache = ClientCache(_ollama_settings())
        p1 = _make_provider(pid="a")
        p2 = _make_provider(pid="b")  # same key
        first = cache.get(p1)
        cache.get(p2)  # both ids now hold the slot
        cache.invalidate("a")
        # Slot still alive because ``b`` references it.
        self.assertIs(cache.get(p2), first)

    def test_invalidate_unknown_id_is_noop(self) -> None:
        cache = ClientCache(_ollama_settings())
        provider = _make_provider(pid="x")
        client = cache.get(provider)
        cache.invalidate("does-not-exist")
        self.assertIs(cache.get(provider), client)


class ShutdownTests(unittest.TestCase):
    def test_shutdown_clears_cache(self) -> None:
        cache = ClientCache(_ollama_settings())
        provider = _make_provider()
        first = cache.get(provider)
        cache.shutdown()
        # After shutdown, the next ``get`` builds a fresh client.
        second = cache.get(provider)
        self.assertIsNot(first, second)

    def test_stats_snapshot(self) -> None:
        cache = ClientCache(_ollama_settings())
        cache.get(_make_provider(pid="a"))
        cache.get(_make_provider(pid="b", base_url="http://other:11434"))
        stats = cache.stats()
        self.assertEqual(stats["entries"], 2)
        self.assertEqual(stats["providers"], 2)
        # ``keys`` is a list of {kind, base_url, has_api_key, provider_ids}.
        self.assertEqual(len(stats["keys"]), 2)


class EnvVarResolutionTests(unittest.TestCase):
    """``_resolve_api_key`` falls back to an env var when api_key is
    empty. Two providers with the same env-resolved key share a slot."""

    def setUp(self) -> None:
        self._old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "env-resolved"

    def tearDown(self) -> None:
        if self._old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._old_key

    def test_env_var_resolved_keys_share_slot(self) -> None:
        cache = ClientCache(_ollama_settings())
        p1 = LlmProvider(
            id="a",
            name="A",
            kind="openai_compatible",
            base_url="https://api.openai.com/v1",
        )
        p2 = LlmProvider(
            id="b",
            name="B",
            kind="openai_compatible",
            base_url="https://api.openai.com/v1",
        )
        # Both resolve their key from OPENAI_API_KEY -> same slot.
        self.assertIs(
            cache.get(p1, model="gpt-4o-mini"),
            cache.get(p2, model="gpt-4o-mini"),
        )

    def test_explicit_key_overrides_env(self) -> None:
        cache = ClientCache(_ollama_settings())
        p1 = LlmProvider(
            id="a",
            name="A",
            kind="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="explicit-1",
        )
        p2 = LlmProvider(
            id="b",
            name="B",
            kind="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="explicit-2",
        )
        # Different explicit keys -> different slots.
        self.assertIsNot(
            cache.get(p1, model="gpt-4o-mini"),
            cache.get(p2, model="gpt-4o-mini"),
        )


if __name__ == "__main__":
    unittest.main()

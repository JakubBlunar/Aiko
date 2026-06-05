"""LLM client factory + connection cache.

Resolves :class:`app.core.infra.settings.LlmProvider` entries into
concrete :class:`ChatClient` instances and caches them by
``(kind, base_url, resolved_api_key)`` so two routes pointing at the
same provider share one underlying HTTP client.

Why a cache? Real-world example: ``main_chat -> openai`` (gpt-5-mini)
and ``worker_default -> openai`` (gpt-4.1-nano) both target
``https://api.openai.com/v1`` with the same key — they should share
a single :class:`OpenAICompatibleClient` so connection-pool / DNS /
TLS cost is paid once. Different models on the same provider don't
need different clients (the model is per-request, not per-client).

The cache also lets us cheaply invalidate when credentials change:
``invalidate(provider_id)`` drops the matching entry, the next
``get`` rebuilds, and downstream code (TurnRunner / workers) doesn't
need to know.

This module is intentionally thin — all the provider-specific quirks
(Gemini system-message collapse, OpenRouter extra headers, …) live
in the concrete client implementations under
:mod:`app.llm.openai_compatible_client` and :mod:`app.llm.ollama_client`.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from app.core.infra.settings import (
    LlmProvider,
    LlmRoute,
    LlmSettings,
    OllamaSettings,
)
from app.llm.chat_client import ChatClient
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import OpenAICompatibleClient


log = logging.getLogger("app.llm.factory")


# Env-var fallbacks mirrored from ``app/core/session/session_controller.py``
# so the factory remains usable without circular-importing the controller.
# Order matters: longest / most-specific needles first so ``api.openai.com``
# doesn't shadow ``openrouter.ai``.
_PROVIDER_ENV_HINTS: tuple[tuple[str, str], ...] = (
    ("openrouter.ai", "OPENROUTER_API_KEY"),
    ("api.openai.com", "OPENAI_API_KEY"),
    ("generativelanguage.googleapis.com", "GEMINI_API_KEY"),
    ("api.groq.com", "GROQ_API_KEY"),
    ("ollama.com", "OLLAMA_API_KEY"),
    ("api.anthropic.com", "ANTHROPIC_API_KEY"),
    ("api.x.ai", "XAI_API_KEY"),
    ("api.together.xyz", "TOGETHER_API_KEY"),
    ("api.deepseek.com", "DEEPSEEK_API_KEY"),
    ("api.mistral.ai", "MISTRAL_API_KEY"),
)


def _resolve_env_var(*, base_url: str, explicit: str) -> str:
    """Pick the env-var name that holds this provider's API key."""
    if explicit:
        return explicit
    host = (base_url or "").lower()
    for needle, name in _PROVIDER_ENV_HINTS:
        if needle in host:
            return name
    return ""


def _resolve_api_key(provider: LlmProvider) -> str:
    """Return the API key for ``provider`` — explicit first, env fallback."""
    explicit = (provider.api_key or "").strip()
    if explicit:
        return explicit
    env_name = _resolve_env_var(
        base_url=provider.base_url,
        explicit=(provider.api_key_env or "").strip(),
    )
    if not env_name:
        return ""
    return (os.environ.get(env_name, "") or "").strip()


@dataclass(frozen=True)
class _CacheKey:
    """Cache key for :class:`ClientCache`.

    Two providers that match on ``(kind, base_url, resolved_key)``
    are considered indistinguishable from the factory's point of
    view — even if they have different ``LlmProvider.id`` rows, they
    share one underlying client. This is intentional: it lets a
    user define both "OpenAI cheap" and "OpenAI premium" provider
    rows with the same key and still pay only one connection cost.
    """

    kind: str
    base_url: str
    api_key: str  # resolved (explicit or env-var lookup, normalised)


class ClientCache:
    """Process-wide cache of :class:`ChatClient` instances.

    Thread-safe (a single ``threading.RLock`` protects the map);
    contention is negligible because each role builds its client
    once at startup / route change and stashes the reference.
    """

    def __init__(self, ollama_settings: OllamaSettings) -> None:
        self._ollama_settings = ollama_settings
        self._lock = threading.RLock()
        # cache_key -> (provider_ids, client). ``provider_ids`` is the
        # set of ``LlmProvider.id`` rows that hashed to this key — we
        # need it so :meth:`invalidate` can drop the entry by id.
        self._entries: dict[_CacheKey, tuple[set[str], ChatClient]] = {}

    @staticmethod
    def _key_for(provider: LlmProvider) -> _CacheKey:
        return _CacheKey(
            kind=(provider.kind or "").strip().lower(),
            base_url=(provider.base_url or "").strip().rstrip("/").lower(),
            api_key=_resolve_api_key(provider),
        )

    def get(self, provider: LlmProvider, *, model: str = "") -> ChatClient:
        """Return a shared :class:`ChatClient` for ``provider``.

        ``model`` is only consulted on the openai-compat path — the
        client constructor requires a non-empty model for OpenRouter /
        Gemini quirk-handling, but the *cache* doesn't key on model
        (one client serves all models on a given provider). When the
        caller doesn't have a model yet, pass empty string and the
        factory falls back to a local Ollama client (parity with
        :func:`session_controller._build_chat_client`).
        """
        key = self._key_for(provider)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                provider_ids, client = entry
                provider_ids.add(provider.id)
                return client
            client = self._build(provider, model=model)
            self._entries[key] = ({provider.id}, client)
            log.info(
                "client cache: built kind=%s base_url=%s providers=%s",
                key.kind,
                key.base_url,
                provider.id,
            )
            return client

    def _build(self, provider: LlmProvider, *, model: str) -> ChatClient:
        """Construct a concrete client from a :class:`LlmProvider`."""
        base_url = (provider.base_url or "").strip() or self._ollama_settings.base_url
        api_key = _resolve_api_key(provider)
        extra_headers = {
            str(k).strip(): str(v).strip()
            for k, v in dict(provider.extra_headers or {}).items()
            if str(k).strip() and v is not None
        }
        kind = (provider.kind or "").strip().lower()
        if kind == "openai_compatible":
            if not (model or "").strip():
                # No model picked yet — fall back to a local Ollama
                # client so the controller boots healthy. The user
                # gets a meaningful error from the drawer the moment
                # they actually try to chat, not at startup.
                log.warning(
                    "factory: kind=openai_compatible but model is empty for "
                    "provider=%s; falling back to local Ollama client.",
                    provider.id,
                )
                return OllamaClient(
                    self._ollama_settings,
                    base_url=base_url,
                    api_key=api_key or None,
                    extra_headers=extra_headers or None,
                    keep_alive=provider.keep_alive,
                )
            return OpenAICompatibleClient(
                self._ollama_settings,
                base_url=base_url,
                api_key=api_key or None,
                model=model.strip(),
                extra_headers=extra_headers or None,
                keep_alive=provider.keep_alive,
            )
        return OllamaClient(
            self._ollama_settings,
            base_url=base_url,
            api_key=api_key or None,
            extra_headers=extra_headers or None,
            keep_alive=provider.keep_alive,
        )

    def invalidate(self, provider_id: str) -> None:
        """Drop the cached client for ``provider_id``.

        Call this after editing a provider's credentials / base_url
        / extra_headers — the next :meth:`get` will rebuild from the
        updated row. No-op when the id isn't currently cached.
        """
        with self._lock:
            doomed: list[_CacheKey] = []
            for key, (ids, _client) in self._entries.items():
                ids.discard(provider_id)
                if not ids:
                    doomed.append(key)
            for key in doomed:
                self._entries.pop(key, None)
            if doomed:
                log.info(
                    "client cache: invalidated provider=%s (dropped %d entries)",
                    provider_id,
                    len(doomed),
                )

    def shutdown(self) -> None:
        """Release every cached client.

        Some clients (``OllamaClient`` in particular) hold a
        ``requests.Session`` — best practice is to close it when the
        app is shutting down to release the underlying TCP pool. We
        do this best-effort: clients that don't implement ``close()``
        are silently dropped.
        """
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for _ids, client in entries:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - best-effort
                    pass

    def stats(self) -> dict[str, Any]:
        """Diagnostic snapshot (for MCP / Diagnostics tab)."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "providers": sum(
                    len(ids) for ids, _ in self._entries.values()
                ),
                "keys": [
                    {
                        "kind": k.kind,
                        "base_url": k.base_url,
                        "has_api_key": bool(k.api_key),
                        "provider_ids": sorted(ids),
                    }
                    for k, (ids, _client) in self._entries.items()
                ],
            }


def build_client_for_route(
    cache: ClientCache,
    *,
    route: LlmRoute,
    settings: LlmSettings,
) -> ChatClient:
    """Resolve a route -> provider lookup and hit the cache.

    Convenience entry point: ``SessionController._build_chat_client``
    / ``_build_worker_client`` pass their cache + the active
    ``LlmSettings`` snapshot here rather than re-implementing the
    provider lookup. Raises ``KeyError`` when the route points at a
    provider that isn't in the catalogue — that's a hard error
    surfaced to the caller (the UI guards against it on save, but a
    hand-edited ``user.json`` could trip this).
    """
    provider = _find_provider(settings, route.provider_id)
    return cache.get(provider, model=route.model)


def _find_provider(settings: LlmSettings, provider_id: str) -> LlmProvider:
    for entry in settings.providers:
        if entry.id == provider_id:
            return entry
    raise KeyError(
        f"LLM route references unknown provider_id={provider_id!r}; "
        f"known providers: {[p.id for p in settings.providers]!r}"
    )

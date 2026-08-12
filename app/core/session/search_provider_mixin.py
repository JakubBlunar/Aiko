"""Web-search provider plumbing for :class:`SessionController`.

Owns two :class:`app.llm.search.providers.SearchProvider` instances. The
shared one backs the background lanes — the worker-facing
``WebSearchTool`` (F1 / G3 / F9) and the ``WebSearchHandler`` (goal
workflow). The second backs the D3 brain lane (the synchronous
``web_search`` tool) and differs deliberately: no DuckDuckGo fallback
and a shorter timeout, because a user is waiting on it. Both are built
lazily from ``settings.search`` and cached; consumers register
themselves — on the matching list — so a live ``reconfigure_search`` can
re-point them without a restart.

State ownership note: like the other session mixins, this class only
groups methods. It carries no ``__init__``; the few attributes it uses
(the two provider caches, the two consumer lists, and the turn-scoped
``_turn_search_results`` stash) are created lazily via ``getattr`` so
``SessionController.__init__`` does not need a
dedicated assignment. The API key is read from / written to the OS
keychain (best-effort, inert under pytest) via
:mod:`app.core.infra.secret_store`, mirroring the provider-catalogue path.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.infra import secret_store
from app.core.infra.settings import persist_user_overrides
from app.llm.search import build_brain_search_provider, build_search_provider
from app.llm.search.providers import resolve_api_key


log = logging.getLogger("app.session.search")


class SearchProviderMixin:
    """Lazy build + live reconfigure of the shared search provider."""

    # ── provider lifecycle ───────────────────────────────────────────

    def _get_search_provider(self) -> Any:
        """Return the shared provider, building + caching it on first use."""
        prov = getattr(self, "_search_provider", None)
        if prov is None:
            self._hydrate_search_key()
            prov = build_search_provider(getattr(self._settings, "search", None))
            self._search_provider = prov
            log.info(
                "search provider ready: %s",
                getattr(prov, "name", type(prov).__name__),
            )
        return prov

    def _get_brain_search_provider(self) -> Any:
        """Return the D3 brain-lane provider, building + caching on first use.

        Separate instance from the worker provider: no DuckDuckGo
        fallback and a shorter timeout, because a user is waiting on this
        one. See :func:`build_brain_search_provider`.
        """
        prov = getattr(self, "_brain_search_provider", None)
        if prov is None:
            self._hydrate_search_key()
            prov = build_brain_search_provider(
                getattr(self._settings, "search", None)
            )
            self._brain_search_provider = prov
            log.info(
                "brain search provider ready: %s",
                getattr(prov, "name", type(prov).__name__),
            )
        return prov

    def _register_search_consumer(self, consumer: Any) -> Any:
        """Track a ``WebSearchTool`` / ``WebSearchHandler`` for re-pointing.

        Returns ``consumer`` so call sites can register inline.
        """
        consumers = getattr(self, "_search_consumers", None)
        if consumers is None:
            consumers = []
            self._search_consumers = consumers
        consumers.append(consumer)
        return consumer

    def _register_brain_search_consumer(self, consumer: Any) -> Any:
        """Track a brain-lane tool so a reconfigure re-points it too.

        Kept in its own list because these consumers must receive the
        brain provider, not the shared worker one. The tool registry is
        long-lived (rebuilt on settings change, not per turn), so a live
        ``reconfigure_search`` would otherwise leave a stale provider —
        or, if they shared the worker list, hand the brain lane the
        fallback-wrapped provider it deliberately avoids.
        """
        consumers = getattr(self, "_brain_search_consumers", None)
        if consumers is None:
            consumers = []
            self._brain_search_consumers = consumers
        consumers.append(consumer)
        return consumer

    def _rebuild_search_provider(self) -> Any:
        """Rebuild the provider from current settings and re-point consumers."""
        prov = build_search_provider(getattr(self._settings, "search", None))
        self._search_provider = prov
        for consumer in list(getattr(self, "_search_consumers", []) or []):
            try:
                consumer.set_provider(prov)
            except Exception:
                log.debug("search consumer set_provider failed", exc_info=True)
        brain_prov = build_brain_search_provider(
            getattr(self._settings, "search", None)
        )
        self._brain_search_provider = brain_prov
        for consumer in list(getattr(self, "_brain_search_consumers", []) or []):
            try:
                consumer.set_provider(brain_prov)
            except Exception:
                log.debug(
                    "brain search consumer set_provider failed", exc_info=True,
                )
        log.info(
            "search provider rebuilt: %s (brain: %s)",
            getattr(prov, "name", type(prov).__name__),
            getattr(brain_prov, "name", type(brain_prov).__name__),
        )
        return prov

    # ── D3 turn-scoped result capture ────────────────────────────────

    def _stash_turn_search_results(
        self, query: str, results: list[dict[str, str]],
    ) -> None:
        """Hold this turn's brain-lane hits for the post-turn distill.

        Called by :class:`BrainWebSearchTool` from inside the tool pass.
        Only the most recent search survives: if she searched twice in one
        turn, the later query is the one she actually answered from, and
        the distill is a nice-to-have rather than an audit trail.
        """
        if not query or not results:
            return
        self._turn_search_results = (query, [dict(r) for r in results])

    def _drain_turn_search_results(
        self,
    ) -> "tuple[str, list[dict[str, str]]] | None":
        """Take and clear the stashed hits (``None`` when she didn't search)."""
        stashed = getattr(self, "_turn_search_results", None)
        self._turn_search_results = None
        return stashed

    def _hydrate_search_key(self) -> None:
        """Pull the LangSearch key from the keychain into memory if blank.

        Mirrors the provider hydrate path: an explicit on-disk key or
        the ``api_key_env`` environment variable still win (resolved later
        in :func:`build_search_provider`); this only fills the in-memory
        ``api_key`` from the OS keychain when nothing else is set. Inert
        under pytest.
        """
        if secret_store.running_under_test():
            return
        s = getattr(self._settings, "search", None)
        if s is None or (getattr(s, "api_key", "") or "").strip():
            return
        try:
            hydrated = secret_store.get_secret(secret_store.SEARCH_API_KEY_ACCOUNT)
        except Exception:
            hydrated = None
        if hydrated:
            s.api_key = hydrated

    def _build_query_reformulator(self) -> Any:
        """Build the F6 reformulate_fn for the workers, or ``None``.

        Returns ``None`` when ``search.query_reformulation_enabled`` is
        off or no worker LLM client is available, so the workers fall
        back to the deterministic scrub. Uses the maintenance (worker)
        client + worker model so the rewrite stays local and never
        spends chat quota.
        """
        s = getattr(self._settings, "search", None)
        if s is None or not bool(getattr(s, "query_reformulation_enabled", True)):
            return None
        client = getattr(self, "_maintenance_client", None)
        model = getattr(self, "_effective_worker_model", None)
        if client is None or not model:
            return None
        try:
            from app.core.memory.query_reformulation import make_reformulator

            return make_reformulator(
                ollama=client,
                chat_model=model,
                cancel_event=getattr(self, "_fact_check_cancel", None),
                surface="query_reformulation",
            )
        except Exception:
            log.debug("build query reformulator failed", exc_info=True)
            return None

    # ── REST surface ─────────────────────────────────────────────────

    def _search_public_snapshot(self) -> dict[str, Any]:
        """Masked snapshot for ``GET /api/settings`` (no raw API key)."""
        s = getattr(self._settings, "search", None)
        if s is None:
            return {"provider": "duckduckgo", "has_api_key": False}
        resolved = resolve_api_key(
            getattr(s, "api_key", "") or "",
            getattr(s, "api_key_env", "") or "",
        )
        return {
            "provider": getattr(s, "provider", "duckduckgo"),
            "has_api_key": bool(resolved),
            "api_key_env": getattr(s, "api_key_env", ""),
            "langsearch_summary": bool(getattr(s, "langsearch_summary", True)),
            "langsearch_freshness": getattr(s, "langsearch_freshness", "noLimit"),
            "langsearch_count": int(getattr(s, "langsearch_count", 10)),
            "fallback_to_duckduckgo": bool(
                getattr(s, "fallback_to_duckduckgo", True)
            ),
            "timeout_seconds": float(getattr(s, "timeout_seconds", 12.0)),
            "query_reformulation_enabled": bool(
                getattr(s, "query_reformulation_enabled", True)
            ),
            "brain_tool_enabled": bool(getattr(s, "brain_tool_enabled", True)),
            "brain_timeout_seconds": float(
                getattr(s, "brain_timeout_seconds", 6.0)
            ),
        }

    def reconfigure_search(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial ``search`` patch, persist, rebuild, re-point.

        Accepts any subset of the :class:`SearchSettings` fields. The
        ``api_key`` (if present) is routed through the OS keychain
        (``""`` on disk when a backend exists); the other fields persist
        to ``user.json``. Returns the masked snapshot.
        """
        s = self._settings.search

        if "provider" in patch:
            raw = str(patch["provider"] or "").strip().lower()
            if raw in {"duckduckgo", "langsearch"}:
                s.provider = raw
        if "api_key" in patch:
            # Empty string clears the stored key.
            s.api_key = str(patch["api_key"] or "").strip()
        if "api_key_env" in patch:
            s.api_key_env = str(patch["api_key_env"] or "").strip()
        if "langsearch_summary" in patch:
            s.langsearch_summary = bool(patch["langsearch_summary"])
        if "langsearch_freshness" in patch:
            s.langsearch_freshness = (
                str(patch["langsearch_freshness"] or "noLimit").strip()
                or "noLimit"
            )
        if "langsearch_count" in patch:
            try:
                s.langsearch_count = max(1, min(10, int(patch["langsearch_count"])))
            except (TypeError, ValueError):
                pass
        if "fallback_to_duckduckgo" in patch:
            s.fallback_to_duckduckgo = bool(patch["fallback_to_duckduckgo"])
        if "timeout_seconds" in patch:
            try:
                s.timeout_seconds = max(1.0, float(patch["timeout_seconds"]))
            except (TypeError, ValueError):
                pass
        if "query_reformulation_enabled" in patch:
            s.query_reformulation_enabled = bool(
                patch["query_reformulation_enabled"]
            )
        if "brain_tool_enabled" in patch:
            s.brain_tool_enabled = bool(patch["brain_tool_enabled"])
        if "brain_timeout_seconds" in patch:
            try:
                s.brain_timeout_seconds = max(
                    1.0, float(patch["brain_timeout_seconds"])
                )
            except (TypeError, ValueError):
                pass

        try:
            persist_user_overrides({"search": {
                "provider": s.provider,
                "api_key": secret_store.store_or_passthrough(
                    secret_store.SEARCH_API_KEY_ACCOUNT, s.api_key
                ),
                "api_key_env": s.api_key_env,
                "langsearch_summary": s.langsearch_summary,
                "langsearch_freshness": s.langsearch_freshness,
                "langsearch_count": s.langsearch_count,
                "fallback_to_duckduckgo": s.fallback_to_duckduckgo,
                "timeout_seconds": s.timeout_seconds,
                "query_reformulation_enabled": s.query_reformulation_enabled,
                "brain_tool_enabled": s.brain_tool_enabled,
                "brain_timeout_seconds": s.brain_timeout_seconds,
            }})
        except Exception:
            log.warning("persist search overrides failed", exc_info=True)

        self._rebuild_search_provider()
        if "brain_tool_enabled" in patch:
            # The brain tool's *existence* changed, not just its backend:
            # the registry is long-lived, so it has to be rebuilt for the
            # switch to take effect before the next restart.
            try:
                self.rebuild_tool_registry()
            except Exception:
                log.debug("rebuild_tool_registry failed", exc_info=True)
        return self._search_public_snapshot()

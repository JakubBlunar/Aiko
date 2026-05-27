"""Speculative RAG pre-fetch off the hot path (Phase 1b).

While the user is still talking we keep getting `stt_partial` events that
*grow* into the eventual final transcript. Rather than waiting until the
final transcript lands and then doing the embed + multi-source retrieval
on the hot path, this module starts a background fetch as soon as we have
a partial that's long enough to be meaningful, and stashes the result in a
small TTL cache.

When the final transcript arrives, :class:`PromptAssembler` consults the
cache before falling back to a fresh fetch. Because partials are *prefixes*
of the final transcript, prefix-similarity is a very effective lookup
metric — the user almost never re-types from scratch mid-utterance.

Design constraints:
  - Must never raise on the hot path. Everything is exception-swallowed.
  - Must not pile up: a single shared worker thread, debounced submissions,
    and a ``max_inflight`` cap drop further requests until one completes.
  - Pre-fetch results live for ``ttl_seconds`` so a fresh prompt build can
    reuse them; older entries are GC'd lazily.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from app.core.rag_retriever import RagRetriever
    from app.core.rag_store import RagHit


log = logging.getLogger("app.rag_prefetcher")


@dataclass(slots=True)
class _CachedFetch:
    query_norm: str
    query_raw: str
    hits: list["RagHit"]
    block: str
    fetched_at: float
    pending: bool = False
    waiters: list[threading.Event] = field(default_factory=list)


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace so prefix matches are stable."""
    return " ".join((text or "").lower().split())


def _prefix_similarity(a: str, b: str) -> float:
    """Score how similar two normalized strings are using prefix overlap.

    Returns 1.0 when one is a prefix of the other and both are non-empty,
    falling off proportionally as their common prefix shrinks. The metric
    is symmetric (we take the longer string as the denominator) so a
    short partial that's a prefix of a long final still scores well.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
    if longer.startswith(shorter):
        return len(shorter) / len(longer)
    # Fall back to longest common prefix length.
    common = 0
    for ca, cb in zip(longer, shorter):
        if ca != cb:
            break
        common += 1
    return common / len(longer)


class RagPrefetcher:
    """Background RAG pre-fetcher with a tiny TTL cache.

    Intended lifecycle:
      - ``submit(partial_text)`` is called from ``feed_stt_partial`` (every
        ~200ms) and is debounced + length-gated.
      - ``lookup(final_text)`` is called by ``PromptAssembler`` right
        before the prompt build; on a cache hit, it returns the formatted
        prompt block and the prompt build skips the live retrieval.
      - ``shutdown()`` is called from :class:`SessionController.shutdown`.
    """

    def __init__(
        self,
        retriever: "RagRetriever",
        *,
        ttl_seconds: float = 30.0,
        debounce_ms: int = 400,
        min_partial_chars: int = 8,
        similarity_threshold: float = 0.55,
        max_cached: int = 12,
        user_display_name_provider: Callable[[], str] | None = None,
    ) -> None:
        self._retriever = retriever
        # First-run identity: optional callable, evaluated per-fetch so a
        # mid-session rename takes effect on the next pre-warm without a
        # restart. None falls back to the formatter's generic placeholder.
        self._user_display_name_provider = user_display_name_provider
        self._ttl = float(ttl_seconds)
        self._debounce_s = max(0.0, debounce_ms / 1000.0)
        self._min_partial_chars = max(1, int(min_partial_chars))
        self._sim_threshold = max(0.0, min(1.0, float(similarity_threshold)))
        self._max_cached = max(1, int(max_cached))

        self._lock = threading.Lock()
        self._cache: dict[str, _CachedFetch] = {}
        self._last_submit_at = 0.0
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rag-prefetch"
        )
        self._closed = False

        # Telemetry counters (cheap, useful in MCP dump).
        self._stats = {
            "submitted": 0,
            "skipped_debounce": 0,
            "skipped_short": 0,
            "skipped_dup": 0,
            "completed": 0,
            "failed": 0,
            "lookup_hit": 0,
            "lookup_miss": 0,
        }

    # ── submission ──────────────────────────────────────────────────────

    def submit(
        self,
        partial_text: str,
        *,
        recent_turns: Iterable[str] | None = None,
        exclude_session_id: str | None = None,
    ) -> bool:
        """Maybe enqueue a background fetch for ``partial_text``.

        Returns True if a fetch was actually scheduled. The hot path
        ignores the return value; it's exposed for tests.
        """
        if self._closed:
            return False
        text = (partial_text or "").strip()
        if len(text) < self._min_partial_chars:
            self._stats["skipped_short"] += 1
            return False
        now = time.monotonic()
        if now - self._last_submit_at < self._debounce_s:
            self._stats["skipped_debounce"] += 1
            return False
        norm = _normalize(text)
        with self._lock:
            existing = self._cache.get(norm)
            if existing is not None and (
                existing.pending
                or now - existing.fetched_at < self._ttl
            ):
                self._stats["skipped_dup"] += 1
                self._last_submit_at = now
                return False
            entry = _CachedFetch(
                query_norm=norm,
                query_raw=text,
                hits=[],
                block="",
                fetched_at=now,
                pending=True,
            )
            self._cache[norm] = entry
            self._gc_locked(now)
        self._last_submit_at = now
        self._stats["submitted"] += 1
        recent_snapshot: tuple[str, ...] | None = (
            tuple(t for t in recent_turns if t) if recent_turns is not None else None
        )
        try:
            self._executor.submit(
                self._do_fetch, text, norm, recent_snapshot, exclude_session_id,
            )
        except RuntimeError:
            # Executor already shut down — drop silently.
            with self._lock:
                self._cache.pop(norm, None)
            return False
        return True

    def _do_fetch(
        self,
        query: str,
        norm: str,
        recent_turns: tuple[str, ...] | None,
        exclude_session_id: str | None,
    ) -> None:
        try:
            hits = self._retriever.retrieve(
                query,
                recent_turns=list(recent_turns) if recent_turns else None,
                exclude_session_id=exclude_session_id,
            )
            name = "the user"
            provider = self._user_display_name_provider
            if provider is not None:
                try:
                    name = (provider() or "").strip() or "the user"
                except Exception:
                    name = "the user"
            block = self._retriever.format_block(
                hits, user_display_name=name,
            )
        except Exception:
            log.debug("rag prefetch retrieval failed", exc_info=True)
            self._stats["failed"] += 1
            with self._lock:
                entry = self._cache.pop(norm, None)
                if entry is not None:
                    for ev in entry.waiters:
                        ev.set()
            return

        with self._lock:
            entry = self._cache.get(norm)
            if entry is None:
                # Evicted by GC; replant a minimal record so a near-immediate
                # lookup still benefits.
                entry = _CachedFetch(
                    query_norm=norm,
                    query_raw=query,
                    hits=hits,
                    block=block,
                    fetched_at=time.monotonic(),
                    pending=False,
                )
                self._cache[norm] = entry
            else:
                entry.hits = hits
                entry.block = block
                entry.fetched_at = time.monotonic()
                entry.pending = False
                for ev in entry.waiters:
                    ev.set()
                entry.waiters.clear()
        self._stats["completed"] += 1

    # ── lookup ──────────────────────────────────────────────────────────

    def lookup(
        self,
        final_text: str,
        *,
        wait_pending_seconds: float = 0.0,
    ) -> str | None:
        """Return a cached RAG block for ``final_text`` if recent enough.

        ``wait_pending_seconds > 0`` lets the prompt builder briefly block
        on an in-flight fetch if no completed entry is similar enough yet.
        Returns ``None`` on miss; the caller should then run the live
        retriever as usual.
        """
        if self._closed:
            return None
        norm = _normalize(final_text)
        if not norm:
            self._stats["lookup_miss"] += 1
            return None
        now = time.monotonic()

        def _pick_best() -> tuple[_CachedFetch | None, float]:
            best: tuple[_CachedFetch, float] | None = None
            with self._lock:
                self._gc_locked(now)
                for entry in self._cache.values():
                    if entry.pending:
                        continue
                    if now - entry.fetched_at > self._ttl:
                        continue
                    sim = _prefix_similarity(norm, entry.query_norm)
                    if sim < self._sim_threshold:
                        continue
                    if best is None or sim > best[1]:
                        best = (entry, sim)
            return (best[0] if best else None, best[1] if best else 0.0)

        entry, _sim = _pick_best()
        if entry is not None:
            self._stats["lookup_hit"] += 1
            return entry.block

        if wait_pending_seconds > 0.0:
            # Find any pending entry that *could* match and wait briefly.
            wait_event: threading.Event | None = None
            with self._lock:
                for cached in self._cache.values():
                    if not cached.pending:
                        continue
                    if _prefix_similarity(norm, cached.query_norm) < self._sim_threshold:
                        continue
                    ev = threading.Event()
                    cached.waiters.append(ev)
                    wait_event = ev
                    break
            if wait_event is not None:
                wait_event.wait(timeout=wait_pending_seconds)
                entry, _sim = _pick_best()
                if entry is not None:
                    self._stats["lookup_hit"] += 1
                    return entry.block

        self._stats["lookup_miss"] += 1
        return None

    # ── housekeeping ────────────────────────────────────────────────────

    def _gc_locked(self, now: float) -> None:
        """Evict expired entries; cap cache size by oldest fetched_at."""
        expired = [
            key for key, entry in self._cache.items()
            if not entry.pending and now - entry.fetched_at > self._ttl
        ]
        for key in expired:
            self._cache.pop(key, None)
        if len(self._cache) <= self._max_cached:
            return
        # Drop the oldest non-pending entries first.
        ordered = sorted(
            self._cache.items(),
            key=lambda item: (item[1].pending, item[1].fetched_at),
        )
        for key, entry in ordered:
            if entry.pending:
                continue
            self._cache.pop(key, None)
            if len(self._cache) <= self._max_cached:
                break

    def reset(self) -> None:
        """Clear the cache (e.g., on session change)."""
        with self._lock:
            for entry in self._cache.values():
                for ev in entry.waiters:
                    ev.set()
            self._cache.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                **self._stats,
                "cache_size": len(self._cache),
                "pending": sum(1 for e in self._cache.values() if e.pending),
            }

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            log.debug("prefetcher shutdown raised", exc_info=True)
        self.reset()


__all__ = ["RagPrefetcher"]

"""Thin wrapper around Ollama's ``/api/embeddings`` endpoint.

Used by :mod:`app.core.memory_store` and :mod:`app.core.memory_retriever` to
turn arbitrary text into a vector for cosine similarity. The model name is
controlled by :attr:`OllamaSettings.embedding_model` (default
``qwen3-embedding:0.6b`` -- already wired through ``config/default.json``).

A small in-memory LRU cache (keyed by sha1 of the text + model) keeps repeated
retrieval embeds from hitting the GPU on every turn.

P1 (perf backlog): per-turn embed budget. ``begin_turn`` / ``end_turn``
maintain thread-local counters of HTTP calls + cumulative wall time so the
turn runner can attribute "this turn was slow because of embeds" without a
custom log dive. The counters are thread-local on purpose -- the
``MessageIndexer`` runs on a background thread that shares this same
``Embedder`` instance, and we don't want its async embeds to pollute the
turn-thread's accounting. Cache hits (LRU) don't count as calls; only real
``/api/embeddings`` round-trips do.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Iterable

import numpy as np
import requests

from app.core.settings import OllamaSettings


log = logging.getLogger("app.embedder")


class Embedder:
    """Embed text via Ollama. Thread-safe; reuses one HTTP session."""

    def __init__(
        self,
        settings: OllamaSettings,
        *,
        model: str | None = None,
        timeout_seconds: float = 30.0,
        cache_size: int = 256,
    ) -> None:
        self._settings = settings
        self._model = (model or settings.embedding_model or "").strip()
        if not self._model:
            raise ValueError("Embedder needs a non-empty embedding model name")
        # Embeddings can run on a separate Ollama instance if configured.
        base = (settings.embedding_base_url or "").strip() or settings.base_url
        self._base_url = base.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self._session = requests.Session()
        # P1: thread-local turn counters. ``begin_turn`` initialises
        # the slot on the current thread; ``embed`` increments it on
        # every cache miss; ``end_turn`` reads it back and clears.
        # Threads that never call ``begin_turn`` (e.g. the
        # ``MessageIndexer`` background worker) just see ``active=False``
        # and skip accounting entirely.
        self._turn_local = threading.local()

    # ── public API ────────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    def embed(self, text: str) -> np.ndarray:
        """Return a unit-normalized embedding vector for ``text``."""
        normalized = (text or "").strip()
        if not normalized:
            raise ValueError("Embedder.embed: empty text")
        key = self._cache_key(normalized)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                # LRU touch.
                self._cache.move_to_end(key)
                return cached
        # P1: time the actual HTTP call. Cache hits above don't count
        # as calls (they're free); only round-trips go on the meter.
        call_start = time.perf_counter()
        vector = self._call_ollama(normalized)
        call_ms = (time.perf_counter() - call_start) * 1000.0
        self._record_turn_call(call_ms)
        with self._lock:
            self._cache[key] = vector
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return vector

    # ── P1: per-turn budget hooks ───────────────────────────────────────────

    def begin_turn(self) -> None:
        """Start counting embed calls + wall time on the current thread.

        Called by ``TurnRunner`` right before the prompt build so any
        embed that runs on the turn thread (RAG retrieval, K6 detector,
        K18 detector via K6) lands on this turn's budget. Idempotent
        within a thread -- a second call resets the counters, which is
        the right thing if a previous turn somehow forgot to call
        ``end_turn``.
        """
        self._turn_local.active = True
        self._turn_local.calls = 0
        self._turn_local.elapsed_ms = 0.0

    def end_turn(self) -> tuple[int, float]:
        """Stop counting and return ``(calls, elapsed_ms)`` for this thread.

        Safe to call without a matching ``begin_turn`` -- returns
        ``(0, 0.0)`` and leaves the thread-local slot uninitialised.
        Always clears state so the next turn starts cold.
        """
        active = bool(getattr(self._turn_local, "active", False))
        if not active:
            return (0, 0.0)
        calls = int(getattr(self._turn_local, "calls", 0) or 0)
        elapsed_ms = float(getattr(self._turn_local, "elapsed_ms", 0.0) or 0.0)
        self._turn_local.active = False
        self._turn_local.calls = 0
        self._turn_local.elapsed_ms = 0.0
        return (calls, elapsed_ms)

    def peek_turn_stats(self) -> tuple[int, float]:
        """Read the current thread's running turn counters without resetting.

        Useful for tests and ad-hoc instrumentation. Returns
        ``(0, 0.0)`` outside a turn boundary.
        """
        if not bool(getattr(self._turn_local, "active", False)):
            return (0, 0.0)
        return (
            int(getattr(self._turn_local, "calls", 0) or 0),
            float(getattr(self._turn_local, "elapsed_ms", 0.0) or 0.0),
        )

    def _record_turn_call(self, call_ms: float) -> None:
        """Add one HTTP call + its wall time to the current thread's budget.

        Silently no-ops on threads that haven't entered a turn (e.g.
        the ``MessageIndexer`` background worker), so ad-hoc embeds
        from tools / workers never accidentally land on a user turn.
        """
        if not bool(getattr(self._turn_local, "active", False)):
            return
        self._turn_local.calls = (
            int(getattr(self._turn_local, "calls", 0) or 0) + 1
        )
        self._turn_local.elapsed_ms = (
            float(getattr(self._turn_local, "elapsed_ms", 0.0) or 0.0)
            + float(call_ms)
        )

    def batch_embed(self, texts: Iterable[str]) -> list[np.ndarray]:
        """Embed a list of texts. Sequential -- Ollama doesn't batch over HTTP."""
        return [self.embed(t) for t in texts]

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ── internals ─────────────────────────────────────────────────────────

    def _cache_key(self, text: str) -> str:
        h = hashlib.sha1()
        h.update(self._model.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _call_ollama(self, text: str) -> np.ndarray:
        url = f"{self._base_url}/api/embeddings"
        payload = {"model": self._model, "prompt": text}
        try:
            response = self._session.post(url, json=payload, timeout=self._timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            log.warning("embedding request failed: %s", exc)
            raise
        embedding = data.get("embedding")
        if not embedding or not isinstance(embedding, list):
            raise RuntimeError(
                f"Ollama embedding response missing 'embedding' field for model {self._model}"
            )
        vector = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector = vector / norm
        return vector

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. Vectors should already be unit-norm.

    Falls back to a dot/norm calculation if either is not normalized.
    """
    if a is None or b is None:
        return 0.0
    if a.size == 0 or b.size == 0:
        return 0.0
    if a.shape != b.shape:
        return 0.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    if abs(na - 1.0) < 1e-3 and abs(nb - 1.0) < 1e-3:
        return float(np.dot(a, b))
    return float(np.dot(a, b) / (na * nb))

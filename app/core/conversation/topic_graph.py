"""Lightweight topic graph over the existing memory embeddings (K9).

Two consumers want a "where do these memories cluster, and how close
is *this* candidate to anything we've already discussed?" view:

- :class:`app.core.proactive.curiosity_seed_worker.CuriositySeedWorker` uses
  :meth:`is_close_to_any_cluster` as the "we've already covered that"
  filter when an LLM proposes new topics. Without this filter the
  worker would happily re-mine ground we've been on for weeks.
- The Memory tab UI panel surfaces a flat-list cluster view so the
  user can see what Aiko sees (and so a human can spot when a topic
  family has drifted into a single dense knot).

The graph is **not** persisted. It rebuilds in milliseconds from
:attr:`MemoryStore._mirror` (every :class:`Memory` already carries a
unit-norm ``embedding: np.ndarray``); persistence would only add
cache-coherence complexity without saving real wall time.

Clustering reuses the single-link cosine algorithm already shipped
in :func:`app.core.memory.memory_consolidator._cluster_memories` -- same
shape so the two cluster maps stay comparable. We keep the call here
as a thin re-export so callers don't need to depend on the
consolidator module.

Cache invalidation is keyed on ``(mirror_size, last_modified_token)``
where the token is just the maximum ``id`` currently in the mirror
plus the highest ``last_used_at`` seen. New writes flip the size,
deletes flip the size, and updates flip ``last_used_at``. That misses
in-place embedding edits without an ``last_used_at`` bump, but the
graph is purely advisory -- a stale read is at most one tick wrong
and the next memory write fixes it.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from app.core.memory.memory_consolidator import _cluster_memories

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore


log = logging.getLogger("app.topic_graph")


# Defaults so the class is constructible without a settings stub
# (used by tests + by the in-process default boot path before
# settings are read).
_DEFAULT_SIMILARITY: float = 0.55
_DEFAULT_MIN_CLUSTER_SIZE: int = 3
_DEFAULT_FILTER_THRESHOLD: float = 0.65


@dataclass(slots=True, frozen=True)
class TopicCluster:
    """One cluster in the topic graph.

    ``representative_id`` is the highest-salience member; ``summary``
    is the first sentence of that member's content trimmed to ~120
    chars so the UI panel has a label without re-doing any NLP. The
    full member list is exposed for callers that want to count
    density (number of memories in the cluster) or pull a richer
    view; the graph itself never stores embeddings beyond what the
    mirror already holds.
    """

    cluster_id: int
    representative_id: int
    summary: str
    member_ids: tuple[int, ...]
    member_kinds: tuple[str, ...]
    centroid: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))

    @property
    def size(self) -> int:
        return len(self.member_ids)


@dataclass(slots=True)
class _CachedGraph:
    """Internal snapshot keyed on the mirror identity tuple."""

    cache_key: tuple[int, int, str]
    clusters: tuple[TopicCluster, ...]
    # Flat list of every memory's normalised embedding the cluster
    # build saw. Used by :meth:`is_close_to_any_cluster` as a
    # cheap "any memory" lookup without rebuilding the cluster map.
    all_vectors: np.ndarray  # shape (N, D); empty when N == 0


def _trim_summary(text: str, *, max_chars: int = 120) -> str:
    """First sentence of ``text``, capped at ``max_chars``.

    Conservative: splits on the first ``. ``/``? ``/``! `` only;
    falls back to a hard char cap so a very long bullet point still
    becomes a usable label.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    for sep in (". ", "? ", "! "):
        idx = flat.find(sep)
        if 0 < idx < max_chars:
            return flat[: idx + 1].strip()
    if len(flat) > max_chars:
        return flat[: max_chars - 1].rstrip(",;: ") + "…"
    return flat


def _normalise(vec: np.ndarray) -> np.ndarray:
    """Return ``vec`` re-projected to unit norm (no-op when already
    normalised). The mirror already stores unit-norm vectors but the
    candidate vectors a worker passes in may not be."""
    arr = np.asarray(vec, dtype=np.float32)
    if arr.size == 0:
        return arr
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return arr
    return arr / norm


class TopicGraph:
    """Memory-graph wrapper exposing cluster + "is this fresh?" checks.

    Construction is cheap; the actual clustering is deferred to the
    first call of :meth:`topic_clusters` (or the first call of
    :meth:`is_close_to_any_cluster` when there's no cached snapshot
    yet). Subsequent calls reuse the snapshot until the mirror
    changes.

    Thread-safe: all reads + writes go through a single internal
    lock. The clustering call itself is allowed to release the lock
    so an idle worker doesn't block per-turn reads on it.
    """

    def __init__(
        self,
        memory_store: "MemoryStore",
        *,
        similarity: float = _DEFAULT_SIMILARITY,
        min_cluster_size: int = _DEFAULT_MIN_CLUSTER_SIZE,
        filter_threshold: float = _DEFAULT_FILTER_THRESHOLD,
    ) -> None:
        self._memory_store = memory_store
        self._similarity = float(similarity)
        self._min_cluster_size = max(2, int(min_cluster_size))
        self._filter_threshold = float(filter_threshold)
        self._lock = threading.Lock()
        self._cached: _CachedGraph | None = None

    # ── public API ────────────────────────────────────────────────────

    def update_runtime(
        self,
        *,
        similarity: float | None = None,
        min_cluster_size: int | None = None,
        filter_threshold: float | None = None,
    ) -> None:
        """Live-tune the thresholds without rebuilding right away.

        The next read invalidates the cache because ``_cache_key``
        folds the threshold in -- the stored snapshot was clustered
        against the previous threshold and would mislead callers
        otherwise.
        """
        with self._lock:
            if similarity is not None:
                self._similarity = float(similarity)
            if min_cluster_size is not None:
                self._min_cluster_size = max(2, int(min_cluster_size))
            if filter_threshold is not None:
                self._filter_threshold = float(filter_threshold)
            self._cached = None

    def topic_clusters(self) -> list[TopicCluster]:
        """Return the current cluster list, building lazily if needed."""
        snapshot = self._ensure_cached()
        return list(snapshot.clusters)

    def is_close_to_any_cluster(
        self,
        vec: np.ndarray,
        *,
        threshold: float | None = None,
    ) -> bool:
        """``True`` if ``vec`` is cosine-close to any existing memory.

        ``threshold`` defaults to ``self._filter_threshold``; pass an
        explicit value to override per-call (the seed worker uses
        this to relax / tighten depending on candidate source).

        Uses the flat ``all_vectors`` matrix (not just cluster
        centroids) so a topic that's close to a *singleton* memory
        still counts as "we discussed that". Empty mirror -> ``False``
        so a fresh DB doesn't filter every candidate to nothing.
        """
        thr = float(threshold) if threshold is not None else self._filter_threshold
        snapshot = self._ensure_cached()
        if snapshot.all_vectors.size == 0:
            return False
        candidate = _normalise(vec)
        if candidate.size == 0:
            return False
        if candidate.shape[0] != snapshot.all_vectors.shape[1]:
            log.debug(
                "topic_graph dimension mismatch (cand=%d store=%d)",
                int(candidate.shape[0]),
                int(snapshot.all_vectors.shape[1]),
            )
            return False
        sims = snapshot.all_vectors @ candidate
        return bool(np.any(sims >= thr))

    def best_match(
        self, vec: np.ndarray,
    ) -> tuple[float, int | None]:
        """Return ``(max_cosine, memory_id_or_None)`` for ``vec``.

        Useful for tests and for the worker's logging path so we can
        attribute *why* a candidate was rejected. ``memory_id`` is
        ``None`` when the mirror is empty.
        """
        snapshot = self._ensure_cached()
        if snapshot.all_vectors.size == 0:
            return 0.0, None
        candidate = _normalise(vec)
        if candidate.size == 0 or candidate.shape[0] != snapshot.all_vectors.shape[1]:
            return 0.0, None
        sims = snapshot.all_vectors @ candidate
        idx = int(np.argmax(sims))
        # ``all_member_ids`` lives in the same order as ``all_vectors``
        # because we built them in lockstep.
        return float(sims[idx]), self._all_member_ids[idx]

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next read rebuilds."""
        with self._lock:
            self._cached = None

    # ── internals ─────────────────────────────────────────────────────

    def _cache_key(self, mems: list["Memory"]) -> tuple[int, int, str]:
        """Identity tuple folding mirror size + max id + last touch.

        Cheap to compute (linear over the snapshot we already need
        for the cluster build), and tightly coupled to the mirror so
        an add / delete / touch flips at least one component.
        """
        size = len(mems)
        max_id = 0
        max_touch = ""
        for mem in mems:
            if mem.id > max_id:
                max_id = int(mem.id)
            touch = mem.last_used_at or mem.created_at or ""
            if touch and touch > max_touch:
                max_touch = touch
        # Fold thresholds so a runtime update invalidates the cache.
        thr_token = f"{self._similarity:.4f}|{self._min_cluster_size}|{self._filter_threshold:.4f}"
        return size, max_id, f"{max_touch}|{thr_token}"

    def _ensure_cached(self) -> _CachedGraph:
        """Return a cached snapshot, rebuilding when the mirror moved."""
        # Snapshot the mirror under the store's own lock by going
        # through ``iter_by_kind`` for every kind we care about would
        # be expensive; the consolidator-style read of a flat list is
        # enough. We intentionally include every kind because the
        # graph's purpose is "what topic territory have we covered" --
        # excluding e.g. ``self_tagged`` would let the seed worker
        # propose seeds Aiko has explicitly anchored as her own.
        memory_store = self._memory_store
        # Use the public mirror snapshot via locks. ``MemoryStore``
        # exposes ``iter_by_*`` helpers but not "all"; reach into
        # ``_mirror`` under its own lock explicitly. This is a
        # documented in-process touch (the same pattern
        # :class:`MemoryConsolidator` uses).
        with memory_store._lock:  # type: ignore[attr-defined]
            mems = [
                m for m in memory_store._mirror.values()  # type: ignore[attr-defined]
                if m.embedding is not None and m.embedding.size > 0
            ]

        key = self._cache_key(mems)
        with self._lock:
            cached = self._cached
            if cached is not None and cached.cache_key == key:
                return cached

        clusters_raw = _cluster_memories(
            mems,
            similarity=self._similarity,
            min_size=self._min_cluster_size,
        )
        clusters: list[TopicCluster] = []
        for idx, group in enumerate(clusters_raw):
            head = group[0]
            member_ids = tuple(int(m.id) for m in group)
            kinds = tuple(str(m.kind) for m in group)
            centroid = self._compute_centroid(group)
            clusters.append(
                TopicCluster(
                    cluster_id=idx,
                    representative_id=int(head.id),
                    summary=_trim_summary(head.content or ""),
                    member_ids=member_ids,
                    member_kinds=kinds,
                    centroid=centroid,
                )
            )

        if mems:
            all_vectors = np.stack(
                [_normalise(m.embedding) for m in mems], axis=0,
            )
            self._all_member_ids: list[int] = [int(m.id) for m in mems]
        else:
            all_vectors = np.zeros((0, 0), dtype=np.float32)
            self._all_member_ids = []

        snapshot = _CachedGraph(
            cache_key=key,
            clusters=tuple(clusters),
            all_vectors=all_vectors,
        )
        with self._lock:
            self._cached = snapshot
        log.debug(
            "topic_graph rebuilt: %d memories -> %d clusters",
            len(mems),
            len(clusters),
        )
        return snapshot

    @staticmethod
    def _compute_centroid(group: list["Memory"]) -> np.ndarray:
        """Mean of the cluster's unit vectors, re-normalised. Empty
        clusters return an empty array."""
        if not group:
            return np.zeros(0, dtype=np.float32)
        vecs = [_normalise(m.embedding) for m in group if m.embedding is not None]
        if not vecs:
            return np.zeros(0, dtype=np.float32)
        stacked = np.stack(vecs, axis=0)
        mean = stacked.mean(axis=0)
        return _normalise(mean)


__all__ = [
    "TopicCluster",
    "TopicGraph",
]

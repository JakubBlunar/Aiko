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

Clustering is a two-stage pipeline. First an **adaptive mutual-k-NN**
graph is built (see :func:`_cluster_memories_adaptive`): an undirected,
similarity-weighted edge between two memories only forms when each is in
the *other's* top-``k`` nearest neighbours, so a promiscuous "bridge"
memory (whose own top-``k`` slots are consumed by whichever family it is
closest to) cannot stitch two dense families together. ``k`` scales with
the corpus size (``~log2(n)+1``) so there is no global similarity
threshold to hand-tune; a conservative absolute ``floor`` only guards
against spurious links in sparse corners.

Second, that graph is partitioned by **Louvain community detection**
(see :func:`_partition_graph`) rather than plain connected components.
This matters because mutual-k-NN only stops *bridge chaining* -- it does
nothing when the entire corpus is densely + uniformly similar (e.g. a
single-person memory store where everything is loosely related to the
same life). In that regime there is always a chain of mutual edges
through the dense core, so connected components collapses the whole thing
into one giant blob. Modularity-based community detection finds
densely-internal sub-communities *within* a connected graph -- i.e. the
actual topics -- which connectivity alone cannot. The Louvain
``resolution`` (granularity) is auto-calibrated, never hand-tuned: a
corpus-size base is escalated while any single community still dominates
the graph, so the "one huge cluster" symptom self-corrects regardless of
how tightly the user's embeddings happen to pack. Connected components
remains the fallback when networkx is unavailable. This whole pipeline is
intentionally separate from the consolidator's merge clustering so the
two thresholds can evolve independently.

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
import math
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

try:  # networkx ships louvain_communities since 3.0; stay defensive anyway.
    import networkx as _nx
    from networkx.algorithms.community import (
        louvain_communities as _louvain_communities,
    )

    _HAS_NX = True
except Exception:  # pragma: no cover - networkx is a declared dep
    _nx = None  # type: ignore[assignment]
    _louvain_communities = None  # type: ignore[assignment]
    _HAS_NX = False

if TYPE_CHECKING:
    from app.core.conversation.topic_cluster_store import TopicClusterStore
    from app.core.memory.memory_store import Memory, MemoryStore


log = logging.getLogger("app.topic_graph")


# Defaults so the class is constructible without a settings stub
# (used by tests + by the in-process default boot path before
# settings are read). ``_DEFAULT_SIMILARITY`` is now the *edge floor*
# for the adaptive mutual-k-NN clusterer, not a single-link threshold:
# the floor only suppresses spurious links in sparse corners; the
# top-``k`` mutual-neighbour requirement does the real structural work.
_DEFAULT_SIMILARITY: float = 0.55
_DEFAULT_MIN_CLUSTER_SIZE: int = 3
_DEFAULT_FILTER_THRESHOLD: float = 0.65

# Upper bound on the per-node neighbour fan-out. Keeps the mutual-k-NN
# graph sparse on large corpora even though ``k`` grows with ``log2(n)``.
_K_MAX: int = 12
# Soft cap on the number of nodes fed into the O(n^2) similarity matrix.
# Above this we cluster only the most salient / most-used memories (the
# rest still feed the flat ``all_vectors`` path used by ``best_match`` /
# ``is_close_to_any_cluster``, which is a cheap per-query mat-vec).
_MAX_CLUSTER_NODES: int = 3000

# At or above this corpus size the batch rebuild routes through LanceDB
# ANN (``_cluster_memories_ann``) instead of the dense in-memory matrix,
# so the O(n^2) allocation never happens on a large / uncapped corpus.
_ANN_REBUILD_THRESHOLD: int = 2000


def _adaptive_k(n: int) -> int:
    """Neighbour fan-out for an ``n``-node corpus.

    Grows logarithmically (``round(log2(n)) + 1``) and is clamped to
    ``[2, _K_MAX]`` so a 100-memory corpus uses ~8 neighbours and a
    10k-memory corpus is capped at ``_K_MAX``. Tiny corpora (``n <= 3``)
    fall back to "everyone is a neighbour" so the floor + min-size gates
    are the only filter.
    """
    if n <= 3:
        return max(1, n - 1)
    return max(2, min(_K_MAX, int(round(math.log2(n))) + 1))


# ── Louvain community detection ────────────────────────────────────────
# Connected components on the mutual-k-NN graph still collapses a densely
# + uniformly similar corpus into one giant component (there is always a
# chain of mutual edges through the dense core). Louvain partitions that
# blob by modularity -- dense-inside / sparse-between -- into the actual
# topics. The resolution is auto-calibrated, never hand-tuned: a
# corpus-size base (``_adaptive_resolution``) is escalated while any
# single community still dominates the graph, so the "one huge cluster"
# symptom self-corrects regardless of how tightly the embeddings pack.
_LOUVAIN_SEED: int = 42
# Keep escalating resolution while the largest community holds more than
# this fraction of the graph's nodes (and more than the absolute floor).
_LOUVAIN_DOMINANCE: float = 0.35
_LOUVAIN_MIN_SPLIT_NODES: int = 15
_LOUVAIN_RESOLUTION_STEP: float = 1.5
_LOUVAIN_RESOLUTION_CAP: float = 8.0
_LOUVAIN_MAX_ESCALATIONS: int = 6


def _adaptive_resolution(n: int) -> float:
    """Base Louvain resolution for an ``n``-node corpus (pre-escalation).

    Grows gently with ``log10(n)`` so a larger corpus starts a touch more
    fragmented; the dominance-escalation loop in :func:`_partition_graph`
    does the real adaptive work, so this only needs to be a sane floor.
    """
    if n <= 10:
        return 1.0
    return float(min(2.5, 0.7 + 0.3 * math.log10(n)))


def _connected_components(
    n: int, edges: "list[tuple[int, int, float]]",
) -> list[list[int]]:
    """Union-find connected components -- the networkx-less fallback."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _w in edges:
        ra, rb = find(i), find(j)
        if ra != rb:
            parent[ra] = rb
    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    return list(groups.values())


def _partition_graph(
    n: int,
    edges: "list[tuple[int, int, float]]",
    *,
    meta: dict | None = None,
) -> list[list[int]]:
    """Partition a weighted mutual-k-NN graph into communities.

    Prefers Louvain modularity (dense-inside / sparse-between) over plain
    connected components so a single densely-connected blob is split into
    its constituent topics. The resolution is auto-escalated while the
    largest community still dominates the graph (more than
    ``_LOUVAIN_DOMINANCE`` of the nodes), which is the self-calibrating
    "no manual threshold" behaviour. Falls back to connected components
    when networkx is missing or Louvain raises. Returns node-index
    communities, unfiltered by size. ``meta`` (when supplied) is populated
    with the ``algorithm`` used and the final ``resolution``.
    """
    if n <= 0:
        return []
    if not edges or not _HAS_NX:
        if meta is not None:
            meta["algorithm"] = "mutual_knn"
            meta["resolution"] = 0.0
        return _connected_components(n, edges) if edges else [[i] for i in range(n)]

    graph = _nx.Graph()
    graph.add_nodes_from(range(n))
    for i, j, w in edges:
        # Modularity wants positive weights; clamp tiny / negative noise.
        graph.add_edge(i, j, weight=max(1e-6, float(w)))

    resolution = _adaptive_resolution(n)
    dominance_cap = max(_LOUVAIN_MIN_SPLIT_NODES, int(_LOUVAIN_DOMINANCE * n))
    best: list = []
    used_resolution = resolution
    for _ in range(_LOUVAIN_MAX_ESCALATIONS):
        try:
            communities = _louvain_communities(
                graph,
                weight="weight",
                resolution=resolution,
                seed=_LOUVAIN_SEED,
            )
        except Exception:
            log.debug("louvain failed; using connected components", exc_info=True)
            if meta is not None:
                meta["algorithm"] = "mutual_knn"
                meta["resolution"] = 0.0
            return _connected_components(n, edges)
        best = communities
        used_resolution = resolution
        largest = max((len(c) for c in communities), default=0)
        if largest <= dominance_cap or resolution >= _LOUVAIN_RESOLUTION_CAP:
            break
        resolution = min(_LOUVAIN_RESOLUTION_CAP, resolution * _LOUVAIN_RESOLUTION_STEP)
    if meta is not None:
        meta["algorithm"] = "mutual_knn_louvain"
        meta["resolution"] = float(used_resolution)
    log.debug(
        "louvain partition: n=%d edges=%d communities=%d resolution=%.2f",
        n, len(edges), len(best), used_resolution,
    )
    return [list(c) for c in best]


def _cluster_memories_adaptive(
    memories: "list[Memory]",
    *,
    min_size: int,
    floor: float,
    meta: dict | None = None,
) -> list[list["Memory"]]:
    """Adaptive **mutual-k-NN** graph + **Louvain** community detection.

    An undirected, similarity-weighted edge ``i--j`` is added only when
    *all three* hold:

    1. ``j`` is among ``i``'s top-``k`` most-similar memories,
    2. ``i`` is among ``j``'s top-``k`` (the *mutual* requirement), and
    3. ``cos(i, j) >= floor`` (sparse-corner safety net).

    The resulting graph is then partitioned by :func:`_partition_graph`
    (Louvain modularity, connected-components fallback) into communities
    with at least ``min_size`` members. Mutual-k-NN stops a promiscuous
    bridge memory from fusing two dense families; Louvain additionally
    splits a single densely-connected blob into its constituent topics,
    which connectivity alone cannot. ``k`` is derived from the corpus
    size and the Louvain resolution is auto-calibrated, so there is no
    global threshold to tune.

    Returns each group sorted by ``(salience, use_count)`` descending so
    ``group[0]`` is the natural representative -- identical shape to the
    consolidator's ``_cluster_memories`` so the rest of the graph code is
    unchanged. ``meta`` (when supplied) is populated by the partitioner.
    """
    items = list(memories)
    n = len(items)
    if n < min_size:
        return []
    # Bound the O(n^2) structure build on very large corpora.
    if n > _MAX_CLUSTER_NODES:
        items = sorted(
            items,
            key=lambda m: (float(m.salience), int(m.use_count)),
            reverse=True,
        )[:_MAX_CLUSTER_NODES]
        n = len(items)

    vecs = [_normalise(m.embedding) for m in items]
    # Guard against a ragged set of dims (mixed embedding models): drop
    # to the modal dimension so the stack/matmul is well-formed.
    dim = vecs[0].shape[0] if vecs and vecs[0].size else 0
    if dim == 0 or any(v.shape[0] != dim for v in vecs):
        usable = [(m, v) for m, v in zip(items, vecs) if v.shape[0] == dim and dim]
        if len(usable) < min_size:
            return []
        items = [m for m, _ in usable]
        vecs = [v for _, v in usable]
        n = len(items)

    matrix = np.stack(vecs, axis=0)  # (n, dim), unit-norm rows
    sims = matrix @ matrix.T  # (n, n) cosine
    np.fill_diagonal(sims, -1.0)

    k = _adaptive_k(n)
    if k >= n - 1:
        topk = np.argsort(-sims, axis=1)[:, : max(1, n - 1)]
    else:
        topk = np.argpartition(-sims, k, axis=1)[:, :k]
    neighbours = [set(int(j) for j in topk[i]) for i in range(n)]

    edges: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in neighbours[i]:
            if j <= i:
                continue
            sim_ij = float(sims[i, j])
            if i in neighbours[j] and sim_ij >= floor:
                edges.append((i, j, sim_ij))

    communities = _partition_graph(n, edges, meta=meta)
    out: list[list["Memory"]] = []
    for comp in communities:
        if len(comp) < min_size:
            continue
        group = [items[idx] for idx in comp]
        out.append(
            sorted(
                group,
                key=lambda m: (float(m.salience), int(m.use_count)),
                reverse=True,
            )
        )
    return out


def _cluster_memories_ann(
    memories: "list[Memory]",
    rag_store: "Any",
    *,
    min_size: int,
    floor: float,
    meta: dict | None = None,
) -> list[list["Memory"]]:
    """Same mutual-k-NN + Louvain clustering as
    :func:`_cluster_memories_adaptive` but sourcing each node's neighbours
    from LanceDB ANN instead of an in-memory ``n x n`` matrix.

    This is the **scale** path: graph build memory is ``O(n*k)`` (sparse
    adjacency), not ``O(n^2)``, so it survives a large / uncapped corpus.
    Each node costs one ANN query (sub-linear once
    :meth:`RagStore.ensure_vector_index` has built an index); the same
    :func:`_partition_graph` (Louvain) step then carves it into topics.
    Falls back to returning ``[]`` on any ANN failure so the caller can
    retry the dense path.
    """
    items = list(memories)
    n = len(items)
    if n < min_size:
        return []
    id_to_idx = {int(m.id): i for i, m in enumerate(items)}
    k = _adaptive_k(n)
    neighbours: list[set[int]] = [set() for _ in range(n)]
    sims: dict[tuple[int, int], float] = {}
    try:
        for i, mem in enumerate(items):
            if mem.embedding is None or mem.embedding.size == 0:
                continue
            hits = rag_store.knn_memories(
                mem.embedding, top_k=k, min_score=floor, exclude_id=str(int(mem.id)),
            )
            for mid, sim in hits:
                j = id_to_idx.get(int(mid))
                if j is None or j == i:
                    continue
                neighbours[i].add(j)
                sims[(min(i, j), max(i, j))] = float(sim)
    except Exception:
        log.debug("ann cluster build failed; caller should fall back", exc_info=True)
        return []

    edges: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in neighbours[i]:
            if j <= i:
                continue
            sim_ij = sims.get((i, j), 0.0)
            if i in neighbours[j] and sim_ij >= floor:
                edges.append((i, j, float(sim_ij)))

    communities = _partition_graph(n, edges, meta=meta)
    out: list[list["Memory"]] = []
    for comp in communities:
        if len(comp) < min_size:
            continue
        group = [items[idx] for idx in comp]
        out.append(
            sorted(
                group,
                key=lambda m: (float(m.salience), int(m.use_count)),
                reverse=True,
            )
        )
    return out


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
class _LiveCluster:
    """Mutable in-process cluster used by the persistent/incremental
    mode. ``centroid`` is a running unit-norm mean; ``member_ids`` is the
    live membership set. Representative / summary / kinds are derived on
    demand from the memory mirror (not stored here)."""

    cluster_id: int
    centroid: np.ndarray
    member_ids: set[int]
    label: str = ""


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
        cluster_store: "TopicClusterStore | None" = None,
        rag_store: Any = None,
        assign_threshold: float | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._similarity = float(similarity)
        self._min_cluster_size = max(2, int(min_cluster_size))
        self._filter_threshold = float(filter_threshold)
        self._lock = threading.Lock()
        self._cached: _CachedGraph | None = None
        # Neighbour fan-out + Louvain resolution + algorithm used on the
        # last build (advisory; surfaced in the snapshot so the UI can show
        # how the graph was carved).
        self._last_k: int = 0
        self._last_resolution: float = 0.0
        self._last_algorithm: str = "mutual_knn_louvain"

        # ── persistent / incremental mode ─────────────────────────────
        # Active only when a ``cluster_store`` is injected. Without it the
        # class behaves exactly as before: a lazy in-memory rebuild keyed
        # on the mirror identity (the path tests + the no-DB boot use).
        self._cluster_store = cluster_store
        self._rag_store = rag_store
        # Threshold for "close enough to join an existing cluster" on the
        # incremental path; reuses the edge floor by default.
        self._assign_threshold = (
            float(assign_threshold)
            if assign_threshold is not None
            else float(similarity)
        )
        self._live: dict[int, _LiveCluster] = {}
        self._assignment: dict[int, int] = {}
        self._warm = False
        self._pending_unclustered = 0

    @property
    def persistent(self) -> bool:
        """True when running in the persisted/incremental mode."""
        return self._cluster_store is not None

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
        """Return the current cluster list.

        Persistent mode: returns the live (persisted + incrementally
        maintained) state, warm-starting from SQLite on first call and
        only doing a full rebuild when there is no persisted state.
        Default mode: lazy in-memory rebuild keyed on the mirror identity.
        """
        if not self.persistent:
            snapshot = self._ensure_cached()
            return list(snapshot.clusters)
        self._ensure_warm()
        with self._lock:
            return self._live_to_topic_clusters_locked()

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
        # Persistent mode with ANN: a single nearest-neighbour query is
        # cheaper than holding the full ``all_vectors`` matrix in memory
        # and scales to an uncapped corpus.
        if self.persistent and self._rag_store is not None:
            sim, _ = self._best_match_ann(vec)
            return sim >= thr
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
        if self.persistent and self._rag_store is not None:
            return self._best_match_ann(vec)
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
        """Drop the cached snapshot so the next read rebuilds.

        In persistent mode this also clears the warm flag so the next
        read warm-starts from SQLite again (used by the MCP
        ``force_topic_graph_rebuild`` tool, which then calls
        :meth:`rebuild`).
        """
        with self._lock:
            self._cached = None
            self._warm = False

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

        meta: dict = {}
        clusters_raw = _cluster_memories_adaptive(
            mems,
            min_size=self._min_cluster_size,
            floor=self._similarity,
            meta=meta,
        )
        self._last_k = _adaptive_k(len(mems)) if mems else 0
        self._last_resolution = float(meta.get("resolution", 0.0))
        self._last_algorithm = str(meta.get("algorithm", "mutual_knn_louvain"))
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
            "topic_graph rebuilt: %d memories -> %d clusters "
            "(k=%d floor=%.2f algo=%s res=%.2f)",
            len(mems),
            len(clusters),
            self._last_k,
            self._similarity,
            self._last_algorithm,
            self._last_resolution,
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

    # ── persistent / incremental mode ──────────────────────────────────

    def _snapshot_mirror(self) -> list["Memory"]:
        """Embedded-memory snapshot taken under the *store* lock only
        (never while holding the topic-graph lock), so the two locks are
        always acquired in a single, consistent order."""
        ms = self._memory_store
        with ms._lock:  # type: ignore[attr-defined]
            return [
                m
                for m in ms._mirror.values()  # type: ignore[attr-defined]
                if m.embedding is not None and m.embedding.size > 0
            ]

    def _best_match_ann(self, vec: np.ndarray) -> tuple[float, int | None]:
        try:
            hits = self._rag_store.knn_memories(_normalise(vec), top_k=1)
        except Exception:
            log.debug("best_match_ann failed", exc_info=True)
            return 0.0, None
        if not hits:
            return 0.0, None
        mid, sim = hits[0]
        return float(sim), int(mid)

    def _ensure_warm(self) -> None:
        """Warm the live state without doing heavy work under the lock.

        Tries a cheap SQLite warm-start first; only falls back to a full
        :meth:`rebuild` (heavy, lock released) when there is no persisted
        state at all (fresh install / first run after the upgrade).
        """
        with self._lock:
            if self._warm:
                return
        clusters: list = []
        assignments: dict[int, int] = {}
        try:
            clusters, assignments = self._cluster_store.load_all()  # type: ignore[union-attr]
        except Exception:
            log.warning("topic_graph warm-start load failed", exc_info=True)
        if clusters:
            live: dict[int, _LiveCluster] = {}
            for row in clusters:
                centroid = (
                    _normalise(row.centroid)
                    if getattr(row.centroid, "size", 0)
                    else np.zeros(0, dtype=np.float32)
                )
                live[row.cluster_id] = _LiveCluster(
                    cluster_id=row.cluster_id,
                    centroid=centroid,
                    member_ids=set(),
                    label=row.label,
                )
            assign: dict[int, int] = {}
            for mid, cid in assignments.items():
                cluster = live.get(cid)
                if cluster is not None:
                    cluster.member_ids.add(int(mid))
                    assign[int(mid)] = cid
            live = {cid: c for cid, c in live.items() if c.member_ids}
            with self._lock:
                if not self._warm:
                    self._live = live
                    self._assignment = assign
                    self._warm = True
            log.debug("topic_graph warm-start: %d clusters from store", len(live))
            return
        # No persisted graph yet -> full build (also persists).
        self.rebuild()

    def rebuild(self) -> int:
        """Full batch re-cluster from the memory mirror (the heavy path).

        Routes through LanceDB ANN above ``_ANN_REBUILD_THRESHOLD`` rows
        (sparse, ``O(n*k)``) and the dense in-memory matrix below it.
        Recomputes centroids + memberships, persists the whole graph via
        :meth:`TopicClusterStore.replace_all`, and swaps the live state in
        under the lock. Returns the number of clusters. No-op (returns
        current cluster count) when not in persistent mode.
        """
        if not self.persistent:
            return len(self.topic_clusters())
        mems = self._snapshot_mirror()
        n = len(mems)
        clusters_raw: list[list["Memory"]] = []
        used_ann = False
        meta: dict = {}
        if self._rag_store is not None and n >= _ANN_REBUILD_THRESHOLD:
            try:
                self._rag_store.ensure_vector_index()
            except Exception:
                log.debug("ensure_vector_index during rebuild failed", exc_info=True)
            clusters_raw = _cluster_memories_ann(
                mems, self._rag_store,
                min_size=self._min_cluster_size, floor=self._similarity,
                meta=meta,
            )
            used_ann = bool(clusters_raw) or n == 0
        if not used_ann:
            meta = {}
            clusters_raw = _cluster_memories_adaptive(
                mems, min_size=self._min_cluster_size, floor=self._similarity,
                meta=meta,
            )
        self._last_k = _adaptive_k(n) if n else 0
        self._last_resolution = float(meta.get("resolution", 0.0))
        self._last_algorithm = str(meta.get("algorithm", "mutual_knn_louvain"))

        live: dict[int, _LiveCluster] = {}
        assignment: dict[int, int] = {}
        rows: list = []
        from app.core.conversation.topic_cluster_store import ClusterRow

        for idx, group in enumerate(clusters_raw, start=1):
            member_ids = {int(m.id) for m in group}
            centroid = self._compute_centroid(group)
            label = _trim_summary((group[0].content or "")) if group else ""
            live[idx] = _LiveCluster(
                cluster_id=idx, centroid=centroid,
                member_ids=member_ids, label=label,
            )
            for mid in member_ids:
                assignment[mid] = idx
            rows.append(
                ClusterRow(
                    cluster_id=idx, label=label,
                    centroid=centroid, size=len(member_ids),
                )
            )
        try:
            self._cluster_store.replace_all(rows, assignment)  # type: ignore[union-attr]
        except Exception:
            log.warning("topic_graph rebuild persist failed", exc_info=True)
        with self._lock:
            self._live = live
            self._assignment = assignment
            self._warm = True
            self._pending_unclustered = 0
        log.info(
            "topic_graph batch rebuild: %d memories -> %d clusters "
            "(k=%d floor=%.2f algo=%s res=%.2f ann=%s)",
            n, len(live), self._last_k, self._similarity,
            self._last_algorithm, self._last_resolution, used_ann,
        )
        return len(live)

    def on_memory_added(self, memory: "Memory") -> None:
        """Incrementally place a freshly-added memory (no full rebuild).

        Assigns to the nearest cluster centroid when the cosine clears
        ``assign_threshold``; otherwise leaves the memory unclustered
        (the next batch refit folds it in). Updates the centroid as a
        running unit-norm mean and persists just the touched rows.
        """
        if not self.persistent:
            return
        emb = getattr(memory, "embedding", None)
        if emb is None or getattr(emb, "size", 0) == 0:
            return
        self._ensure_warm()
        vec = _normalise(emb)
        mid = int(memory.id)
        with self._lock:
            best_cid: int | None = None
            best_sim = -1.0
            for cid, cluster in self._live.items():
                if cluster.centroid.size != vec.size:
                    continue
                sim = float(np.dot(cluster.centroid, vec))
                if sim > best_sim:
                    best_sim, best_cid = sim, cid
            if best_cid is not None and best_sim >= self._assign_threshold:
                cluster = self._live[best_cid]
                size = len(cluster.member_ids)
                cluster.member_ids.add(mid)
                # Running mean: keeps the centroid representative without
                # re-reading every member vector.
                if cluster.centroid.size == vec.size and size > 0:
                    cluster.centroid = _normalise(
                        (cluster.centroid * size + vec) / (size + 1)
                    )
                else:
                    cluster.centroid = vec
                self._assignment[mid] = best_cid
                row = _LiveClusterToRow(cluster)
                store = self._cluster_store
            else:
                self._pending_unclustered += 1
                store = None
                row = None
        if store is not None and row is not None:
            try:
                store.upsert_cluster(row)
                store.set_assignment(mid, row.cluster_id)
            except Exception:
                log.debug("on_memory_added persist failed", exc_info=True)

    def on_memory_deleted(self, memory_id: int) -> None:
        """Drop a deleted memory from the live graph + persisted store.

        Removes the member from its cluster (dropping the cluster when it
        empties). The centroid is left approximate -- the next batch refit
        re-derives it -- because we don't keep per-member vectors in the
        live state."""
        if not self.persistent:
            return
        mid = int(memory_id)
        drop_cluster: int | None = None
        touched: _LiveCluster | None = None
        with self._lock:
            cid = self._assignment.pop(mid, None)
            if cid is not None:
                cluster = self._live.get(cid)
                if cluster is not None:
                    cluster.member_ids.discard(mid)
                    if not cluster.member_ids:
                        drop_cluster = cid
                        self._live.pop(cid, None)
                    else:
                        touched = cluster
        store = self._cluster_store
        try:
            store.delete_assignment(mid)  # type: ignore[union-attr]
            if drop_cluster is not None:
                store.delete_cluster(drop_cluster)  # type: ignore[union-attr]
            elif touched is not None:
                store.upsert_cluster(_LiveClusterToRow(touched))  # type: ignore[union-attr]
        except Exception:
            log.debug("on_memory_deleted persist failed", exc_info=True)

    def pending_count(self) -> int:
        """Number of memories added since the last batch refit that did
        not fit any existing cluster (drives the worker's refit trigger)."""
        with self._lock:
            return int(self._pending_unclustered)

    def _live_to_topic_clusters_locked(self) -> list[TopicCluster]:
        """Build the public ``TopicCluster`` list from live state.

        Joins member ids back to the mirror for representative / summary /
        kinds (cheap, ``O(members)``). Called with ``self._lock`` held;
        ``memory_store.get`` acquires the store lock (TG->MS order, never
        the inverse), so this is deadlock-safe."""
        get = self._memory_store.get
        out: list[TopicCluster] = []
        for cid, cluster in self._live.items():
            if len(cluster.member_ids) < self._min_cluster_size:
                continue
            members = [m for m in (get(mid) for mid in cluster.member_ids) if m is not None]
            if len(members) < self._min_cluster_size:
                continue
            members.sort(
                key=lambda m: (float(m.salience), int(m.use_count)), reverse=True
            )
            head = members[0]
            out.append(
                TopicCluster(
                    cluster_id=cid,
                    representative_id=int(head.id),
                    summary=cluster.label or _trim_summary(head.content or ""),
                    member_ids=tuple(int(m.id) for m in members),
                    member_kinds=tuple(str(m.kind) for m in members),
                    centroid=cluster.centroid,
                )
            )
        out.sort(key=lambda c: len(c.member_ids), reverse=True)
        return out


def _LiveClusterToRow(cluster: "_LiveCluster"):
    from app.core.conversation.topic_cluster_store import ClusterRow

    return ClusterRow(
        cluster_id=cluster.cluster_id,
        label=cluster.label,
        centroid=cluster.centroid,
        size=len(cluster.member_ids),
    )


def _trim_member(text: str, *, max_chars: int) -> str:
    """Collapse whitespace and hard-cap a member's content for the UI."""
    flat = " ".join(str(text or "").split())
    if len(flat) > max_chars:
        return flat[: max_chars - 1].rstrip(",;: ") + "\u2026"
    return flat


def build_topic_graph_snapshot(
    topic_graph: "TopicGraph | None",
    memory_store: "MemoryStore | None",
    *,
    max_member_chars: int = 160,
) -> dict:
    """Serialise the K9 topic graph into a JSON-friendly dict.

    This is the single source of truth behind the ``GET /api/topic-graph``
    REST endpoint and the ``get_topic_graph`` MCP tool. It is pure (no
    I/O beyond reading the in-process mirror), so it is unit-testable
    without a full :class:`SessionController`.

    When ``topic_graph`` or ``memory_store`` is ``None`` (feature
    disabled, init failed, or memory subsystem absent) it returns an
    empty-but-valid shape with ``enabled=False`` so callers never have
    to special-case the disabled path.

    Each cluster joins its ``member_ids`` back to the live
    :class:`Memory` rows for content / kind / salience / tier; missing
    rows (deleted between cluster build and snapshot) are skipped.
    Clusters are sorted by size descending so the densest topic
    knots -- the ones most worth eyeballing -- sit at the top.
    """
    if topic_graph is None or memory_store is None:
        return {
            "enabled": False,
            "total_memories": 0,
            "total_clusters": 0,
            "clustered_memories": 0,
            "similarity": 0.0,
            "min_cluster_size": 0,
            "filter_threshold": 0.0,
            "clusters": [],
        }

    clusters_out: list[dict] = []
    clustered_memories = 0
    for cluster in topic_graph.topic_clusters():
        members: list[dict] = []
        kind_counts: dict[str, int] = {}
        for mid in cluster.member_ids:
            mem = memory_store.get(int(mid))
            if mem is None:
                continue
            kind = str(mem.kind)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            members.append({
                "id": int(mem.id),
                "content": _trim_member(mem.content, max_chars=max_member_chars),
                "kind": kind,
                "salience": float(mem.salience),
                "tier": str(mem.tier),
            })
        if not members:
            continue
        clustered_memories += len(members)
        clusters_out.append({
            "cluster_id": int(cluster.cluster_id),
            "summary": cluster.summary,
            "size": len(members),
            "representative_id": int(cluster.representative_id),
            "kind_counts": kind_counts,
            "members": members,
        })

    clusters_out.sort(key=lambda c: c["size"], reverse=True)

    # Total memories with a usable embedding -- the denominator for the
    # "what fraction of memory has clustered" readout in the UI header.
    total_memories = 0
    try:
        with memory_store._lock:  # type: ignore[attr-defined]
            total_memories = sum(
                1
                for m in memory_store._mirror.values()  # type: ignore[attr-defined]
                if m.embedding is not None and m.embedding.size > 0
            )
    except Exception:
        total_memories = clustered_memories

    return {
        "enabled": True,
        "total_memories": total_memories,
        "total_clusters": len(clusters_out),
        "clustered_memories": clustered_memories,
        "algorithm": str(getattr(topic_graph, "_last_algorithm", "mutual_knn_louvain")),
        "neighbors_k": int(getattr(topic_graph, "_last_k", 0)),
        "resolution": float(getattr(topic_graph, "_last_resolution", 0.0)),
        "similarity": float(getattr(topic_graph, "_similarity", 0.0)),
        "min_cluster_size": int(getattr(topic_graph, "_min_cluster_size", 0)),
        "filter_threshold": float(getattr(topic_graph, "_filter_threshold", 0.0)),
        # Persistence / incremental telemetry (v20). ``persistent`` flips
        # true once a TopicClusterStore is wired; ``pending_unclustered``
        # is how many incrementally-added memories are waiting for the
        # next batch refit to place them.
        "persistent": bool(getattr(topic_graph, "persistent", False)),
        "pending_unclustered": int(getattr(topic_graph, "_pending_unclustered", 0)),
        "clusters": clusters_out,
    }


__all__ = [
    "TopicCluster",
    "TopicGraph",
    "build_topic_graph_snapshot",
]

"""Persistence for the L1 higher-order concept layer (schema v21).

A **concept** is a cross-cluster abstraction that sits above topic
clusters -- a first-class, long-lived entity with a lifecycle, evidence,
and confidence (see ``docs/personality-backlog/concepts.md``). This
store owns two kind-agnostic SQLite tables:

- ``concepts`` -- one row per concept (label, ``kind`` / ``subject`` /
  ``evidence_model`` axes, ``status`` / ``confidence`` / ``plasticity``
  lifecycle fields, a label ``embedding`` blob, and provenance).
- ``concept_edges`` -- one typed, directed, signed influence graph.

**Storage & retrieval strategy (L1 decision).** SQLite is the source of
truth; there is deliberately *no* LanceDB mirror. Concept cardinality
stays small by design (a concept compresses many clusters, which
compress many memories), so the store keeps an in-process embedding
mirror -- a ``dict[id -> unit-norm vector]`` plus a cached stacked matrix
of the ``active`` set -- and answers similarity queries with a tiny numpy
cosine matmul (the concept analog of nearest-centroid in
:mod:`app.core.conversation.topic_graph`, not an ANN search). Every
consumer (L2 dedup, L5 recall, L23 selection, L24 derivers) retrieves
through the one :meth:`ConceptStore.nearest` primitive; the escape hatch
if the active set ever explodes is to add an ANN index behind that same
seam without changing callers.

**Edge direction convention.** An edge always points *from the
supporting node toward the node that depends on it*. So ``evidence`` is
``memory|cluster -> concept`` and a meta ``references`` edge is
``base concept -> meta concept``. Consequently
:meth:`ConceptStore.dependents_of` walks ``src -> dst`` (a base's
dependents are the concepts reachable from it), which is what the L1
cascade rule needs when a base concept changes status.

**Single writer.** The store exposes concept + edge CRUD, but by
convention only the L3 lifecycle engine mutates ``confidence`` /
``plasticity`` / ``status`` -- the proposer (L2) only *creates*
candidates and ``evidence`` edges. L1 provides the mechanism; it does
not schedule any mutation itself.

Like :class:`app.core.conversation.topic_cluster_store.TopicClusterStore`
this talks to SQLite directly and does cascade cleanup in Python
(:meth:`delete`, :meth:`delete_for_memory`) rather than via SQL foreign
keys, because ``memories`` / ``concepts`` rows are owned by in-process
mirrors that would drift if SQL deleted rows behind their back.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.concept_store")

# Sentinel so ``nearest(user_id=None)`` (match the global/aiko scope)
# is distinguishable from ``nearest()`` (don't filter on user at all).
_UNSET: object = object()

# Column list for ``concepts`` reads, kept in one place so row unpacking
# stays in sync with the SELECT order.
_CONCEPT_COLS = (
    "id, label, kind, subject, user_id, evidence_model, status, "
    "confidence, plasticity, evidence_count, distinct_source_count, "
    "rationale, embedding, dim, origin_session, first_evidence_at, "
    "created_at, updated_at, last_reinforced_at, promoted_at, "
    "last_lifecycle_at, last_lifecycle_engagement, first_evidence_engagement"
)


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


def _encode_embedding(vec: "np.ndarray | None") -> tuple[bytes, int]:
    """Serialise a label embedding to raw float32 bytes + its dim.

    The raw (un-normalised) vector is persisted so recall can
    reconstruct it; the in-process mirror normalises separately for
    cosine.
    """
    if vec is None:
        return b"", 0
    arr = np.asarray(vec, dtype=np.float32).ravel()
    return arr.tobytes(), int(arr.size)


def _decode_embedding(blob: "bytes | None", dim: int) -> np.ndarray:
    if not blob or dim <= 0:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != dim:
        # Dimension drift (embedding-model swap): treat as absent so the
        # concept is re-embedded rather than crashing a cosine matmul.
        return np.zeros(0, dtype=np.float32)
    return np.array(arr, dtype=np.float32)  # copy: frombuffer is read-only


def _unit(vec: "np.ndarray | None") -> np.ndarray:
    """Return a unit-norm float32 copy, or an empty array for a zero /
    empty vector."""
    if vec is None:
        return np.zeros(0, dtype=np.float32)
    arr = np.asarray(vec, dtype=np.float32).ravel()
    if arr.size == 0:
        return arr
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return np.zeros(0, dtype=np.float32)
    return arr / norm


@dataclass(slots=True)
class Concept:
    """One persisted concept. ``embedding`` is the raw (un-normalised)
    label embedding; an empty array means "not embedded yet"."""

    label: str
    kind: str = "identity"
    subject: str = "user"
    user_id: str | None = None
    evidence_model: str = "set"
    status: str = "candidate"
    confidence: float = 0.5
    plasticity: float = 0.5
    evidence_count: int = 0
    distinct_source_count: int = 0
    rationale: str = ""
    embedding: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32)
    )
    origin_session: str | None = None
    first_evidence_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_reinforced_at: str | None = None
    promoted_at: str | None = None
    # L3 per-concept engagement anchor (written only by the lifecycle
    # worker). ``last_lifecycle_at`` NULL => never evaluated.
    last_lifecycle_at: str | None = None
    last_lifecycle_engagement: float | None = None
    # L3 engagement anchor for *age*: the engagement-clock ``total()`` at
    # first evidence, so promotion / candidate-TTL age is measured in
    # engaged (active-conversation) time, symmetric with decay. NULL =>
    # not yet anchored (the lifecycle worker stamps it on first eval;
    # ``_age_days`` falls back to wall-clock until then).
    first_evidence_engagement: float | None = None
    concept_id: int = 0


@dataclass(slots=True)
class ConceptEdge:
    """One typed, directed, signed edge in the influence graph. Node ids
    are stored as TEXT so ``concept`` / ``memory`` / stable ``cluster``
    keys share one column type."""

    src_type: str
    src_id: str
    dst_type: str
    dst_id: str
    relation: str
    polarity: int = 1
    strength: float = 1.0
    ordinal: int | None = None
    edge_id: int = 0
    created_at: str = ""


class ConceptStore:
    """CRUD + in-process cosine mirror for ``concepts`` /
    ``concept_edges``."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db
        # L17c: optional sink for absorption records. ``merge_into``
        # deletes the absorbed row, so unless something captures the
        # mapping at that moment the id becomes a dead end and any
        # history ending in a merge is unreachable. Injected rather than
        # imported so the store keeps its single dependency on the db.
        self._alias_sink: Callable[[dict[str, object]], None] | None = None
        # In-process mirror (see module docstring). Small by design.
        self._concepts: dict[int, Concept] = {}
        self._vectors: dict[int, np.ndarray] = {}  # unit-norm
        # Cached stacked matrix of the active set, rebuilt lazily on the
        # next query after any write marks it dirty.
        self._active_dirty: bool = True
        self._active_ids: list[int] = []
        self._active_mat: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._active_dim: int = 0

    # ── mirror maintenance ────────────────────────────────────────────

    def _put_mirror(self, concept: Concept) -> None:
        cid = concept.concept_id
        self._concepts[cid] = concept
        unit = _unit(concept.embedding)
        if unit.size:
            self._vectors[cid] = unit
        else:
            self._vectors.pop(cid, None)
        self._active_dirty = True

    def _drop_mirror(self, concept_id: int) -> None:
        self._concepts.pop(concept_id, None)
        self._vectors.pop(concept_id, None)
        self._active_dirty = True

    def _ensure_active_cache(self) -> None:
        if not self._active_dirty:
            return
        # Pick the majority embedding dim so a mid-swap mix of dims can't
        # break the vstack; minority-dim vectors fall back to the
        # per-query filter path.
        dims: dict[int, int] = {}
        for cid, concept in self._concepts.items():
            if concept.status == "active" and cid in self._vectors:
                d = int(self._vectors[cid].size)
                dims[d] = dims.get(d, 0) + 1
        if dims:
            self._active_dim = max(dims, key=lambda d: dims[d])
            ids = [
                cid
                for cid, concept in self._concepts.items()
                if concept.status == "active"
                and cid in self._vectors
                and self._vectors[cid].size == self._active_dim
            ]
            self._active_ids = ids
            self._active_mat = (
                np.vstack([self._vectors[i] for i in ids])
                if ids
                else np.zeros((0, 0), dtype=np.float32)
            )
        else:
            self._active_dim = 0
            self._active_ids = []
            self._active_mat = np.zeros((0, 0), dtype=np.float32)
        self._active_dirty = False

    def _filtered_matrix(
        self,
        q_dim: int,
        *,
        subject: str | None,
        kind: str | None,
        status: str | None,
        user_id: object,
    ) -> tuple[list[int], np.ndarray]:
        ids = [
            cid
            for cid, concept in self._concepts.items()
            if cid in self._vectors
            and self._vectors[cid].size == q_dim
            and (status is None or concept.status == status)
            and (subject is None or concept.subject == subject)
            and (kind is None or concept.kind == kind)
            and (user_id is _UNSET or concept.user_id == user_id)
        ]
        mat = (
            np.vstack([self._vectors[i] for i in ids])
            if ids
            else np.zeros((0, 0), dtype=np.float32)
        )
        return ids, mat

    # ── boot warm-start ───────────────────────────────────────────────

    def load_all(self) -> list[Concept]:
        """Load every concept into the in-process mirror and return them.

        Called once at boot so the cosine mirror is warm without touching
        the embedder (mirrors ``TopicClusterStore.load_all``). Edges are
        queried on demand rather than fully mirrored.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        self._concepts.clear()
        self._vectors.clear()
        self._active_dirty = True
        try:
            rows = conn.execute(
                f"SELECT {_CONCEPT_COLS} FROM concepts"
            ).fetchall()
        except Exception:
            log.warning("concepts load failed", exc_info=True)
            rows = []
        for r in rows:
            concept = self._row_to_concept(r)
            self._put_mirror(concept)
        return list(self._concepts.values())

    @staticmethod
    def _row_to_concept(r: tuple) -> Concept:
        return Concept(
            concept_id=int(r[0]),
            label=str(r[1] or ""),
            kind=str(r[2] or "identity"),
            subject=str(r[3] or "user"),
            user_id=(str(r[4]) if r[4] is not None else None),
            evidence_model=str(r[5] or "set"),
            status=str(r[6] or "candidate"),
            confidence=float(r[7] or 0.0),
            plasticity=float(r[8] or 0.0),
            evidence_count=int(r[9] or 0),
            distinct_source_count=int(r[10] or 0),
            rationale=str(r[11] or ""),
            embedding=_decode_embedding(r[12], int(r[13] or 0)),
            origin_session=(str(r[14]) if r[14] is not None else None),
            first_evidence_at=(str(r[15]) if r[15] is not None else None),
            created_at=str(r[16] or ""),
            updated_at=str(r[17] or ""),
            last_reinforced_at=(str(r[18]) if r[18] is not None else None),
            promoted_at=(str(r[19]) if r[19] is not None else None),
            last_lifecycle_at=(str(r[20]) if r[20] is not None else None),
            last_lifecycle_engagement=(
                float(r[21]) if r[21] is not None else None
            ),
            first_evidence_engagement=(
                float(r[22]) if r[22] is not None else None
            ),
        )

    # ── concept reads ─────────────────────────────────────────────────

    def get(self, concept_id: int) -> Concept | None:
        return self._concepts.get(int(concept_id))

    def all(self) -> list[Concept]:
        return list(self._concepts.values())

    def list_by(
        self,
        *,
        status: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
        user_id: object = _UNSET,
    ) -> list[Concept]:
        return [
            c
            for c in self._concepts.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
            and (user_id is _UNSET or c.user_id == user_id)
        ]

    def count(self) -> int:
        return len(self._concepts)

    def list_stalest(self, limit: int) -> list[Concept]:
        """Return up to ``limit`` concepts ordered by ``last_lifecycle_at``
        ascending, NULLs first -- i.e. never-evaluated / most-overdue
        concepts first. This is the L3 rolling round-robin fetch: over
        successive ticks it sweeps the whole (small, mirrored) set without
        a persisted cursor. Ties broken by ``concept_id`` for determinism.
        """
        if limit <= 0:
            return []
        concepts = list(self._concepts.values())
        # NULL last_lifecycle_at sorts before any timestamp; then oldest
        # timestamp; then by id.
        concepts.sort(
            key=lambda c: (
                0 if not c.last_lifecycle_at else 1,
                c.last_lifecycle_at or "",
                c.concept_id,
            )
        )
        return concepts[: int(limit)]

    def matrix_snapshot(
        self, concept_ids: "Sequence[int] | None" = None
    ) -> tuple[list[int], "np.ndarray"]:
        """One stacked unit-vector matrix for a caller-chosen id set.

        The single primitive for callers that need to compare *many*
        concepts against many others in one shot -- notably the L17
        drift worker's succession pass, which pairs faded beliefs against
        rising ones.

        :meth:`nearest` is the wrong tool for that: it serves only the
        plain ``status='active'`` query from the cached matrix, so any
        cross-status scan falls through to ``_filtered_matrix`` and
        restacks a fresh NumPy array *per call*. Doing that in a loop is
        the pattern behind the access violation that took down the
        consolidation worker's ``demand()`` probe. Callers stack once,
        here, and do a single matmul.

        Ids with no embedding, or whose embedding is a minority
        dimension, are dropped -- the returned id list is authoritative
        for the returned rows.
        """
        wanted = (
            [int(c) for c in concept_ids]
            if concept_ids is not None
            else list(self._concepts.keys())
        )
        vectors = [
            (cid, self._vectors[cid])
            for cid in wanted
            if cid in self._vectors and self._vectors[cid].size
        ]
        if not vectors:
            return [], np.zeros((0, 0), dtype=np.float32)
        dims: dict[int, int] = {}
        for _cid, vec in vectors:
            dims[vec.size] = dims.get(vec.size, 0) + 1
        dim = max(dims, key=lambda d: dims[d])
        kept = [(cid, vec) for cid, vec in vectors if vec.size == dim]
        if not kept:
            return [], np.zeros((0, 0), dtype=np.float32)
        ids = [cid for cid, _vec in kept]
        mat = np.vstack([vec for _cid, vec in kept])
        return ids, mat

    def nearest(
        self,
        query_vec: "np.ndarray",
        *,
        subject: str | None = None,
        kind: str | None = None,
        status: str | None = "active",
        user_id: object = _UNSET,
        k: int = 8,
    ) -> list[tuple[Concept, float]]:
        """Return up to ``k`` ``(concept, cosine)`` pairs nearest to
        ``query_vec``, filtered by the given axes.

        This is the single retrieval primitive every consumer uses. The
        common ``status='active'`` / no-other-filter path is served from
        the cached stacked matrix; filtered queries stack on demand
        (cheap at concept scale).
        """
        q = _unit(query_vec)
        if q.size == 0 or k <= 0:
            return []
        use_cache = (
            status == "active"
            and subject is None
            and kind is None
            and user_id is _UNSET
        )
        if use_cache:
            self._ensure_active_cache()
            if self._active_ids and q.size == self._active_dim:
                ids, mat = self._active_ids, self._active_mat
            else:
                ids, mat = self._filtered_matrix(
                    q.size, subject=subject, kind=kind,
                    status=status, user_id=user_id,
                )
        else:
            ids, mat = self._filtered_matrix(
                q.size, subject=subject, kind=kind,
                status=status, user_id=user_id,
            )
        if not ids or mat.size == 0:
            return []
        sims = mat @ q
        order = np.argsort(-sims)[: min(k, len(ids))]
        return [(self._concepts[ids[i]], float(sims[i])) for i in order]

    # ── concept writes ────────────────────────────────────────────────
    # Only the L3 lifecycle engine should mutate confidence / plasticity
    # / status; the store enforces no policy but keeps that discipline.

    def add(self, concept: Concept) -> int:
        """Insert a new concept, populate its ``concept_id``, mirror it,
        and return the id."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        now = _now_iso()
        concept.created_at = concept.created_at or now
        concept.updated_at = now
        blob, dim = _encode_embedding(concept.embedding)
        cursor = conn.execute(
            "INSERT INTO concepts "
            "(label, kind, subject, user_id, evidence_model, status, "
            " confidence, plasticity, evidence_count, distinct_source_count, "
            " rationale, embedding, dim, origin_session, first_evidence_at, "
            " created_at, updated_at, last_reinforced_at, promoted_at, "
            " last_lifecycle_at, last_lifecycle_engagement, "
            " first_evidence_engagement) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (
                str(concept.label),
                str(concept.kind),
                str(concept.subject),
                concept.user_id,
                str(concept.evidence_model),
                str(concept.status),
                float(concept.confidence),
                float(concept.plasticity),
                int(concept.evidence_count),
                int(concept.distinct_source_count),
                str(concept.rationale or ""),
                blob,
                dim,
                concept.origin_session,
                concept.first_evidence_at,
                concept.created_at,
                concept.updated_at,
                concept.last_reinforced_at,
                concept.promoted_at,
                concept.last_lifecycle_at,
                (
                    float(concept.last_lifecycle_engagement)
                    if concept.last_lifecycle_engagement is not None
                    else None
                ),
                (
                    float(concept.first_evidence_engagement)
                    if concept.first_evidence_engagement is not None
                    else None
                ),
            ),
        )
        conn.commit()
        concept.concept_id = int(cursor.lastrowid or 0)
        self._put_mirror(concept)
        return concept.concept_id

    def update(self, concept: Concept) -> None:
        """Persist every mutable field of ``concept`` (by id) and refresh
        the mirror. Bumps ``updated_at``."""
        if not concept.concept_id:
            raise ValueError("update requires a persisted concept_id")
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        concept.updated_at = _now_iso()
        blob, dim = _encode_embedding(concept.embedding)
        try:
            conn.execute(
                "UPDATE concepts SET "
                "  label = ?, kind = ?, subject = ?, user_id = ?, "
                "  evidence_model = ?, status = ?, confidence = ?, "
                "  plasticity = ?, evidence_count = ?, "
                "  distinct_source_count = ?, rationale = ?, embedding = ?, "
                "  dim = ?, origin_session = ?, first_evidence_at = ?, "
                "  updated_at = ?, last_reinforced_at = ?, promoted_at = ?, "
                "  last_lifecycle_at = ?, last_lifecycle_engagement = ?, "
                "  first_evidence_engagement = ? "
                "WHERE id = ?",
                (
                    str(concept.label),
                    str(concept.kind),
                    str(concept.subject),
                    concept.user_id,
                    str(concept.evidence_model),
                    str(concept.status),
                    float(concept.confidence),
                    float(concept.plasticity),
                    int(concept.evidence_count),
                    int(concept.distinct_source_count),
                    str(concept.rationale or ""),
                    blob,
                    dim,
                    concept.origin_session,
                    concept.first_evidence_at,
                    concept.updated_at,
                    concept.last_reinforced_at,
                    concept.promoted_at,
                    concept.last_lifecycle_at,
                    (
                        float(concept.last_lifecycle_engagement)
                        if concept.last_lifecycle_engagement is not None
                        else None
                    ),
                    (
                        float(concept.first_evidence_engagement)
                        if concept.first_evidence_engagement is not None
                        else None
                    ),
                    int(concept.concept_id),
                ),
            )
            conn.commit()
        except Exception:
            log.warning(
                "concept update failed (id=%s)", concept.concept_id,
                exc_info=True,
            )
            return
        self._put_mirror(concept)

    def set_alias_sink(
        self, sink: "Callable[[dict[str, object]], None] | None"
    ) -> None:
        """Attach the L17c absorption recorder (see :meth:`merge_into`)."""
        self._alias_sink = sink

    def _record_alias(self, *, canonical_id: int, absorbed: Concept) -> None:
        sink = self._alias_sink
        if sink is None:
            return
        try:
            sink(
                {
                    "absorbed_id": int(absorbed.concept_id),
                    "canonical_id": int(canonical_id),
                    "absorbed_label": str(absorbed.label or ""),
                    "kind": str(absorbed.kind or ""),
                    "subject": str(absorbed.subject or ""),
                }
            )
        except Exception:
            log.warning("concept alias record failed", exc_info=True)

    def delete(self, concept_id: int) -> None:
        """Delete a concept and every edge touching it (as concept node),
        then drop it from the mirror."""
        cid = int(concept_id)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "DELETE FROM concept_edges WHERE "
                "(src_type = 'concept' AND src_id = ?) OR "
                "(dst_type = 'concept' AND dst_id = ?)",
                (str(cid), str(cid)),
            )
            conn.execute("DELETE FROM concepts WHERE id = ?", (cid,))
            conn.commit()
        except Exception:
            log.warning("concept delete failed (id=%s)", cid, exc_info=True)
            return
        self._drop_mirror(cid)

    def _tension_parents(self, concept_id: int) -> set[int]:
        """The ids of the ``tension`` meta concepts this concept is a *base*
        of. A tension base points at its meta via a concept->concept
        ``evidence`` edge, so this walks the outgoing evidence edges and keeps
        those whose destination resolves to an active-or-not ``tension``
        concept. Used by :meth:`merge_into` to refuse fusing two co-bases of
        the same tension."""
        out: set[int] = set()
        for e in self.edges_from("concept", int(concept_id)):
            if e.dst_type != "concept" or e.relation != "evidence":
                continue
            try:
                meta_id = int(e.dst_id)
            except (TypeError, ValueError):
                continue
            meta = self.get(meta_id)
            if meta is not None and meta.kind == "tension":
                out.add(meta_id)
        return out

    def merge_into(self, *, canonical_id: int, absorbed_id: int) -> bool:
        """Fuse ``absorbed`` into ``canonical`` (L2 near-duplicate
        consolidation) and delete the absorbed row. Structural /
        evidence-only: this re-points every edge touching
        ``concept:absorbed`` onto ``concept:canonical`` (``add_edge``
        upserts on the unique key, so a collision with an edge the
        canonical already owns merges rather than duplicates), recomputes
        the canonical's ``distinct_source_count`` / ``evidence_count`` from
        the union of its surviving evidence edges, and bumps
        ``last_reinforced_at`` so the L3 lifecycle engine re-derives
        confidence on its next tick.

        Deliberately does **not** touch the canonical's
        ``confidence`` / ``plasticity`` / ``status`` -- that stays the
        single-writer L3 engine's job. Callers therefore pick the
        stronger row as ``canonical`` so nothing is lost.

        Returns ``True`` on a completed merge, ``False`` when refused
        (missing row, same id, differing ``(subject, kind)``, or a direct
        conflict edge between the two).
        """
        can_id = int(canonical_id)
        abs_id = int(absorbed_id)
        if can_id == abs_id:
            return False
        canonical = self.get(can_id)
        absorbed = self.get(abs_id)
        if canonical is None or absorbed is None:
            return False
        if (
            canonical.subject != absorbed.subject
            or canonical.kind != absorbed.kind
        ):
            return False
        # Refuse to collapse two concepts held in explicit friction with each
        # other -- that is L12 tension territory, not a dup. Tension bases link
        # to their meta via a concept->concept ``evidence`` edge (not a direct
        # ``tension`` edge between the bases), so two concepts that are
        # co-bases of the *same* tension meta are in explicit friction: merging
        # them would collapse a tension onto itself.
        if self._tension_parents(abs_id) & self._tension_parents(can_id):
            return False
        # Also refuse on a direct concept->concept friction/disproof edge
        # between the two. None are written today (contradicts is
        # concept->memory), but this future-proofs L18d / L20 relations.
        conflict = {"tension", "contradicts"}
        for e in self.edges_from("concept", abs_id):
            if e.dst_type == "concept" and str(e.dst_id) == str(can_id) and (
                e.relation in conflict
            ):
                return False
        for e in self.edges_into("concept", abs_id):
            if e.src_type == "concept" and str(e.src_id) == str(can_id) and (
                e.relation in conflict
            ):
                return False

        can_s = str(can_id)
        # Re-point edges whose *source* is the absorbed concept (things
        # that depend on it: metas / generalizations), skipping any edge
        # that would become a self-loop on the canonical.
        for e in self.edges_from("concept", abs_id):
            if e.dst_type == "concept" and str(e.dst_id) == can_s:
                continue
            e.src_id = can_s
            e.edge_id = 0
            self.add_edge(e)
        # Re-point edges whose *destination* is the absorbed concept (its
        # supporting evidence / bases).
        for e in self.edges_into("concept", abs_id):
            if e.src_type == "concept" and str(e.src_id) == can_s:
                continue
            e.dst_id = can_s
            e.edge_id = 0
            self.add_edge(e)

        # L17c: record the absorption *before* the delete -- this is the
        # last moment the absorbed row's label and identity exist. Without
        # it the id becomes a dead end and every trajectory ending in this
        # merge is unreachable.
        self._record_alias(canonical_id=can_id, absorbed=absorbed)

        # Drop the absorbed row (also clears its now-orphaned edges + the
        # mirror entry).
        self.delete(abs_id)

        # Recompute the canonical's evidence tallies from the surviving
        # (re-pointed + deduped) evidence edges -- honest structural counts
        # rather than a naive sum that would double-count shared sources.
        evidence = self.evidence_of(can_id)
        sources = {(e.src_type, str(e.src_id)) for e in evidence}
        canonical.evidence_count = len(evidence)
        canonical.distinct_source_count = len(sources)
        canonical.last_reinforced_at = _now_iso()
        self.update(canonical)
        return True

    # ── edge writes ───────────────────────────────────────────────────

    def add_edge(self, edge: ConceptEdge) -> int:
        """Insert (or update, on the unique key) one influence edge and
        return its row id."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        edge.created_at = edge.created_at or _now_iso()
        try:
            conn.execute(
                "INSERT INTO concept_edges "
                "(src_type, src_id, dst_type, dst_id, relation, polarity, "
                " strength, ordinal, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(src_type, src_id, dst_type, dst_id, relation) "
                "DO UPDATE SET polarity = excluded.polarity, "
                "  strength = excluded.strength, ordinal = excluded.ordinal",
                (
                    str(edge.src_type),
                    str(edge.src_id),
                    str(edge.dst_type),
                    str(edge.dst_id),
                    str(edge.relation),
                    int(edge.polarity),
                    float(edge.strength),
                    (int(edge.ordinal) if edge.ordinal is not None else None),
                    edge.created_at,
                ),
            )
            conn.commit()
        except Exception:
            log.warning("add_edge failed", exc_info=True)
            return 0
        row = conn.execute(
            "SELECT id FROM concept_edges WHERE src_type = ? AND src_id = ? "
            "AND dst_type = ? AND dst_id = ? AND relation = ?",
            (
                str(edge.src_type), str(edge.src_id),
                str(edge.dst_type), str(edge.dst_id), str(edge.relation),
            ),
        ).fetchone()
        edge.edge_id = int(row[0]) if row else 0
        return edge.edge_id

    # ── edge reads ────────────────────────────────────────────────────

    @staticmethod
    def _row_to_edge(r: tuple) -> ConceptEdge:
        return ConceptEdge(
            edge_id=int(r[0]),
            src_type=str(r[1]),
            src_id=str(r[2]),
            dst_type=str(r[3]),
            dst_id=str(r[4]),
            relation=str(r[5]),
            polarity=int(r[6]),
            strength=float(r[7]),
            ordinal=(int(r[8]) if r[8] is not None else None),
            created_at=str(r[9] or ""),
        )

    def _edge_rows(self, where: str, params: tuple) -> list[ConceptEdge]:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT id, src_type, src_id, dst_type, dst_id, relation, "
                "polarity, strength, ordinal, created_at FROM concept_edges "
                f"WHERE {where}",
                params,
            ).fetchall()
        except Exception:
            log.warning("edge query failed", exc_info=True)
            return []
        return [self._row_to_edge(r) for r in rows]

    def edges_from(self, node_type: str, node_id: object) -> list[ConceptEdge]:
        """Edges whose *source* is this node (things that depend on it)."""
        return self._edge_rows(
            "src_type = ? AND src_id = ?", (str(node_type), str(node_id))
        )

    def edges_into(self, node_type: str, node_id: object) -> list[ConceptEdge]:
        """Edges whose *destination* is this node (its supporting
        evidence / bases)."""
        return self._edge_rows(
            "dst_type = ? AND dst_id = ?", (str(node_type), str(node_id))
        )

    def evidence_of(self, concept_id: int) -> list[ConceptEdge]:
        """The ``evidence`` edges supporting a concept
        (memory|cluster -> concept), ordered by ``ordinal`` for
        memory-chain kinds."""
        edges = [
            e
            for e in self.edges_into("concept", int(concept_id))
            if e.relation == "evidence"
        ]
        edges.sort(
            key=lambda e: (e.ordinal if e.ordinal is not None else 1 << 30)
        )
        return edges

    def dependents_of(self, concept_id: int) -> list[int]:
        """Concept ids that *depend on* this concept -- i.e. metas that
        reference it as a base. Walks ``src -> dst`` per the edge
        direction convention, so a base's status change can cascade to
        its dependents (L1 rule 2)."""
        out: list[int] = []
        for e in self.edges_from("concept", int(concept_id)):
            if e.dst_type == "concept":
                try:
                    out.append(int(e.dst_id))
                except ValueError:
                    continue
        return out

    # ── cascade hooks ─────────────────────────────────────────────────

    def delete_edge(self, edge_id: int) -> None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "DELETE FROM concept_edges WHERE id = ?", (int(edge_id),)
            )
            conn.commit()
        except Exception:
            log.warning("delete_edge failed (id=%s)", edge_id, exc_info=True)

    def delete_edges_for_node(self, node_type: str, node_id: object) -> None:
        """Drop every edge touching a node on either side."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "DELETE FROM concept_edges WHERE "
                "(src_type = ? AND src_id = ?) OR (dst_type = ? AND dst_id = ?)",
                (str(node_type), str(node_id), str(node_type), str(node_id)),
            )
            conn.commit()
        except Exception:
            log.warning(
                "delete_edges_for_node failed (%s=%s)", node_type, node_id,
                exc_info=True,
            )

    def delete_for_memory(self, memory_id: int) -> None:
        """Cascade hook for ``MemoryStore.delete`` -- drop every edge that
        touched a now-deleted memory (``evidence`` edges pointing *from* it
        and ``contradicts`` edges pointing *at* it). Reconciling the
        affected concepts' evidence counts is the L25
        :class:`~app.core.concepts.concept_edge_reconciler.ConceptEdgeReconciler`'s
        job, not done here (the store stays mechanism-only)."""
        self.delete_edges_for_node("memory", int(memory_id))

    def affected_concepts_for_memory(self, memory_id: int) -> set[int]:
        """Concept ids with any edge touching this memory, on either side:
        ``evidence`` edges (memory -> concept) and ``contradicts`` edges
        (concept -> memory). Used to recompute evidence counts after the
        memory's edges are dropped or repointed (L25)."""
        out: set[int] = set()
        for e in self.edges_from("memory", memory_id):
            if e.dst_type == "concept":
                try:
                    out.add(int(e.dst_id))
                except (TypeError, ValueError):
                    continue
        for e in self.edges_into("memory", memory_id):
            if e.src_type == "concept":
                try:
                    out.add(int(e.src_id))
                except (TypeError, ValueError):
                    continue
        return out

    def repoint_memory_edges(self, old_id: int, new_id: int) -> int:
        """Move every edge touching ``memory:old_id`` onto ``memory:new_id``
        (L25 rule (b): a destructively-merged evidence memory keeps
        supporting its concept via the survivor). Re-adds each edge at the
        new endpoint -- ``add_edge`` upserts on the unique key, so a
        collision with an edge the survivor already owns merges rather than
        duplicates -- then drops the old endpoint's edges. Returns the
        number of edges moved."""
        old = int(old_id)
        new = int(new_id)
        if old == new:
            return 0
        new_s = str(new)
        moved = 0
        for e in self.edges_from("memory", old):
            e.src_id = new_s
            e.edge_id = 0
            self.add_edge(e)
            moved += 1
        for e in self.edges_into("memory", old):
            e.dst_id = new_s
            e.edge_id = 0
            self.add_edge(e)
            moved += 1
        if moved:
            self.delete_edges_for_node("memory", old)
        return moved

    def orphaned_memory_edges(self, limit: int = 200) -> list[ConceptEdge]:
        """Edges whose ``memory`` endpoint no longer has a surviving row in
        ``memories`` -- the defence-in-depth catch for deletes that skip the
        delete-listener path (notably ``MemoryStore.prune`` batch deletes).
        Bounded by ``limit`` so the L25 integrity sweep stays a small,
        rolling job."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT id, src_type, src_id, dst_type, dst_id, relation, "
                "polarity, strength, ordinal, created_at FROM concept_edges "
                "WHERE (src_type = 'memory' AND CAST(src_id AS INTEGER) "
                "       NOT IN (SELECT id FROM memories)) "
                "   OR (dst_type = 'memory' AND CAST(dst_id AS INTEGER) "
                "       NOT IN (SELECT id FROM memories)) "
                "LIMIT ?",
                (int(limit),),
            ).fetchall()
        except Exception:
            log.warning("orphaned_memory_edges query failed", exc_info=True)
            return []
        return [self._row_to_edge(r) for r in rows]


__all__ = ["Concept", "ConceptEdge", "ConceptStore"]

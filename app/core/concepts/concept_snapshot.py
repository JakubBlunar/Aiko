"""JSON snapshot of the concept layer for the debug UI + MCP.

Pure builder (no I/O beyond reading the in-process ``ConceptStore`` mirror
+ the memory/topic-graph mirrors), so it is unit-testable without a full
:class:`SessionController`. Models
:func:`app.core.conversation.topic_graph.build_topic_graph_snapshot`.

Two deliberate differences from the topic-graph snapshot:

- **No truncation.** Labels, rationale, and evidence text are sent in
  full; the debug panel wraps them. Truncating here would make the UI
  look as if the stored value were clipped.
- **Resolved evidence.** Each evidence edge is joined to a human-readable
  label -- the memory's content, the topic cluster's summary, or the
  referenced concept's label -- so the panel shows *what* a concept rests
  on, not just opaque ``memory:42`` ids.

Because nothing is truncated, the snapshot is **paged** rather than
trimmed: a graph of ~800 concepts resolves ~3.3k evidence edges into
~1.5 MB of JSON, which locks up a phone both in transit and in the
render. ``limit`` / ``offset`` keep the no-truncation promise for what
is returned while bounding how much that is, and filtering + slicing
happen *before* evidence resolution so a page costs a page's worth of
joins instead of the whole graph's. ``counts`` is always tallied over
the full store, so the filter pills and totals stay honest on any page.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.concepts.concept_importance import memory_ids_from_edges
from app.core.infra.text_query import compile_query

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_quality import (
        EvidenceFacts,
        QualityThresholds,
    )
    from app.core.concepts.concept_store import (
        Concept,
        ConceptEdge,
        ConceptStore,
    )
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.concept_snapshot")


def _disabled() -> dict[str, Any]:
    return {
        "enabled": False,
        "total": 0,
        "matched": 0,
        "offset": 0,
        "limit": 0,
        "counts": {"by_status": {}, "by_subject": {}, "by_kind": {}},
        "concepts": [],
    }


def _cluster_label_map(topic_graph: "TopicGraph | None") -> dict[str, str]:
    """Map each cluster's stable representative-member id -> its summary,
    so ``cluster`` evidence edges (keyed by rep id) resolve to a label."""
    labels: dict[str, str] = {}
    if topic_graph is None:
        return labels
    try:
        for cluster in topic_graph.topic_clusters():
            labels[str(cluster.representative_id)] = cluster.summary or ""
    except Exception:
        return labels
    return labels


def _resolve_label(
    edge: "ConceptEdge",
    memory_store: "MemoryStore | None",
    cluster_labels: dict[str, str],
    store: "ConceptStore",
) -> str:
    """Human-readable label for one evidence node (full text, untrimmed)."""
    try:
        if edge.src_type == "memory":
            if memory_store is None:
                return ""
            mem = memory_store.get(int(edge.src_id))
            return (getattr(mem, "content", "") or "") if mem else ""
        if edge.src_type == "cluster":
            return cluster_labels.get(str(edge.src_id), "")
        if edge.src_type == "concept":
            other = store.get(int(edge.src_id))
            return other.label if other else ""
    except (TypeError, ValueError):
        return ""
    return ""


def resolve_evidence_labels(
    store: "ConceptStore",
    memory_store: "MemoryStore | None",
    topic_graph: "TopicGraph | None",
    concept_id: int,
    *,
    limit: int | None = None,
    src_types: "tuple[str, ...] | None" = None,
) -> list[str]:
    """Human-readable, non-empty labels for a concept's evidence edges.

    The shared seam used by both the debug snapshot and the L5 concept
    block (living-belief "supporting grounding") so evidence-node
    resolution -- memory content / cluster summary / concept label -- has
    a single implementation. Ordered by ``evidence_of`` (ordinal), blanks
    dropped, capped at ``limit`` when given.

    ``src_types`` restricts which evidence node types contribute. The
    L5 grounding clause passes ``("cluster", "concept")`` so a concept is
    grounded on the *themes* it recurs around (topic clusters / related
    concepts) rather than a raw memory sentence -- the latter is a full
    first-person statement that reads as a truncated fragment once trimmed
    for the prompt (notably ``subject=aiko`` self-concepts, whose evidence
    is memory-typed). ``None`` (default) keeps every node type.
    """
    cluster_labels = _cluster_label_map(topic_graph)
    out: list[str] = []
    for edge in store.evidence_of(int(concept_id)):
        if src_types is not None and edge.src_type not in src_types:
            continue
        label = (
            _resolve_label(edge, memory_store, cluster_labels, store) or ""
        ).strip()
        if not label:
            continue
        out.append(label)
        if limit is not None and len(out) >= int(limit):
            break
    return out


def build_concepts_snapshot(
    store: "ConceptStore | None",
    memory_store: "MemoryStore | None",
    topic_graph: "TopicGraph | None",
    *,
    limit: int | None = None,
    offset: int = 0,
    status: str | None = None,
    subject: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    kv_get: "Callable[[str], str | None] | None" = None,
) -> dict[str, Any]:
    """Serialise one page of the concept layer for ``GET /api/concepts``.

    ``status`` / ``subject`` / ``kind`` narrow the page and ``q`` searches
    label + rationale (see :mod:`app.core.infra.text_query`); ``limit`` /
    ``offset`` cut it. ``limit=None`` returns everything from ``offset``
    on, which is the whole graph by default -- kept as the default so an
    unparameterised call still means "the whole snapshot".

    The ``counts`` tallies describe the **whole store**, not the filtered
    set, because they are what the UI builds its filter pills from: a
    count that narrowed as you filtered would leave you unable to
    navigate back out of an empty selection.

    ``kv_get`` enables the L32 ``importance`` axis, which needs the
    per-cluster affect maps out of ``kv_meta``. Omitting it drops the
    three importance fields rather than failing -- they are a debug lens,
    not part of the contract.

    Returns an empty-but-valid ``enabled=False`` shape when the store is
    absent (``concepts_enabled`` off or init failed) so callers never
    special-case the disabled path.
    """
    if store is None:
        return _disabled()

    by_status: dict[str, int] = {}
    by_subject: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    matched: list["Concept"] = []

    kind_norm = (kind or "").strip().lower() or None
    query = compile_query(q)

    # One pass for the tallies -- they describe the whole store, not the
    # page, so the filter pills show real counts however you are paging.
    for c in store.all():
        by_status[c.status] = by_status.get(c.status, 0) + 1
        by_subject[c.subject] = by_subject.get(c.subject, 0) + 1
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
        if status is not None and c.status != status:
            continue
        if subject is not None and c.subject != subject:
            continue
        if kind_norm is not None and c.kind != kind_norm:
            continue
        # Label and rationale only. Evidence labels are deliberately out
        # of scope: resolving them is the expensive half of this function
        # and runs for one page, so searching them would mean paying that
        # cost across the whole store on every keystroke.
        if query is not None and not query.matches(c.label, c.rationale):
            continue
        matched.append(c)

    # Most-supported / most-confident on top. ``concept_id`` breaks ties
    # so a row cannot swap pages between two requests that see the same
    # confidence -- without it, paging can show or skip a duplicate.
    matched.sort(
        key=lambda c: (
            float(c.confidence),
            int(c.evidence_count),
            int(c.concept_id),
        ),
        reverse=True,
    )

    start = max(0, int(offset))
    if limit is None:
        page = matched[start:]
    else:
        page = matched[start:start + max(0, int(limit))]

    # Evidence resolution is the expensive half (a join per edge), so it
    # runs only for the rows actually being returned.
    cluster_labels = _cluster_label_map(topic_graph)
    # L32 importance rides along free: the edges it needs to find a
    # concept's grounding clusters are the same ones the loop below
    # resolves for display, so they are read once and used twice.
    page_edges = {c.concept_id: store.evidence_of(c.concept_id) for c in page}
    importance = importance_context_for(
        {
            cid: memory_ids_from_edges(edges)
            for cid, edges in page_edges.items()
        },
        topic_graph,
        kv_get,
    )
    concepts_out: list[dict[str, Any]] = []
    for c in page:
        evidence: list[dict[str, Any]] = []
        for e in page_edges.get(c.concept_id, ()):
            evidence.append({
                "src_type": e.src_type,
                "src_id": e.src_id,
                "relation": e.relation,
                "polarity": e.polarity,
                "strength": e.strength,
                "ordinal": e.ordinal,
                "label": _resolve_label(e, memory_store, cluster_labels, store),
            })

        embedding = getattr(c, "embedding", None)
        dim = int(embedding.size) if embedding is not None else 0

        row: dict[str, Any] = {
            "id": int(c.concept_id),
            "label": c.label,
            "kind": c.kind,
            "subject": c.subject,
            "evidence_model": c.evidence_model,
            "status": c.status,
            "confidence": float(c.confidence),
            "plasticity": float(c.plasticity),
            "evidence_count": int(c.evidence_count),
            "distinct_source_count": int(c.distinct_source_count),
            "rationale": c.rationale,
            "created_at": c.created_at,
            "first_evidence_at": c.first_evidence_at,
            "last_reinforced_at": c.last_reinforced_at,
            "promoted_at": c.promoted_at,
            "dim": dim,
            "evidence": evidence,
        }
        if importance is not None:
            detail = importance.detail(c)
            # Kept as three fields, not one: a reader needs to see whether a
            # high number came from the kind's stake or from the emotional
            # charge of the topics underneath it.
            row["importance"] = round(detail.importance, 4)
            row["importance_prior"] = round(detail.prior, 4)
            row["importance_charge"] = round(detail.charge, 4)
        concepts_out.append(row)

    return {
        "enabled": True,
        # ``total`` stays the whole-store count it has always been;
        # ``matched`` is what the current filter selects, and is what
        # paging divides.
        "total": sum(by_status.values()),
        "matched": len(matched),
        "offset": start,
        "limit": len(concepts_out) if limit is None else int(limit),
        "counts": {
            "by_status": by_status,
            "by_subject": by_subject,
            "by_kind": by_kind,
        },
        "concepts": concepts_out,
    }


def importance_context_for(
    memory_ids_by_concept: dict[int, tuple[int, ...]],
    topic_graph: "TopicGraph | None",
    kv_get: "Callable[[str], str | None] | None",
):
    """An :class:`ImportanceContext` over a set of concepts, or ``None``.

    Takes the already-resolved cluster-evidence ids so neither caller pays
    for a second edge read: the snapshot reuses the edges its display loop
    fetched, the quality report does one bulk query for the whole graph.
    Returns ``None`` without a ``kv_get`` (nothing to read affect from) or
    on any failure -- importance is a lens on the report, never a reason
    for the report to fail.
    """
    if kv_get is None:
        return None
    try:
        from app.core.concepts.cluster_affect import (
            KV_CLUSTER_AFFECT_AIKO,
            KV_CLUSTER_AFFECT_USER,
            load_map,
        )
        from app.core.concepts.concept_importance import (
            ImportanceContext,
            cluster_membership,
        )

        return ImportanceContext(
            affect_user=load_map(kv_get, KV_CLUSTER_AFFECT_USER),
            affect_aiko=load_map(kv_get, KV_CLUSTER_AFFECT_AIKO),
            cluster_by_memory=cluster_membership(topic_graph),
            memory_ids_by_concept=memory_ids_by_concept,
        )
    except Exception:
        log.debug("importance context build failed", exc_info=True)
        return None


# ── L22 quality report ────────────────────────────────────────────────


def _memory_cluster_map(topic_graph: "TopicGraph | None") -> dict[int, str]:
    """Map every clustered memory id -> its cluster's representative id.

    The inverse of ``_cluster_label_map``, and the join L22 signal A
    needs: ``memory`` evidence edges name a memory, not the cluster it
    belongs to, so without this there is no way to tell three memories
    from one topic apart from three memories spanning three.
    """
    out: dict[int, str] = {}
    if topic_graph is None:
        return out
    try:
        for cluster in topic_graph.topic_clusters():
            rep = str(cluster.representative_id)
            for mid in cluster.member_ids:
                out[int(mid)] = rep
    except Exception:
        return out
    return out


def resolve_evidence_facts(
    store: "ConceptStore",
    memory_store: "MemoryStore | None",
    topic_graph: "TopicGraph | None",
) -> dict[int, "EvidenceFacts"]:
    """Resolve the L22 A/B signals for every concept.

    The graph-join half of the quality report, kept here because it needs
    the edge table, the topic graph and the memory mirror -- none of
    which the pure scorer is allowed to touch.

    - **Cluster span (A)**: ``cluster`` edges contribute their own rep id;
      ``memory`` edges resolve through the cluster map. ``concept`` edges
      are skipped, since meta concepts are grounded on other concepts
      rather than on topics and would otherwise read as span-0.
    - **Memory confidence (B)**: the confidence of each supporting memory,
      so a belief resting on shaky recall can be told apart from one
      resting on firm recall.
    """
    from app.core.concepts.concept_quality import EvidenceFacts

    cluster_of_memory = _memory_cluster_map(topic_graph)
    facts: dict[int, EvidenceFacts] = {}

    for concept in store.all():
        reps: set[str] = set()
        confidences: list[float] = []
        for edge in store.evidence_of(concept.concept_id):
            try:
                if edge.src_type == "cluster":
                    reps.add(str(edge.src_id))
                elif edge.src_type == "memory":
                    mid = int(edge.src_id)
                    rep = cluster_of_memory.get(mid)
                    if rep is not None:
                        reps.add(rep)
                    if memory_store is not None:
                        mem = memory_store.get(mid)
                        if mem is not None:
                            confidences.append(
                                float(getattr(mem, "confidence", 0.7))
                            )
            except (TypeError, ValueError):
                continue
        facts[int(concept.concept_id)] = EvidenceFacts(
            cluster_span=len(reps),
            memory_confidences=tuple(confidences),
        )
    return facts


def build_concept_quality(
    store: "ConceptStore | None",
    memory_store: "MemoryStore | None",
    topic_graph: "TopicGraph | None",
    event_store: "ConceptEventStore | None" = None,
    *,
    thresholds: "QualityThresholds | None" = None,
    kv_get: "Callable[[str], str | None] | None" = None,
) -> dict[str, Any]:
    """Build the L22 quality report for ``GET /api/concepts/quality``.

    Does the I/O -- load concepts, tally the event timeline, resolve the
    evidence joins -- then hands everything to the pure scorer in
    :mod:`app.core.concepts.concept_quality`. Returns the disabled shape
    when the store is absent, matching ``build_concepts_snapshot``.

    ``kv_get`` enables the L32 importance section, same as in
    :func:`build_concepts_snapshot`.
    """
    from app.core.concepts.concept_quality import (
        build_quality_report,
        disabled_quality_report,
    )

    if store is None:
        return disabled_quality_report()

    event_counts: dict[str, int] = {}
    if event_store is not None:
        try:
            event_counts = event_store.counts_by_type()
        except Exception:
            event_counts = {}

    rows = list(store.all())
    return build_quality_report(
        rows,
        event_counts=event_counts,
        evidence_facts=resolve_evidence_facts(store, memory_store, topic_graph),
        thresholds=thresholds,
        # The report scores the whole graph rather than a page, so it
        # takes the bulk edge read instead of the snapshot's reuse.
        importance=importance_context_for(
            store.cluster_evidence_for([c.concept_id for c in rows]),
            topic_graph,
            kv_get,
        ),
    )


__all__ = [
    "build_concept_quality",
    "build_concepts_snapshot",
    "importance_context_for",
    "resolve_evidence_facts",
    "resolve_evidence_labels",
]

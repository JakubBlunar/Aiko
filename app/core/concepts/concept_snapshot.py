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
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_quality import (
        EvidenceFacts,
        QualityThresholds,
    )
    from app.core.concepts.concept_store import ConceptEdge, ConceptStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import MemoryStore


def _disabled() -> dict[str, Any]:
    return {
        "enabled": False,
        "total": 0,
        "counts": {"by_status": {}, "by_subject": {}},
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
) -> dict[str, Any]:
    """Serialise the concept layer for ``GET /api/concepts``.

    Returns an empty-but-valid ``enabled=False`` shape when the store is
    absent (``concepts_enabled`` off or init failed) so callers never
    special-case the disabled path.
    """
    if store is None:
        return _disabled()

    cluster_labels = _cluster_label_map(topic_graph)
    by_status: dict[str, int] = {}
    by_subject: dict[str, int] = {}
    concepts_out: list[dict[str, Any]] = []

    for c in store.all():
        by_status[c.status] = by_status.get(c.status, 0) + 1
        by_subject[c.subject] = by_subject.get(c.subject, 0) + 1

        evidence: list[dict[str, Any]] = []
        for e in store.evidence_of(c.concept_id):
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

        concepts_out.append({
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
        })

    # Most-supported / most-confident on top; grouping is client-side.
    concepts_out.sort(
        key=lambda c: (c["confidence"], c["evidence_count"]), reverse=True
    )

    return {
        "enabled": True,
        "total": len(concepts_out),
        "counts": {"by_status": by_status, "by_subject": by_subject},
        "concepts": concepts_out,
    }


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
) -> dict[str, Any]:
    """Build the L22 quality report for ``GET /api/concepts/quality``.

    Does the I/O -- load concepts, tally the event timeline, resolve the
    evidence joins -- then hands everything to the pure scorer in
    :mod:`app.core.concepts.concept_quality`. Returns the disabled shape
    when the store is absent, matching ``build_concepts_snapshot``.
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

    return build_quality_report(
        store.all(),
        event_counts=event_counts,
        evidence_facts=resolve_evidence_facts(store, memory_store, topic_graph),
        thresholds=thresholds,
    )


__all__ = [
    "build_concept_quality",
    "build_concepts_snapshot",
    "resolve_evidence_facts",
    "resolve_evidence_labels",
]

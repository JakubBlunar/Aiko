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


__all__ = ["build_concepts_snapshot"]

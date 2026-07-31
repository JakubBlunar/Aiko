"""L25 concept<->memory edge referential integrity.

Concepts point at memories through ``concept_edges`` (``evidence``:
``memory -> concept``; ``contradicts``: ``concept -> memory``), but memories
are not permanent -- they're deleted, pruned, merged, archived, and
reclassified. This reconciler keeps the edge graph honest when a memory
moves or vanishes, so a concept never silently keeps dangling support (or
loses the support it was promoted on).

Three entry points, all pure SQL / arithmetic (no LLM):

- :meth:`on_memory_deleted` -- registered as a ``MemoryStore`` delete
  listener. When a memory is hard-deleted, drop every edge touching it and
  recompute the affected concepts' evidence counts so L3 can weaken /
  demote them when its rolling sweep next reaches them.
- :meth:`sweep` -- the defence-in-depth pass the L25 idle worker runs.
  ``MemoryStore.prune`` batch-deletes rows *without* firing delete
  listeners, so orphaned edges accumulate; the sweep garbage-collects any
  edge whose memory endpoint no longer exists and reconciles counts.
- :meth:`repoint` -- for a *destructive* merge (legacy Phase 4b
  consolidation hard-deletes the absorbed memory): move the victim's edges
  onto the surviving memory so a merged evidence memory keeps supporting
  its concept (rule (b)).

**Ownership.** ``evidence_count`` / ``distinct_source_count`` are treated as
*edge-derived*: any path that mutates edges recomputes them from the live
edge table (L2's reinforce does this; so does this reconciler). L3 remains
the single writer of ``confidence`` / ``plasticity`` / ``status`` -- this
reconciler never touches those.

That division is why the re-gate itself lives in
:meth:`ConceptLifecycleWorker._has_any_evidence` rather than here. This
reconciler only makes the counts truthful; L3's rolling sweep (which
reaches every concept within a handful of ticks) reads them and demotes an
active belief left with no evidence at all. For a long time nothing did
read them, which is how concepts ended up ``active`` on zero sources --
the status floors looked at confidence alone, so a belief whose support
had been deleted stayed active until decay caught up with it tens of
engaged days later.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.concepts.concept_store import ConceptStore

log = logging.getLogger("app.concept_edge_reconciler")


class ConceptEdgeReconciler:
    """Keeps concept<->memory edges and edge-derived counts consistent
    across the memory lifecycle (L25)."""

    def __init__(self, concept_store: "ConceptStore") -> None:
        self._store = concept_store

    # ── delete cascade (registered as a MemoryStore delete listener) ────

    def on_memory_deleted(self, memory_id: int) -> None:
        """Drop every edge touching a just-deleted memory and recompute the
        affected concepts' evidence counts. Safe to call for a memory with
        no edges (a cheap no-op)."""
        try:
            mid = int(memory_id)
        except (TypeError, ValueError):
            return
        affected = self._store.affected_concepts_for_memory(mid)
        if not affected:
            return
        self._store.delete_edges_for_node("memory", mid)
        for cid in affected:
            self._recount(cid)

    # ── idle integrity sweep (prune() bypasses delete listeners) ────────

    def sweep(self, limit: int = 200) -> dict[str, Any]:
        """Garbage-collect edges whose memory endpoint no longer exists and
        reconcile the affected concepts. Bounded by ``limit`` so it stays a
        small rolling job. Returns counters for logging."""
        orphans = self._store.orphaned_memory_edges(int(limit))
        stats = {"orphans_dropped": 0, "concepts_reconciled": 0}
        if not orphans:
            return stats
        affected: set[int] = set()
        for edge in orphans:
            for node_type, node_id in (
                (edge.src_type, edge.src_id),
                (edge.dst_type, edge.dst_id),
            ):
                if node_type == "concept":
                    try:
                        affected.add(int(node_id))
                    except (TypeError, ValueError):
                        continue
            self._store.delete_edge(edge.edge_id)
            stats["orphans_dropped"] += 1
        for cid in affected:
            self._recount(cid)
        stats["concepts_reconciled"] = len(affected)
        return stats

    # ── repoint (destructive merge: keep evidence on the survivor) ──────

    def repoint(self, old_memory_id: int, new_memory_id: int) -> int:
        """Move edges from a soon-to-be-deleted memory onto the surviving
        memory and reconcile both endpoints' concepts. Returns the number
        of edges moved."""
        try:
            old = int(old_memory_id)
            new = int(new_memory_id)
        except (TypeError, ValueError):
            return 0
        affected = self._store.affected_concepts_for_memory(
            old
        ) | self._store.affected_concepts_for_memory(new)
        moved = self._store.repoint_memory_edges(old, new)
        if moved:
            for cid in affected:
                self._recount(cid)
        return moved

    # ── internals ───────────────────────────────────────────────────────

    def _recount(self, concept_id: int) -> None:
        """Recompute a concept's edge-derived evidence counts from the live
        edge table and persist *only* those two fields. Never touches
        ``confidence`` / ``plasticity`` / ``status`` (L3's territory)."""
        concept = self._store.get(int(concept_id))
        if concept is None:
            return
        ev = self._store.evidence_of(int(concept_id))
        evidence_count = len(ev)
        distinct = len({(e.src_type, e.src_id) for e in ev})
        if (
            concept.evidence_count == evidence_count
            and concept.distinct_source_count == distinct
        ):
            return
        concept.evidence_count = evidence_count
        concept.distinct_source_count = distinct
        self._store.update(concept)


__all__ = ["ConceptEdgeReconciler"]

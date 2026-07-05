"""L24 concept read-facade -- the single interface every deriver/worker
uses to consume concepts.

Before L24 each consumer reached the :class:`ConceptStore` directly (via
``getattr(self, "_concept_store", None)``) and called ``nearest`` /
``list_by`` with its own filter tuple, and separately re-resolved evidence
labels / cluster summaries through :mod:`concept_snapshot` helpers. That
spread the "how do I read concepts?" knowledge across the identity pin
lane, ``recall_concept``, the self-image worker, and every future
deriver.

``ConceptView`` is the one documented read + resolution path. It bundles

- the :class:`ConceptStore` (required), and
- an optional ``topic_graph`` and ``memory_store``

so a worker takes **one** dependency and gets both "which concepts?"
(``core`` / ``relevant`` / ``for_target`` / ``for_cluster``) and "resolve
their grounding" (``evidence_labels``) without juggling three stores and
the snapshot helpers itself. Concepts are the upstream source of truth
(L24 stance): a deriver composes from active concepts and falls back to
its raw derivation only when the concept layer is sparse/immature.

Everything degrades gracefully:

- a missing store -> every method returns ``[]`` (a cold / disabled
  install never crashes a consumer),
- a missing ``topic_graph`` / ``memory_store`` -> evidence/cluster
  resolution returns the labels it *can* resolve (or ``[]``), so a
  consumer that only needs concept lookup can construct the view with the
  store alone.

Read-only by contract: only the L3 lifecycle engine mutates concepts.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.concept_view")


class ConceptView:
    """Ergonomic, read-only facade over the concept layer.

    Constructed once from a :class:`ConceptStore` plus optional
    ``topic_graph`` / ``memory_store``; shared by every background worker
    and prompt-time consumer as their single concept dependency.
    """

    def __init__(
        self,
        store: "ConceptStore | None",
        *,
        topic_graph: "TopicGraph | None" = None,
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self._store = store
        self._topic_graph = topic_graph
        self._memory_store = memory_store

    @property
    def enabled(self) -> bool:
        """True when a concept store is wired (the layer is available)."""
        return self._store is not None

    # ── concept lookup ────────────────────────────────────────────────

    def core(
        self,
        *,
        subject: str | None = None,
        kind: str | None = None,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list["Concept"]:
        """Active concepts matching ``(subject, kind)``, confidence-desc.

        The always-on / "who they are" path (generalises the identity pin
        lane). ``min_confidence`` gates by confidence; ``limit`` caps the
        result. Turn-agnostic -- no embedding involved.
        """
        if self._store is None:
            return []
        try:
            rows = self._store.list_by(
                status="active", subject=subject, kind=kind,
            )
        except Exception:
            log.debug("ConceptView.core: list_by failed", exc_info=True)
            return []
        floor = float(min_confidence)
        out = [
            c for c in rows
            if float(getattr(c, "confidence", 0.0)) >= floor
        ]
        out.sort(
            key=lambda c: float(getattr(c, "confidence", 0.0)), reverse=True,
        )
        if limit is not None:
            out = out[: max(0, int(limit))]
        return out

    def relevant(
        self,
        embedding: "np.ndarray | None",
        *,
        subject: str | None = None,
        kind: str | None = None,
        k: int = 8,
        min_sim: float = 0.0,
    ) -> list[tuple["Concept", float]]:
        """Up to ``k`` active concepts nearest ``embedding`` by label
        cosine, each paired with its similarity (>= ``min_sim``).

        The turn-relevant path, wrapping the single
        :meth:`ConceptStore.nearest` retrieval primitive. ``subject`` /
        ``kind`` are only forwarded when set, so the store's cached
        active-set fast path (and simpler test doubles) still apply for
        the common unfiltered query.
        """
        if self._store is None or embedding is None or k <= 0:
            return []
        kwargs: dict[str, object] = {"status": "active", "k": int(k)}
        if subject is not None:
            kwargs["subject"] = subject
        if kind is not None:
            kwargs["kind"] = kind
        try:
            pairs = self._store.nearest(embedding, **kwargs)  # type: ignore[arg-type]
        except Exception:
            log.debug("ConceptView.relevant: nearest failed", exc_info=True)
            return []
        floor = float(min_sim)
        return [
            (c, float(sim)) for (c, sim) in pairs if float(sim) >= floor
        ]

    def for_target(
        self,
        target: str,
        *,
        subject: str | None = None,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list["Concept"]:
        """Active concepts whose kind routes to ``target`` (see
        :func:`app.core.concepts.concept_kinds.kinds_for_target`).

        The plug-in seam: a new kind declares where it surfaces via
        ``surfacing_targets`` and auto-flows to the matching consumer with
        no consumer code change. Merged across the routed kinds,
        deduped by id, confidence-desc.
        """
        if self._store is None or not target:
            return []
        from app.core.concepts.concept_kinds import kinds_for_target

        kinds = kinds_for_target(target, subject=subject)
        if not kinds:
            return []
        seen: set[int] = set()
        out: list["Concept"] = []
        for kind in kinds:
            for c in self.core(
                subject=subject, kind=kind, min_confidence=min_confidence,
            ):
                cid = int(getattr(c, "concept_id", 0))
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(c)
        out.sort(
            key=lambda c: float(getattr(c, "confidence", 0.0)), reverse=True,
        )
        if limit is not None:
            out = out[: max(0, int(limit))]
        return out

    def for_cluster(self, rep_id: object) -> list["Concept"]:
        """Active concepts that span a topic cluster (keyed by the
        cluster's stable representative id).

        Walks the ``cluster -> concept`` evidence edges
        (:meth:`ConceptStore.edges_from`) and resolves them to their
        active concept rows -- the interest-map annotation seam.
        """
        if self._store is None:
            return []
        try:
            edges = self._store.edges_from("cluster", rep_id)
        except Exception:
            log.debug("ConceptView.for_cluster: edges_from failed", exc_info=True)
            return []
        seen: set[int] = set()
        out: list["Concept"] = []
        for e in edges:
            if getattr(e, "dst_type", None) != "concept":
                continue
            try:
                cid = int(e.dst_id)
            except (TypeError, ValueError):
                continue
            if cid in seen:
                continue
            seen.add(cid)
            c = self._store.get(cid)
            if c is not None and getattr(c, "status", None) == "active":
                out.append(c)
        return out

    # ── grounding resolution ──────────────────────────────────────────

    def evidence_labels(
        self, concept_id: int, *, limit: int | None = 2,
    ) -> list[str]:
        """Human-readable labels for a concept's evidence edges
        (memory content / cluster summary / referenced concept label).

        Wraps the shared
        :func:`app.core.concepts.concept_snapshot.resolve_evidence_labels`
        seam, using the view's ``memory_store`` / ``topic_graph``. Returns
        only what it can resolve when those deps are absent.
        """
        if self._store is None:
            return []
        try:
            from app.core.concepts.concept_snapshot import (
                resolve_evidence_labels,
            )

            return resolve_evidence_labels(
                self._store,
                self._memory_store,
                self._topic_graph,
                int(concept_id),
                limit=limit,
            )
        except Exception:
            log.debug("ConceptView.evidence_labels failed", exc_info=True)
            return []


def concept_view_from(host: object) -> "ConceptView | None":
    """Build a :class:`ConceptView` from a host object's wired stores.

    The one place that reads the ``_concept_store`` / ``_topic_graph`` /
    ``_memory_store`` attributes off a :class:`SessionController`-like
    host, so every consumer (the identity pin lane, ``recall_concept``, a
    background worker's late-bound provider) constructs the facade the
    same way. Returns ``None`` when the concept layer isn't wired.
    """
    store = getattr(host, "_concept_store", None)
    if store is None:
        return None
    return ConceptView(
        store,
        topic_graph=getattr(host, "_topic_graph", None),
        memory_store=getattr(host, "_memory_store", None),
    )


__all__ = ["ConceptView", "concept_view_from"]

"""L24 concept read-facade -- the single interface every deriver/worker
uses to consume concepts.

Before L24 each consumer reached the :class:`ConceptStore` directly (via
``getattr(self, "_concept_store", None)``) and called ``nearest`` /
``list_by`` with its own filter tuple, and separately re-resolved evidence
labels / cluster summaries through :mod:`concept_snapshot` helpers. That
spread the "how do I read concepts?" knowledge across the identity pin
lane, ``recall_concept``, and every future deriver.

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
    from collections.abc import Callable

    import numpy as np

    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.concept_view")


# P44 — the ordering of these lists is prompt-cache-critical. Several
# consumers render them straight into T0 / T1 prompt blocks, so two
# concepts trading places invalidates the cache for the whole rest of the
# prompt, and a swap across a ``limit`` cut adds or drops a bullet
# outright.
#
# Raw ``confidence`` is far too live a key for that. L3 nudges it on
# every lifecycle tick, and with hundreds of active concepts packed into
# a narrow band the gaps between neighbours are thousandths — so the
# order, and the membership at the cut, churn every single turn.
# Quantising into 0.05 bands throws the drift away (far coarser than the
# jitter, far finer than any real difference in belief) and leaves
# ordering to settle on a key that does not move.
#
# Same fix and same reasoning as the SQLite profile query in
# ``app/core/infra/user_profile.py``, which this lane bypassed when L28
# put concepts at the head of the profile block. ``concept_id`` rather
# than the label is the tie-breaker because L17 can rewrite a label in
# place, which would put the churn straight back.
_CONFIDENCE_BANDS_PER_UNIT = 20.0


def _stable_rank(concept: "Concept") -> tuple[int, int]:
    """Sort key: strongest confidence band first, then oldest concept."""
    confidence = float(getattr(concept, "confidence", 0.0) or 0.0)
    band = int(confidence * _CONFIDENCE_BANDS_PER_UNIT)
    return (-band, int(getattr(concept, "concept_id", 0) or 0))


#: Stand-in age for the "would this promote if it were old enough?" probe
#: in :meth:`ConceptView.testable`. Any value past the largest per-kind
#: floor (3.0 engaged days today) settles the age leg; this leaves room
#: for a kind that later wants a much longer maturation.
_AGE_SATISFIED_DAYS = 1e6


def _age_is_the_only_blocker(
    concept: "Concept",
    *,
    gate: "Callable[..., bool]",
    min_sources: int,
    min_confidence: float,
    min_age_days: float,
) -> bool:
    """Whether this candidate clears its promotion gate on everything
    except time.

    Asked by re-running the concept's real gate with age neutralised: if
    it passes then, sources and conviction are already satisfied and only
    the clock is left. A malformed row degrades to ``False`` (treated as
    genuinely unsettled), because the cost of asking about one row too
    many is a slightly odd question, while the cost of a raised exception
    here is the whole ask lane going silent.
    """
    try:
        return bool(
            gate(
                distinct_source_count=int(
                    getattr(concept, "distinct_source_count", 0) or 0
                ),
                age_days=_AGE_SATISFIED_DAYS,
                confidence=float(getattr(concept, "confidence", 0.0) or 0.0),
                min_sources=int(min_sources),
                min_age_days=float(min_age_days),
                min_confidence=float(min_confidence),
            )
        )
    except Exception:
        log.debug("testable: promotion gate probe failed", exc_info=True)
        return False


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
        """Active concepts matching ``(subject, kind)``, strongest first.

        The always-on / "who they are" path (generalises the identity pin
        lane). ``min_confidence`` gates by confidence; ``limit`` caps the
        result. Turn-agnostic -- no embedding involved.

        Ranked by :func:`_stable_rank`, not by raw confidence: this feeds
        prompt blocks in the cache prefix, so the order has to survive
        L3's per-tick confidence drift.
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
        out.sort(key=_stable_rank)
        if limit is not None:
            out = out[: max(0, int(limit))]
        return out

    def core_lane(
        self,
        *,
        limit: int,
        default_min_confidence: float = 0.0,
        per_kind_cap: int | None = None,
    ) -> list["Concept"]:
        """The L27 always-on **core lane**: up to ``limit`` high-confidence
        concepts pinned into the prompt every turn regardless of the live
        turn, *balanced across kinds and subjects*.

        Generalises the old identity-only pin: every kind that opts in via
        ``ConceptKind.core_always_on`` contributes, each gated by its own
        ``core_min_confidence`` bar (falling back to
        ``default_min_confidence``). Candidates are bucketed by
        ``(kind, subject)`` and drawn round-robin — strongest bucket first,
        then one from each in turn — so a prolific kind (usually identity,
        the only one mined today) can't crowd out value / boundary /
        relationship, and both the user-model and Aiko's self-model reach
        the brain. ``per_kind_cap`` optionally caps how deep any single
        bucket is drawn before the round-robin.

        Turn-agnostic (no embedding); the turn-relevant fill layers on top
        via :meth:`relevant`. Returns ``[]`` when the layer is cold/disabled
        or no kind opts in.
        """
        if self._store is None or int(limit) <= 0:
            return []
        from app.core.concepts.concept_kinds import core_lane_kinds

        kinds = core_lane_kinds()
        if not kinds:
            return []
        default_bar = float(default_min_confidence)
        # Bucket by (kind, subject); each bucket is confidence-desc because
        # ``core`` sorts, so bucket[0] is that area's strongest concept.
        buckets: dict[tuple[str, str], list["Concept"]] = {}
        for kind in kinds:
            bar = (
                float(kind.core_min_confidence)
                if kind.core_min_confidence is not None
                else default_bar
            )
            rows = self.core(kind=kind.name, min_confidence=bar, limit=per_kind_cap)
            for c in rows:
                subject = str(getattr(c, "subject", "") or "user")
                buckets.setdefault((kind.name, subject), []).append(c)
        if not buckets:
            return []

        def _bucket_band(key: tuple[str, str]) -> int:
            lst = buckets[key]
            if not lst:
                return 0
            # Banded, not raw: ordering buckets by their top concept's
            # live confidence let two close areas trade places on L3
            # drift, reshuffling the whole round-robin. See _stable_rank.
            return -_stable_rank(lst[0])[0]

        # Strongest area first at each round; name+subject breaks ties so
        # selection is deterministic turn to turn.
        order = sorted(buckets, key=lambda k: (-_bucket_band(k), k))
        out: list["Concept"] = []
        cap = int(limit)
        rank = 0
        while len(out) < cap:
            progressed = False
            for key in order:
                lst = buckets[key]
                if rank < len(lst):
                    out.append(lst[rank])
                    progressed = True
                    if len(out) >= cap:
                        break
            if not progressed:
                break
            rank += 1
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

    def hypotheses(
        self,
        embedding: "np.ndarray | None",
        *,
        subject: str | None = None,
        k: int = 8,
        min_sim: float = 0.0,
        min_sources: int = 1,
        min_unsettled: float = 0.22,
    ) -> list[tuple["Concept", float]]:
        """L30a: up to ``k`` **open questions** nearest ``embedding``.

        The one read path in this class that does not return
        ``status="active"`` rows. Everything else here is the settled
        register; this is the tentative one, and the two must not mix --
        a candidate reaching :meth:`core` or :meth:`for_target` would put
        an unestablished belief into the T0 profile block, which is both
        a prompt-cache break and Aiko asserting something she has not
        earned.

        Two eligibility bars, both measured rather than guessed (see
        :mod:`app.core.concepts.concept_hypothesis`):

        - ``min_sources`` drops ungrounded proposals. A candidate with no
          evidence edge at all is a bare LLM hunch, and it scores *highest*
          on unsettledness precisely because nothing supports it -- so
          without this floor the lane leads with its worst rows.
        - ``min_unsettled`` drops the beliefs that are merely young. Most
          candidates have already cleared every evidence and confidence
          bar and are waiting only on the promotion age floor; the default
          sits just above the point a twice-grounded, fully-held belief
          scores, so those stay out of the register.

        Returns ``(concept, cosine)`` pairs like :meth:`relevant`, leaving
        the importance blend to the caller that owns the affect context.
        """
        if self._store is None or embedding is None or k <= 0:
            return []
        from app.core.concepts.concept_hypothesis import unsettledness

        kwargs: dict[str, object] = {"status": "candidate", "k": int(k)}
        if subject is not None:
            kwargs["subject"] = subject
        try:
            pairs = self._store.nearest(embedding, **kwargs)  # type: ignore[arg-type]
        except Exception:
            log.debug("ConceptView.hypotheses: nearest failed", exc_info=True)
            return []
        floor = float(min_sim)
        out: list[tuple["Concept", float]] = []
        for concept, sim in pairs:
            if float(sim) < floor:
                continue
            sources = int(getattr(concept, "distinct_source_count", 0) or 0)
            if sources < int(min_sources):
                continue
            if unsettledness(concept) < float(min_unsettled):
                continue
            out.append((concept, float(sim)))
        return out

    def testable(
        self,
        *,
        limit: int = 12,
        subject: str | None = None,
        min_sources: int = 1,
        min_unsettled: float = 0.22,
        promote_min_sources: int = 2,
        promote_min_confidence: float = 0.6,
        promote_min_age_days: float = 0.0,
    ) -> list[tuple["Concept", float]]:
        """L30b: open questions an **answer could actually settle**.

        The off-turn sibling of :meth:`hypotheses`. The ask worker runs
        during quiet windows with no user text, so there is no query
        vector and no cosine term -- eligibility and unsettledness decide,
        and the caller blends in L32 importance (it owns the affect
        context, the same division :meth:`hypotheses` uses).

        Shares the two eligibility bars with :meth:`hypotheses`, then adds
        the one that only matters when the point is to *ask*: a candidate
        whose sole unmet promotion leg is **age** is skipped. Answering
        adds a distinct source, so it can only move a belief held back on
        sources or conviction; a row already clearing both is simply
        waiting out its clock, and asking about it spends a question to
        change nothing while reading to the user as a pointless quiz.

        That leg is detected by re-running the concept's *own*
        ``promotion_gate`` with the age argument satisfied, rather than
        by comparing against the global settings. Every shipped kind
        floors all three legs with its own constants via ``max`` (identity
        and value want three sources, aspiration wants three engaged
        days), so a check written against ``concept_promote_*`` alone
        would be wrong for every kind at once. Passing the gate with age
        neutralised means sources and confidence are already met, which is
        exactly the row to leave alone.

        Returns ``(concept, unsettled)`` pairs, most unsettled first.
        """
        if self._store is None or int(limit) <= 0:
            return []
        from app.core.concepts.concept_hypothesis import unsettledness
        from app.core.concepts.concept_kinds import get_kind
        from app.core.concepts.concept_lifecycle import set_evidence_gate

        try:
            rows = self._store.list_by(status="candidate", subject=subject)
        except Exception:
            log.debug("ConceptView.testable: list_by failed", exc_info=True)
            return []

        scored: list[tuple["Concept", float]] = []
        for concept in rows:
            sources = int(getattr(concept, "distinct_source_count", 0) or 0)
            if sources < int(min_sources):
                continue
            unsettled = unsettledness(concept)
            if unsettled < float(min_unsettled):
                continue
            if _age_is_the_only_blocker(
                concept,
                gate=(
                    getattr(get_kind(concept.kind), "promotion_gate", None)
                    or set_evidence_gate
                ),
                min_sources=promote_min_sources,
                min_confidence=promote_min_confidence,
                min_age_days=promote_min_age_days,
            ):
                continue
            scored.append((concept, unsettled))
        scored.sort(
            key=lambda pair: (
                -pair[1],
                int(getattr(pair[0], "concept_id", 0) or 0),
            )
        )
        return scored[: max(0, int(limit))]

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
        no consumer code change. Merged across the routed kinds, deduped
        by id, then ranked by :func:`_stable_rank` — the merge has to be
        re-sorted, and re-sorting on raw confidence would undo the
        stability :meth:`core` just established.
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
        out.sort(key=_stable_rank)
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

    def activated(
        self,
        cluster_rep_ids,
        *,
        seed_concept_ids=(),
        limit: int = 8,
    ) -> list[tuple["Concept", float]]:
        """L23 spreading activation: concepts *primed* by the turn's active
        set, each paired with an activation strength in ``(0, 1]``.

        Models associative priming -- thinking about one thing lights up its
        neighbours -- over two adjacency paths:

        1. **Shared-cluster (1-hop, strength 1.0).** Concepts that span any of
           ``cluster_rep_ids`` (the turn's hot topic clusters), via the
           ``cluster -> concept`` evidence edges (:meth:`for_cluster`).
        2. **Shared-cluster via a seed concept (2-hop, strength 0.6).** For each
           id in ``seed_concept_ids`` (the directly-relevant / pinned concepts),
           the clusters that back it are found (its ``evidence`` edges), and
           their *other* member concepts are activated -- siblings that share a
           theme with something already in mind.

        A third path -- concept->concept edges (:meth:`ConceptStore.dependents_of`)
        -- lights up for both meta kinds: L12 **tensions** and L20
        **generalizations**, whose bases point at the meta via concept->concept
        ``evidence`` edges, so a base's activation pulls in the meta above it.
        (The ``generalizes`` edge relation stays reserved for a future
        multi-level hierarchy; L20 rides the ``evidence`` relation like L12.)

        Seeds are excluded from the result; the strongest strength per concept
        wins on overlap; the result is capped at ``limit``.
        """
        if self._store is None or limit <= 0:
            return []
        seeds = {int(s) for s in seed_concept_ids}
        acc: dict[int, tuple["Concept", float]] = {}

        def _bump(concept: "Concept", strength: float) -> None:
            cid = int(getattr(concept, "concept_id", 0))
            if cid <= 0 or cid in seeds:
                return
            prev = acc.get(cid)
            if prev is None or strength > prev[1]:
                acc[cid] = (concept, strength)

        # Path 1: siblings that span each hot cluster.
        for rep in cluster_rep_ids or ():
            for concept in self.for_cluster(rep):
                _bump(concept, 1.0)

        # Path 2 + 3: expand out from each seed concept.
        for sid in seeds:
            # 2-hop shared-cluster: the seed's backing clusters -> their members.
            try:
                back = self._store.evidence_of(int(sid))
            except Exception:
                log.debug("activated: evidence_of failed", exc_info=True)
                back = []
            for e in back:
                if getattr(e, "src_type", None) != "cluster":
                    continue
                for concept in self.for_cluster(getattr(e, "src_id", None)):
                    _bump(concept, 0.6)
            # Meta path: concept->concept edges. Live for both meta kinds --
            # L12 tensions and L20 generalizations (base -> meta via
            # ``evidence``); the ``generalizes`` relation stays reserved for a
            # future multi-level hierarchy.
            try:
                for dep_id in self._store.dependents_of(int(sid)):
                    dep = self._store.get(int(dep_id))
                    if dep is not None and getattr(dep, "status", None) == "active":
                        _bump(dep, 0.8)
            except Exception:
                log.debug("activated: dependents_of failed", exc_info=True)

        out = sorted(acc.values(), key=lambda t: t[1], reverse=True)
        return out[: max(0, int(limit))]

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

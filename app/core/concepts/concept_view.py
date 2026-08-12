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

from app.core.concepts.concept_diets import DEFAULT_DIET_TUNING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np

    from app.core.concepts.concept_diets import DietTuning
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


#: What one rendered concept costs beyond its own label -- the kind
#: prefix, the confidence, the bullet and the newline that every consumer
#: wraps a label in. Counted so a diet's budget is spent on what actually
#: reaches the prompt rather than on bare label text, which would
#: under-count by roughly a third at typical label lengths.
_CONCEPT_RENDER_OVERHEAD_TOKENS = 6


def _concept_tokens(concept: "Concept") -> int:
    """Estimated prompt cost of one rendered concept."""
    from app.llm.token_utils import estimate_tokens

    label = str(getattr(concept, "label", "") or "")
    return estimate_tokens(label) + _CONCEPT_RENDER_OVERHEAD_TOKENS


def _band(strength: float) -> int:
    """Quantise a ``[0, 1]`` strength onto the same ladder as
    :func:`_stable_rank`, for the same anti-churn reason."""
    return int(float(strength) * _CONFIDENCE_BANDS_PER_UNIT)


def _round_robin(
    buckets: "dict[tuple[str, str], list[Concept]]",
    *,
    strength: "Callable[[Concept], int] | None" = None,
) -> list["Concept"]:
    """Flatten ``(kind, subject)`` buckets into one balanced draw order.

    Strongest bucket first, then one from each in turn, so a prolific
    kind cannot crowd out the others no matter how many rows it has. The
    caller decides where to cut -- :meth:`ConceptView.core_lane` slices
    at a count, :meth:`ConceptView.for_consumer` spends a token budget --
    which is the whole reason this returns an order rather than a
    selection: *where* you stop is policy, *what order you stop in* is
    the balance guarantee, and only the second one is shared.

    ``strength`` scores a bucket's leading concept to order the buckets;
    it must return a quantised band rather than a live float, or two
    close areas trade places on L3's per-tick drift and reshuffle the
    whole draw. Defaults to the banded confidence in :func:`_stable_rank`.
    """
    if not buckets:
        return []
    rank_of = strength or (lambda c: -_stable_rank(c)[0])

    def _bucket_band(key: tuple[str, str]) -> int:
        rows = buckets[key]
        return rank_of(rows[0]) if rows else 0

    # Name+subject breaks ties so the order is deterministic turn to turn.
    order = sorted(buckets, key=lambda k: (-_bucket_band(k), k))
    out: list["Concept"] = []
    rank = 0
    while True:
        progressed = False
        for key in order:
            rows = buckets[key]
            if rank < len(rows):
                out.append(rows[rank])
                progressed = True
        if not progressed:
            break
        rank += 1
    return out


def _kind_first(
    by_kind: "dict[str, dict[str, list[Concept]]]",
) -> list["Concept"]:
    """Draw order that rotates kinds first and subjects within a kind.

    :func:`_round_robin` balances ``(kind, subject)`` buckets, which shares
    evenly between *buckets* and therefore hands a kind one share per
    subject it happens to be populated in. A kind mined for both the user
    and Aiko then takes twice the room of one mined for a single subject,
    for no reason a reader of the prompt would recognise -- which is the
    opposite of the "no one kind crowds out the rest" guarantee the
    bucketing is there to provide.

    So each kind is balanced across its own subjects first, and the kinds
    are then interleaved, strongest-band-first. Every kind present gets its
    turn before any kind gets a second, and a kind's subjects still
    alternate inside its own share.
    """
    if not by_kind:
        return []
    per_kind = {
        name: _round_robin({
            (name, subject): rows for subject, rows in subjects.items()
        })
        for name, subjects in by_kind.items()
        if any(subjects.values())
    }
    if not per_kind:
        return []
    # ``_stable_rank`` already leads with the negated band, so sorting on
    # it ascending is strongest-band-first. Name breaks ties so the order
    # is identical turn to turn, which the prompt cache depends on.
    order = sorted(
        per_kind, key=lambda name: (_stable_rank(per_kind[name][0])[0], name),
    )
    draw: list["Concept"] = []
    depth = 0
    while any(len(per_kind[name]) > depth for name in order):
        for name in order:
            rows = per_kind[name]
            if depth < len(rows):
                draw.append(rows[depth])
        depth += 1
    return draw


#: A habituation factor at or above this counts as fully rested. Matches
#: the core lane's own fresh/stale threshold in ``build_relevant_context``
#: -- the factor is a float, so an exact ``1.0`` comparison would classify
#: a rounded 0.9999 as recently shown.
_FRESH_HABITUATION = 0.999


def _rest_key(
    concept: "Concept", rest: "Callable[[Concept], float]",
) -> tuple[int, float]:
    """Sort key preferring rested concepts, for a stable re-order.

    Fresh first (as one group, so the caller's balanced order survives
    inside it), then the rested-longest of the stale ones -- the same
    two-tier ordering the core lane applies to its own picks, and for the
    same reason: reading habituation as a bare threshold leaves the stale
    group in confidence order, so a concept shown last turn outranks one
    rested for three. A failed read counts as fresh, because losing the
    reserve entirely is worse than pinning something recently seen.
    """
    try:
        value = float(rest(concept))
    except Exception:
        log.debug("openness reserve: habituation read failed", exc_info=True)
        return (0, 0.0)
    if value >= _FRESH_HABITUATION:
        return (0, 0.0)
    return (1, -value)


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
        kv_get: "Callable[[str], str | None] | None" = None,
        tuning: "DietTuning | None" = None,
    ) -> None:
        self._store = store
        self._topic_graph = topic_graph
        self._memory_store = memory_store
        # Only :meth:`for_consumer` reads these two. ``kv_get`` is the
        # cluster-affect source behind L32 importance; ``tuning`` carries
        # the diet budget. Both are optional so every existing direct
        # construction keeps working unchanged -- see ``DietTuning``.
        self._kv_get = kv_get
        self._tuning = tuning or DEFAULT_DIET_TUNING

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
        openness_slots: int = 0,
        openness_min_confidence: float = 0.5,
        openness_rest: "Callable[[Concept], float] | None" = None,
    ) -> list["Concept"]:
        """The L27 always-on **core lane**: up to ``limit`` high-confidence
        concepts pinned into the prompt every turn regardless of the live
        turn, *balanced across kinds and subjects*.

        Generalises the old identity-only pin: every kind that opts in via
        ``ConceptKind.core_always_on`` contributes, each gated by its own
        ``core_min_confidence`` bar (falling back to
        ``default_min_confidence``). Candidates are drawn kind-first and
        subject-second (:func:`_kind_first`) — every kind present gets a
        slot before any kind gets a second — so a prolific kind can't crowd
        out value / boundary / relationship, and both the user-model and
        Aiko's self-model reach the brain. ``per_kind_cap`` optionally caps
        how deep any single kind is drawn before the interleave.

        That draw used to balance ``(kind, subject)`` buckets flat, which
        shares evenly between buckets rather than between kinds: a kind
        mined for both subjects took two shares and a kind mined for one
        took a single share. On the live graph that made ``boundary`` --
        the only core kind with two deep subject pools -- 38% of the pinned
        lane, on nothing more principled than which kinds happen to be
        populated on both sides. Same defect ``_openness_picks`` was fixed
        for, so now the same fix, shared.

        ``openness_slots`` keeps that many of ``limit`` for the **openness
        reserve** (see :meth:`_openness_picks`), which is the only way a
        ``generative`` kind can reach this lane at all. It is capped at
        half the lane so the reserve stays an opening rather than a
        takeover: on the default ``core_cap`` of 2 a literal reading of
        the default 2 slots would pin nothing *but* generative concepts
        and lose the identity that says who she is talking to, which is
        the opposite failure to the one this fixes. ``openness_rest`` is
        the caller's habituation read, passed through so the reserve
        rotates like the rest of the lane (the view owns no clock).

        Turn-agnostic (no embedding); the turn-relevant fill layers on top
        via :meth:`relevant`. Returns ``[]`` when the layer is cold/disabled
        or no kind opts in.
        """
        if self._store is None or int(limit) <= 0:
            return []
        from app.core.concepts.concept_kinds import core_lane_kinds

        cap = int(limit)
        reserved = self._openness_picks(
            slots=min(int(openness_slots), cap // 2),
            min_confidence=openness_min_confidence,
            rest=openness_rest,
        )
        kinds = core_lane_kinds()
        if not kinds:
            return reserved
        default_bar = float(default_min_confidence)
        # Bucket by kind, then subject; each bucket is confidence-desc
        # because ``core`` sorts, so bucket[0] is that area's strongest.
        by_kind: dict[str, dict[str, list["Concept"]]] = {}
        for kind in kinds:
            bar = (
                float(kind.core_min_confidence)
                if kind.core_min_confidence is not None
                else default_bar
            )
            rows = self.core(kind=kind.name, min_confidence=bar, limit=per_kind_cap)
            for c in rows:
                subject = str(getattr(c, "subject", "") or "user")
                by_kind.setdefault(kind.name, {}).setdefault(
                    subject, [],
                ).append(c)
        if not by_kind:
            return reserved
        # Only the slots the reserve actually filled are spent: an
        # unfillable reserve gives its room back to the ordinary lane
        # rather than shortening the pin.
        room = max(0, cap - len(reserved))
        return reserved + _kind_first(by_kind)[:room]

    def _openness_picks(
        self,
        *,
        slots: int,
        min_confidence: float,
        rest: "Callable[[Concept], float] | None" = None,
    ) -> list["Concept"]:
        """The strongest ``generative``-role concepts, for the core lane.

        Only ``core_always_on`` kinds are eligible for that lane, and the
        four that opt in are ``identity``, ``value``, ``boundary`` and
        ``generalization`` -- two anchors and two guides. So the pinned
        prompt is structurally incapable of carrying an aspiration, a
        taste or a pursuit, however wide the cap is set: every turn is
        guaranteed to arrive carrying what Aiko must respect and nothing
        she might move on. This reserve is the exception that fixes it,
        and it deliberately draws from the kinds the lane cannot
        otherwise reach rather than asking those kinds to opt in --
        ``core_always_on`` means "pin this whenever it qualifies", which
        is the wrong promise for a taste.

        Three things the first cut got wrong, all measured on the live
        graph (L28m) rather than reasoned about:

        **Not every generative kind can hold a pin.** ``tension`` is the
        most generative kind in the registry, and a tension pinned into
        every turn is exactly the nagging L12's cooldown exists to
        prevent. That used to be handled by reading ``static_render``,
        since a tension rendered nowhere; H10 gave it the flex lane, so
        the two questions came apart and the kind now answers them
        separately -- it renders when the turn is about it (``static_render``)
        and is never pinned regardless of the turn (``pinnable``). Both are
        read off the registry so neither can drift from the renderer.

        **The draw is one kind at a time, not one bucket at a time.** With
        two slots and flat ``(kind, subject)`` buckets ordered by
        confidence, both aspiration buckets outranked everything else and
        took both slots -- ``taste`` and ``pursuit`` were unreachable by
        construction, however much supply they grew. Rotating kinds first
        and subjects within a kind means the reserve's *breadth* scales
        with the slot count, which is what an openness mechanism should
        spend its slots on.

        **It has to rotate.** ``rest`` is the caller's habituation read
        (the view owns no clock). Without it the same strongest
        aspiration is pinned every turn forever, which is the repetition
        failure L23 was built to fix, reintroduced by the mechanism meant
        to keep her open. Fresh concepts are preferred and the rested-longest
        wins among stale ones, matching the core lane's own ordering; the
        sort is stable, so with an all-fresh state the draw is exactly the
        balanced order above and stays prefix-stable for the prompt cache.
        """
        if self._store is None or slots <= 0:
            return []
        from app.core.concepts.concept_kinds import (
            ROLE_GENERATIVE,
            core_lane_kinds,
            kinds_by_role,
            renders_in_static_block,
        )

        already = {k.name for k in core_lane_kinds()}
        by_kind: dict[str, dict[str, list[Concept]]] = {}
        for kind in kinds_by_role(ROLE_GENERATIVE):
            if kind.name in already or not renders_in_static_block(kind.name):
                continue
            if not bool(getattr(kind, "pinnable", True)):
                continue
            rows = self.core(
                kind=kind.name, min_confidence=float(min_confidence),
            )
            if not rows:
                continue
            subjects: dict[str, list[Concept]] = {}
            for c in rows:
                subject = str(getattr(c, "subject", "") or "user")
                subjects.setdefault(subject, []).append(c)
            by_kind[kind.name] = subjects
        if not by_kind:
            return []

        # Kinds in strongest-first order, each already balanced across its
        # own subjects, then interleaved so slot N+1 goes to a different
        # kind than slot N wherever one is available.
        draw = _kind_first(by_kind)
        if rest is not None:
            draw.sort(key=lambda c: _rest_key(c, rest))
        return draw[:slots]

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

    def for_cluster(
        self, rep_id: object, *, kinds: "Sequence[str] | None" = None,
    ) -> list["Concept"]:
        """Active concepts that span a topic cluster (keyed by the
        cluster's stable representative id).

        Walks the ``cluster -> concept`` evidence edges
        (:meth:`ConceptStore.edges_from`) and resolves them to their
        active concept rows -- the interest-map annotation seam.

        ``kinds`` narrows the result to a consumer's declared diet.
        Without it the caller gets whatever happens to be edged to the
        cluster, which is the pre-diet behaviour: fine for a debug
        surface, too indiscriminate for a worker prompt.
        """
        if self._store is None:
            return []
        wanted = {str(k) for k in kinds} if kinds is not None else None
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
            if c is None or getattr(c, "status", None) != "active":
                continue
            if wanted is not None and str(getattr(c, "kind", "")) not in wanted:
                continue
            out.append(c)
        return out

    # ── worker concept diets ──────────────────────────────────────────

    def for_consumer(
        self, consumer: str, *, subject: str | None = None,
    ) -> list["Concept"]:
        """The concepts one worker is allowed to think with.

        Reads that consumer's :class:`~app.core.concepts.concept_diets.ConceptDiet`
        and returns a token-budgeted, kind-balanced selection. ``[]`` when
        the consumer has no diet, which is the ordinary answer for most of
        the codebase and never an error -- see ``diet_for``.

        Two decisions matter here and they interact.

        **Ranking is importance x confidence, within a kind.** Confidence
        alone buries the belief that matters more than it is established
        -- the "attention gap" the L22 report already tracks. Importance
        is the kind's stakes prior lifted by the emotional charge of the
        topics a concept is grounded in, so *within* one kind the prior is
        constant and the axis reorders purely on charge: the tastes she
        actually feels something about lead the tastes she does not.

        **Selection is round-robin, across kinds.** That is what stops the
        first decision becoming the very problem it is meant to solve. The
        prior is constant within a kind but very much not across kinds, so
        a single global sort by importance x confidence would return
        boundaries and values until the budget ran out -- a strictly worse
        result than confidence-only ranking, because it would be *more*
        confident about being closed. Balancing the draw first and ranking
        inside the bucket second gets the benefit of both.

        ``subject`` overrides the diet's own subject scope for a caller
        that already knows which side it is asking about.
        """
        from app.core.concepts.concept_diets import (
            diet_for,
            importance_lookup,
            resolve_budget,
        )

        diet = diet_for(consumer)
        if self._store is None or diet is None:
            return []
        scope = subject if subject is not None else diet.subject
        pool: dict[str, list["Concept"]] = {
            name: self.core(
                subject=scope, kind=name, min_confidence=diet.min_confidence,
            )
            for name in diet.kinds
        }
        importance = importance_lookup(
            {
                int(getattr(c, "concept_id", 0) or 0)
                for rows in pool.values()
                for c in rows
                if getattr(c, "concept_id", 0)
            },
            store=self._store,
            topic_graph=self._topic_graph,
            kv_get=self._kv_get,
            tuning=self._tuning,
        )

        def _weight(concept: "Concept") -> float:
            conf = float(getattr(concept, "confidence", 0.0) or 0.0)
            return conf * importance(concept)

        buckets: dict[tuple[str, str], list["Concept"]] = {}
        for name, rows in pool.items():
            # Re-rank inside the kind before the per-kind cap, or the cap
            # would slice on confidence and discard exactly the charged
            # rows the importance axis exists to promote.
            rows.sort(key=lambda c: (-_band(_weight(c)), _stable_rank(c)))
            if diet.per_kind_cap is not None:
                rows = rows[: max(0, int(diet.per_kind_cap))]
            for c in rows:
                subj = str(getattr(c, "subject", "") or "user")
                buckets.setdefault((name, subj), []).append(c)
        if not buckets:
            return []

        budget = resolve_budget(diet, self._tuning)
        order = _round_robin(buckets, strength=lambda c: _band(_weight(c)))
        out: list["Concept"] = []
        spent = 0
        for concept in order:
            if diet.max_concepts is not None and len(out) >= diet.max_concepts:
                break
            cost = _concept_tokens(concept)
            if spent + cost > budget:
                # Skip rather than stop: a long label should cost itself
                # its slot, not everything queued behind it. The draw is
                # already balanced, so continuing keeps trimming evenly
                # instead of truncating whichever kind the long one is in.
                continue
            out.append(concept)
            spent += cost
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

    Also resolves the diet budget and the affect source here, so a worker
    gets a fully-configured :meth:`ConceptView.for_consumer` without any
    call site having to know that diets are sized off the worker route.
    """
    from app.core.concepts.concept_diets import tuning_from_host

    store = getattr(host, "_concept_store", None)
    if store is None:
        return None
    chat_db = getattr(host, "_chat_db", None)
    return ConceptView(
        store,
        topic_graph=getattr(host, "_topic_graph", None),
        memory_store=getattr(host, "_memory_store", None),
        kv_get=getattr(chat_db, "kv_get", None),
        tuning=tuning_from_host(host),
    )


__all__ = ["ConceptView", "concept_view_from"]

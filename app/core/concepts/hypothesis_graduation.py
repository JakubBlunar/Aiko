"""L30 Phase B — how a guess stops being a guess.

Two operations live here because they are the same lookup asked at two
moments.

Linking (every confirmation)
----------------------------
There is a race built into the design, and it is not an edge case. A
confirmed hypothesis stores the user's answer as an ordinary memory; that
memory is clustered like any other, and L2 proposes a concept from it
**knowing nothing about the hypothesis**. L2 needs one confirmation where
graduation needs two, so L2 usually gets there first. "My guess turned
out to be something I already believe" is therefore the *normal* ending
of a successful hypothesis, not the exceptional one.

:func:`link_if_duplicate` runs after every confirmation rather than only
at graduation, so the link is stamped at the earliest moment it can be.
Once ``linked_concept_id`` is set, three things follow: the answer
memories attach to that concept as they arrive instead of piling up
unused, the lane stops surfacing the guess (the concept speaks for the
belief now), and graduation knows to take the merged exit.

Graduation (support threshold reached)
--------------------------------------
:func:`graduate` has three exits, and which one it takes is decided by
the same duplicate lookup:

- **merged** — a concept already carries this belief. Bump its
  ``last_reinforced_at``, make sure every answer memory is attached, and
  close the row as ``merged``. Distinct from ``graduated`` on purpose:
  L17f and L19 should be able to narrate "I turned out to be right about
  something I already knew" differently from "I was right about something
  new".
- **graduated** — nothing matches, so ``ConceptStore.add()`` mints a
  ``candidate`` concept carrying the answer memories as evidence edges,
  and L3's ordinary promotion gate takes it from there. This is the step
  the whole layer exists for.
- **anchored** — a ``world``-subject guess ("espresso pucks channel when
  the grind is too coarse") has no concept kind to become, so it exits as
  a durable memory instead and skips the duplicate check entirely.

What this deliberately does not do
----------------------------------
Set ``confidence`` or ``status`` on the concept it mints or merges into.
A graduated concept enters as an ordinary ``candidate`` at the ordinary
default confidence and waits for L3 like every other candidate. Having
been guessed correctly twice is not evidence of anything beyond the two
answers, which are already attached as edges for L3 to count.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.core.concepts.concept_dedupe import find_duplicate
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.concepts.hypothesis_store import (
    STATUS_GRADUATED,
    STATUS_MERGED,
    SUBJECT_WORLD,
)

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_store import ConceptStore
    from app.core.concepts.hypothesis_store import Hypothesis, HypothesisStore


log = logging.getLogger("app.hypothesis_graduation")


#: ``concept_events.event_type`` values for the two concept-side exits.
#: Separate values rather than one ``hypothesis_graduated`` with a flag,
#: because the diary reads event types and the two are different stories.
EVENT_GRADUATED = "hypothesis_graduated"
EVENT_MERGED = "hypothesis_merged"

#: What :func:`graduate` did. ``anchored`` is the world-shaped exit.
EXIT_GRADUATED = "graduated"
EXIT_MERGED = "merged"
EXIT_ANCHORED = "anchored"


@dataclass(frozen=True, slots=True)
class GraduationResult:
    """Which exit a hypothesis took, and what it left behind."""

    exit: str
    hypothesis_id: int
    concept_id: int | None = None
    memory_id: int | None = None


def link_if_duplicate(
    *,
    hypothesis_store: "HypothesisStore",
    concept_store: "ConceptStore",
    row: "Hypothesis",
    memory_id: int | None = None,
) -> "Concept | None":
    """Point a confirmed guess at the concept that already holds it.

    Idempotent: an already-linked row re-attaches ``memory_id`` (the
    edge write is an upsert) and returns the concept without repeating
    the lookup, which is what makes calling this on *every* confirmation
    cheap enough to be the policy.

    ``world`` rows never link — there is no concept subject for how
    something works, so the nearest concept would be a coincidence of
    wording. Never raises.
    """
    if row.subject == SUBJECT_WORLD:
        return None
    try:
        if row.linked_concept_id is not None:
            concept = concept_store.get(int(row.linked_concept_id))
            if concept is not None:
                _attach(concept_store, concept, memory_id)
                return concept
            # The concept was deleted out from under the link. Clear it
            # so the next confirmation can find a live one rather than
            # chasing a dead id forever.
            row.linked_concept_id = None
            hypothesis_store.update(row)

        vec = getattr(row, "embedding", None)
        if vec is None or len(vec) == 0:
            return None
        # ``kind=None`` on purpose: the proposer's guessed kind carries no
        # authority, so filtering on it would miss the duplicate whenever
        # L2 chose a different taxonomy for the same belief and fork the
        # graph on a disagreement about labels. See ``concept_dedupe``.
        duplicate, _sim = find_duplicate(
            concept_store, vec, subject=row.subject, kind=None,
        )
        if duplicate is None:
            return None
        hypothesis_store.link(row, int(duplicate.concept_id))
        _attach(concept_store, duplicate, memory_id)
        log.info(
            "hypothesis linked to existing concept: hid=%s cid=%s "
            "statement=%r label=%r",
            row.hypothesis_id,
            duplicate.concept_id,
            str(row.statement)[:60],
            str(duplicate.label)[:60],
        )
        return duplicate
    except Exception:
        log.warning(
            "hypothesis link failed (hid=%s)", row.hypothesis_id, exc_info=True
        )
        return None


def graduate(
    *,
    hypothesis_store: "HypothesisStore",
    concept_store: "ConceptStore",
    row: "Hypothesis",
    event_store: "ConceptEventStore | None" = None,
    memory_writer: Callable[[str], int | None] | None = None,
) -> GraduationResult | None:
    """Close a proven guess by the exit that fits it. Never raises."""
    try:
        if row.subject == SUBJECT_WORLD:
            return _anchor(hypothesis_store, row, memory_writer)
        existing = link_if_duplicate(
            hypothesis_store=hypothesis_store,
            concept_store=concept_store,
            row=row,
        )
        if existing is not None:
            return _merge(
                hypothesis_store, concept_store, row, existing, event_store
            )
        return _mint(hypothesis_store, concept_store, row, event_store)
    except Exception:
        log.warning(
            "hypothesis graduation failed (hid=%s)",
            row.hypothesis_id,
            exc_info=True,
        )
        return None


def is_ready(
    row: "Hypothesis", *, min_support: int, min_credence: float,
) -> bool:
    """Whether a guess has earned an exit.

    The refutation clause is the one doing real work: a hypothesis the
    user has contradicted even once must never graduate on the strength
    of confirmations elsewhere, because the contradiction is about *this*
    belief and the confirmations may have been politeness.

    A linked row is held to the same bar rather than fast-tracked. It is
    tempting to close it early — the concept exists, so what is there to
    prove — but the link is a *cosine* judgement, and closing on one
    confirmation would let a near-miss match retire a guess that was
    actually about something adjacent.
    """
    return (
        row.is_live
        and int(row.refute_count) <= 0
        and int(row.support_count) >= int(min_support)
        and float(row.credence) >= float(min_credence)
    )


# ── exits ─────────────────────────────────────────────────────────────


def _merge(
    hypothesis_store: "HypothesisStore",
    concept_store: "ConceptStore",
    row: "Hypothesis",
    concept: "Concept",
    event_store: "ConceptEventStore | None",
) -> GraduationResult:
    """Fold into a belief that already existed."""
    for memory_id in row.answer_memory_ids:
        _attach(concept_store, concept, memory_id)
    _recount(concept_store, concept)
    concept.last_reinforced_at = _now_iso()
    # confidence / status stay L3's, exactly as on the L30c confirm path.
    concept_store.update(concept)
    hypothesis_store.close(
        row, status=STATUS_MERGED, concept_id=int(concept.concept_id)
    )
    _record(event_store, EVENT_MERGED, concept, row)
    log.info(
        "hypothesis merged into concept: hid=%s cid=%s", row.hypothesis_id,
        concept.concept_id,
    )
    return GraduationResult(
        exit=EXIT_MERGED,
        hypothesis_id=int(row.hypothesis_id),
        concept_id=int(concept.concept_id),
    )


def _mint(
    hypothesis_store: "HypothesisStore",
    concept_store: "ConceptStore",
    row: "Hypothesis",
    event_store: "ConceptEventStore | None",
) -> GraduationResult:
    """Become a new candidate concept."""
    concept = Concept(
        label=str(row.statement),
        kind=str(row.kind),
        subject=str(row.subject),
        user_id=row.user_id,
        evidence_model=_evidence_model_for(row.kind),
        status="candidate",
        rationale=_rationale_for(row),
        embedding=row.embedding,
        origin_session=row.origin_session,
    )
    concept_id = concept_store.add(concept)
    for memory_id in row.answer_memory_ids:
        _attach(concept_store, concept, memory_id)
    _recount(concept_store, concept)
    concept.last_reinforced_at = _now_iso()
    concept_store.update(concept)
    hypothesis_store.close(
        row, status=STATUS_GRADUATED, concept_id=int(concept_id)
    )
    _record(event_store, EVENT_GRADUATED, concept, row)
    log.info(
        "hypothesis graduated to concept: hid=%s cid=%s label=%r",
        row.hypothesis_id,
        concept_id,
        str(row.statement)[:80],
    )
    return GraduationResult(
        exit=EXIT_GRADUATED,
        hypothesis_id=int(row.hypothesis_id),
        concept_id=int(concept_id),
    )


def _anchor(
    hypothesis_store: "HypothesisStore",
    row: "Hypothesis",
    memory_writer: Callable[[str], int | None] | None,
) -> GraduationResult:
    """Become a durable memory: the exit for a guess about the world.

    ``memory_writer`` is a ``(statement) -> memory_id | None`` callable
    the caller supplies, because embedding and notification belong to the
    session, not here. Without one the row still closes — the
    alternative is a proven guess sitting ``supported`` forever because a
    plumbing handle was absent, which is worse than losing the memory.
    """
    memory_id: int | None = None
    if memory_writer is not None:
        try:
            written = memory_writer(str(row.statement))
            memory_id = int(written) if written else None
        except Exception:
            log.warning(
                "world hypothesis memory write failed (hid=%s)",
                row.hypothesis_id,
                exc_info=True,
            )
            memory_id = None
    hypothesis_store.close(
        row, status=STATUS_GRADUATED, memory_id=memory_id
    )
    log.info(
        "world hypothesis anchored: hid=%s mid=%s", row.hypothesis_id, memory_id
    )
    return GraduationResult(
        exit=EXIT_ANCHORED,
        hypothesis_id=int(row.hypothesis_id),
        memory_id=memory_id,
    )


# ── shared write helpers ──────────────────────────────────────────────


def _attach(
    concept_store: "ConceptStore", concept: "Concept", memory_id: int | None,
) -> None:
    """One answer memory as an ``evidence`` edge. Upsert, so re-safe."""
    if memory_id is None:
        return
    try:
        concept_store.add_edge(
            ConceptEdge(
                src_type="memory",
                src_id=str(int(memory_id)),
                dst_type="concept",
                dst_id=str(int(concept.concept_id)),
                relation="evidence",
                polarity=1,
                strength=1.0,
            )
        )
    except Exception:
        log.debug("answer evidence edge failed", exc_info=True)


def _recount(concept_store: "ConceptStore", concept: "Concept") -> None:
    """Both counters from the edges, never incremented.

    Same reasoning as the L30c confirm path: an answer memory that
    already backs this concept is a repeat, not a second source.
    """
    try:
        edges = concept_store.evidence_of(concept.concept_id)
    except Exception:
        return
    concept.evidence_count = len(edges)
    concept.distinct_source_count = len(
        {(e.src_type, e.src_id) for e in edges}
    )


def _evidence_model_for(kind: str) -> str:
    from app.core.concepts.concept_kinds import get_kind

    spec = get_kind(str(kind))
    model = getattr(spec, "evidence_model", "set") if spec else "set"
    # A graduated concept's evidence is the answers it collected -- an
    # unordered set of memories. It cannot honestly claim a ``meta``
    # model (it has no base concepts) or a ``sequence`` (the answers are
    # not a chain), so those degrade rather than mint a row whose stored
    # shape contradicts its edges.
    return model if model in {"set", "recurring"} else "set"


def _rationale_for(row: "Hypothesis") -> str:
    base = str(row.rationale or "").strip()
    provenance = "Started as a hunch and was confirmed."
    return f"{base} {provenance}".strip() if base else provenance


def _record(
    event_store: "ConceptEventStore | None",
    event_type: str,
    concept: "Concept",
    row: "Hypothesis",
) -> None:
    if event_store is None:
        return
    try:
        from app.core.concepts.concept_event_store import ConceptEvent

        event_store.add(
            ConceptEvent(
                event_type=event_type,
                kind=str(concept.kind),
                subject=str(concept.subject),
                label=str(concept.label),
                confidence=float(getattr(concept, "confidence", 0.0) or 0.0),
                evidence_count=int(
                    getattr(concept, "evidence_count", 0) or 0
                ),
                distinct_source_count=int(
                    getattr(concept, "distinct_source_count", 0) or 0
                ),
                reason=(
                    f"Guessed it, then heard it confirmed "
                    f"{int(row.support_count)}x."
                )[:200],
                concept_id=int(concept.concept_id),
            )
        )
    except Exception:
        log.debug("graduation event write failed", exc_info=True)


def _now_iso() -> str:
    from app.core.infra import timephrase

    return timephrase.utcnow().isoformat()


__all__ = [
    "EVENT_GRADUATED",
    "EVENT_MERGED",
    "EXIT_ANCHORED",
    "EXIT_GRADUATED",
    "EXIT_MERGED",
    "GraduationResult",
    "graduate",
    "is_ready",
    "link_if_duplicate",
]

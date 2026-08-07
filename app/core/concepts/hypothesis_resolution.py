"""L30c — folding an answer back onto the belief it was about.

:mod:`app.core.concepts.answer_adjudicator` decides *what the reply
said*; this module decides *what that does to the concept*. Split from
the session mixin so the four write paths can be tested without a
SessionController, and kept target-agnostic in shape so Phase B's
hypothesis rows can sit beside these without a second implementation of
the same policy.

Each path mirrors an existing writer rather than inventing one:

- **Confirm** is L2's ``_reinforce``. An answer is a distinct source, so
  it becomes an ordinary memory with an ``evidence`` edge, the two
  evidence counters are recomputed from ``evidence_of`` and
  ``last_reinforced_at`` is stamped. Confidence and status are *not*
  touched: L3 recomputes confidence from the evidence it can see and
  owns the promotion decision, so a confirmed belief promotes through
  the ordinary gate on the next tick rather than by fiat here.
- **Deny** is L9's disproof step -- ``apply_contradiction_penalty`` plus
  a ``contradicts`` edge, exactly as the lifecycle worker writes one
  when its detector confirms counter-evidence. The edge is the part that
  matters: it is what makes the disconfirmation visible to L9 and L3
  afterwards, rather than an unexplained dip in a number.
- **Correct** takes the same penalty but writes **no** ``contradicts``
  edge, and stores the clarification as a memory. "Not quite -- it's
  more that I hate being still" has not falsified the belief, it has
  re-worded it, and the next L2 synthesis pass should be able to
  reinforce a better version from that memory. An edge here would push a
  refinable near-miss toward retirement instead.
- **Unclear** writes nothing at all.

**The invariant this bends.** Writing ``confidence`` outside the L3
lifecycle worker breaks the one-writer rule. There is one precedent --
``UserCorrectionWorker._propagate_to_concepts`` (F13) -- and this is the
second, deliberately: the user contradicting a belief to Aiko's face is
the same class of event as F13's demoted memory, and routing it through
L3 would mean waiting for a tick to apply something the user said out
loud. ``status`` stays strictly L3's in both cases, so the belief still
fades (or does not) on the lifecycle engine's judgement rather than on
one reply.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.core.concepts.answer_adjudicator import CONFIRM, CORRECT, DENY
from app.core.concepts.concept_lifecycle import apply_contradiction_penalty
from app.core.concepts.concept_store import ConceptEdge

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.concepts.hypothesis_store import Hypothesis, HypothesisStore


log = logging.getLogger("app.hypothesis_resolution")


#: ``concept_events.event_type`` is an open enum; these three record that
#: a belief was put to the user and what came back.
#:
#: Deliberately **not** added to
#: :data:`app.core.concepts.concept_drift.STRUCTURAL_EVENTS`. That set
#: picks the *decisive* point of a trajectory, and ``_shape_for`` maps
#: only the shapes it knows -- so a new member it cannot map would return
#: no finding at all and, worse, mask the genuine structural event behind
#: it. These reach the L17f diary and the L19 autobiography the correct
#: way instead: the drift worker re-reads any concept with a new event,
#: and the status move a denial eventually causes is what the classifier
#: turns into a learning event. Deciding what counts as learning is the
#: classifier's job, not this module's.
EVENT_CONFIRMED = "hypothesis_confirmed"
EVENT_CORRECTED = "hypothesis_corrected"
EVENT_DENIED = "hypothesis_denied"

_EVENT_FOR: dict[str, str] = {
    CONFIRM: EVENT_CONFIRMED,
    CORRECT: EVENT_CORRECTED,
    DENY: EVENT_DENIED,
}


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """What the verdict actually did, for logging and the debug surfaces."""

    verdict: str
    concept_id: int
    #: Confidence before and after. Equal on a confirm (L3 owns that move).
    confidence_before: float = 0.0
    confidence_after: float = 0.0
    evidence_added: bool = False
    contradiction_added: bool = False
    distinct_sources: int = 0


def apply_verdict(
    *,
    store: "ConceptStore",
    concept: "Concept",
    verdict: str,
    memory_id: int | None,
    penalty: float,
    event_store: "ConceptEventStore | None" = None,
    reason: str = "",
) -> ResolutionResult | None:
    """Apply one adjudicated answer to ``concept``.

    ``memory_id`` is the stored answer or clarification; ``None`` means
    the write failed upstream, in which case a confirm has nothing to
    attach and degrades to a no-op rather than silently claiming a source
    it does not have.

    Returns ``None`` when nothing was written. Never raises.
    """
    if verdict not in _EVENT_FOR:
        return None
    before = float(getattr(concept, "confidence", 0.0) or 0.0)
    try:
        if verdict == CONFIRM:
            if memory_id is None:
                log.debug("confirm with no answer memory; skipping write")
                return None
            result = _confirm(store, concept, int(memory_id), before)
        else:
            result = _disconfirm(
                store,
                concept,
                verdict=verdict,
                memory_id=memory_id,
                penalty=penalty,
                before=before,
            )
    except Exception:
        log.warning(
            "hypothesis resolution write failed: cid=%s verdict=%s",
            getattr(concept, "concept_id", "?"),
            verdict,
            exc_info=True,
        )
        return None
    _record_event(event_store, concept, verdict, result, reason)
    log.info(
        "hypothesis %s: cid=%s conf %.3f -> %.3f sources=%d",
        verdict,
        result.concept_id,
        result.confidence_before,
        result.confidence_after,
        result.distinct_sources,
    )
    return result


def _confirm(
    store: "ConceptStore",
    concept: "Concept",
    memory_id: int,
    before: float,
) -> ResolutionResult:
    """L2 ``_reinforce``, with the answer as the new source."""
    store.add_edge(
        ConceptEdge(
            src_type="memory",
            src_id=str(memory_id),
            dst_type="concept",
            dst_id=str(concept.concept_id),
            relation="evidence",
            polarity=1,
            strength=1.0,
        )
    )
    _recount(store, concept)
    concept.last_reinforced_at = _now_iso()
    # confidence / plasticity / status intentionally left to L3.
    store.update(concept)
    return ResolutionResult(
        verdict=CONFIRM,
        concept_id=int(concept.concept_id),
        confidence_before=before,
        confidence_after=before,
        evidence_added=True,
        distinct_sources=int(concept.distinct_source_count),
    )


def _disconfirm(
    store: "ConceptStore",
    concept: "Concept",
    *,
    verdict: str,
    memory_id: int | None,
    penalty: float,
    before: float,
) -> ResolutionResult:
    """The shared penalty, plus a ``contradicts`` edge only on a deny."""
    contradicted = False
    if verdict == DENY and memory_id is not None:
        store.add_edge(
            ConceptEdge(
                src_type="concept",
                src_id=str(concept.concept_id),
                dst_type="memory",
                dst_id=str(memory_id),
                relation="contradicts",
                polarity=-1,
                strength=1.0,
            )
        )
        contradicted = True
    concept.confidence = apply_contradiction_penalty(
        before,
        penalty=float(penalty),
        plasticity=float(getattr(concept, "plasticity", 0.5) or 0.5),
    )
    # Status stays L3's: a single "no" lowers conviction, and whether that
    # is enough to stop carrying the belief is a lifecycle judgement.
    store.update(concept)
    return ResolutionResult(
        verdict=verdict,
        concept_id=int(concept.concept_id),
        confidence_before=before,
        confidence_after=float(concept.confidence),
        contradiction_added=contradicted,
        distinct_sources=int(
            getattr(concept, "distinct_source_count", 0) or 0
        ),
    )


def _recount(store: "ConceptStore", concept: "Concept") -> None:
    """Recompute both evidence counters from the edges themselves.

    Recounted rather than incremented because the answer memory may
    already back this concept -- the user restating something they told
    Aiko months ago is a *repeat*, not a second source, and incrementing
    would let one belief manufacture its own breadth of grounding.
    """
    edges = store.evidence_of(concept.concept_id)
    concept.evidence_count = len(edges)
    concept.distinct_source_count = len(
        {(e.src_type, e.src_id) for e in edges}
    )


def _record_event(
    event_store: "ConceptEventStore | None",
    concept: "Concept",
    verdict: str,
    result: ResolutionResult,
    reason: str,
) -> None:
    if event_store is None:
        return
    try:
        from app.core.concepts.concept_event_store import ConceptEvent

        event_store.add(
            ConceptEvent(
                event_type=_EVENT_FOR[verdict],
                kind=str(concept.kind),
                subject=str(concept.subject),
                label=str(concept.label),
                confidence=float(result.confidence_after),
                evidence_count=int(
                    getattr(concept, "evidence_count", 0) or 0
                ),
                distinct_source_count=int(result.distinct_sources),
                reason=str(reason or "")[:200],
                concept_id=int(concept.concept_id),
            )
        )
    except Exception:
        log.debug("hypothesis concept event write failed", exc_info=True)


# ── the invented target (Phase B) ─────────────────────────────────────
# Same four verdicts, a different object underneath, and the differences
# are all consequences of one fact: a hypothesis has no evidence graph.
#
# A concept's confidence is *derived*, so L30c can move it a little and
# leave the verdict to L3's next tick. A hypothesis's credence is the
# only number it has, and nothing recomputes it later — so an answer has
# to be conclusive here or nowhere. Hence the asymmetries below: a single
# denial closes the row outright, where a denied concept merely loses
# conviction and keeps living.


@dataclass(frozen=True, slots=True)
class HypothesisResult:
    """What a verdict did to an invented row."""

    verdict: str
    hypothesis_id: int
    credence_before: float = 0.0
    credence_after: float = 0.0
    support_count: int = 0
    refute_count: int = 0
    status: str = ""
    restated: bool = False
    linked_concept_id: int | None = None


def apply_hypothesis_verdict(
    *,
    store: "HypothesisStore",
    row: "Hypothesis",
    verdict: str,
    memory_id: int | None,
    credence_step: float,
    concept_store: "ConceptStore | None" = None,
    embed: "Callable[[str], object] | None" = None,
    correction_text: str = "",
) -> HypothesisResult | None:
    """Apply one adjudicated answer to an invented hypothesis.

    Returns ``None`` when nothing was written. Never raises.

    On a **confirm** this also runs the duplicate lookup, because the
    earliest moment a link can be stamped is the first confirmation and
    the whole point of ``linked_concept_id`` is to be early (see
    :mod:`app.core.concepts.hypothesis_graduation`). The answer memory is
    remembered on the row as well: graduation happens on the *second*
    confirmation, and by then the first answer's id is not recoverable
    from anywhere else.

    A **deny** closes the row as ``refuted`` rather than merely lowering
    credence. Aiko made this up; being told no is the end of it, and
    keeping it open would only mean asking again about something already
    answered. The row survives as a row so the proposer's novelty check
    can see it and not re-invent the same wrong guess.

    A **correct** rewrites the statement to what the user actually said
    and keeps the row open. "Not quite, it's more that…" is the single
    most valuable thing this loop can hear: it hands Aiko a better version
    of her own guess, which is the difference between a hypothesis layer
    and a quiz.
    """
    if verdict not in _EVENT_FOR:
        return None
    before = float(getattr(row, "credence", 0.5) or 0.0)
    step = max(0.0, float(credence_step))
    restated = False
    try:
        if verdict == CONFIRM:
            _support(store, row, memory_id, before, step)
            _link(store, concept_store, row, memory_id)
        elif verdict == DENY:
            _refute(store, row, before, step)
        else:
            restated = _restate(
                store, row, before, step, correction_text, embed
            )
        result = _result(verdict, row, before, restated)
    except Exception:
        log.warning(
            "hypothesis row write failed: hid=%s verdict=%s",
            getattr(row, "hypothesis_id", "?"),
            verdict,
            exc_info=True,
        )
        return None
    log.info(
        "hypothesis %s: hid=%s credence %.3f -> %.3f support=%d refute=%d "
        "status=%s",
        verdict,
        result.hypothesis_id,
        result.credence_before,
        result.credence_after,
        result.support_count,
        result.refute_count,
        result.status,
    )
    return result


def _support(
    store: "HypothesisStore",
    row: "Hypothesis",
    memory_id: int | None,
    before: float,
    step: float,
) -> None:
    from app.core.concepts.hypothesis_store import STATUS_SUPPORTED

    row.support_count = int(row.support_count) + 1
    row.credence = _clamp(before + step)
    row.status = STATUS_SUPPORTED
    row.last_tested_at = _now_iso()
    if memory_id is not None and int(memory_id) not in row.answer_memory_ids:
        row.answer_memory_ids = [*row.answer_memory_ids, int(memory_id)]
    store.update(row)


def _refute(
    store: "HypothesisStore", row: "Hypothesis", before: float, step: float,
) -> None:
    from app.core.concepts.hypothesis_store import STATUS_REFUTED

    row.refute_count = int(row.refute_count) + 1
    row.credence = _clamp(before - step)
    row.last_tested_at = _now_iso()
    store.close(row, status=STATUS_REFUTED)


def _restate(
    store: "HypothesisStore",
    row: "Hypothesis",
    before: float,
    step: float,
    correction_text: str,
    embed: "Callable[[str], object] | None",
) -> bool:
    """Take the user's better wording, at half the credence penalty.

    Half, because a correction is partly a hit: Aiko was close enough
    that the user bothered to refine it rather than reject it. Full price
    would punish the most informative answer in the set.
    """
    restated = False
    body = " ".join(str(correction_text or "").split())[:200]
    if body and body.lower() != str(row.statement).strip().lower():
        row.statement = body
        restated = True
        if embed is not None:
            try:
                row.embedding = embed(body)  # type: ignore[assignment]
            except Exception:
                log.debug("restated hypothesis re-embed failed", exc_info=True)
    row.credence = _clamp(before - step / 2.0)
    row.last_tested_at = _now_iso()
    store.update(row)
    return restated


def _link(
    store: "HypothesisStore",
    concept_store: "ConceptStore | None",
    row: "Hypothesis",
    memory_id: int | None,
) -> None:
    """Stamp the link, if the belief already exists as a concept.

    Mutates ``row.linked_concept_id`` in place, which is why the result
    is built after this runs rather than before.
    """
    if concept_store is None:
        return
    from app.core.concepts.hypothesis_graduation import link_if_duplicate

    link_if_duplicate(
        hypothesis_store=store,
        concept_store=concept_store,
        row=row,
        memory_id=memory_id,
    )


def _result(
    verdict: str, row: "Hypothesis", before: float, restated: bool,
) -> HypothesisResult:
    return HypothesisResult(
        verdict=verdict,
        hypothesis_id=int(row.hypothesis_id),
        credence_before=before,
        credence_after=float(row.credence),
        support_count=int(row.support_count),
        refute_count=int(row.refute_count),
        status=str(row.status),
        restated=restated,
        linked_concept_id=row.linked_concept_id,
    )


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _now_iso() -> str:
    from app.core.infra import timephrase

    return timephrase.utcnow().isoformat()


__all__ = [
    "EVENT_CONFIRMED",
    "EVENT_CORRECTED",
    "EVENT_DENIED",
    "HypothesisResult",
    "ResolutionResult",
    "apply_hypothesis_verdict",
    "apply_verdict",
]

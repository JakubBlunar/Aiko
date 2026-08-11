"""L30b — turning an open question into an ask.

L30a gave Aiko a *register* for the beliefs she has not settled: the
hypothesis lane can hold one up mid-conversation as a musing. What it
could not do is close any of them. A candidate concept surfaced as "I
half-think this about you" stayed exactly as unsettled after being
mused about as before, because nothing in the loop ever went and found
out.

This worker is the producer half of the loop that does. During quiet
windows it picks the open question most worth resolving and queues a
``concept_hypothesis`` cue; the provider turns it into a real question
when a fitting moment arrives, and the L30c resolver folds the answer
back onto that specific belief.

**Which question.** :meth:`ConceptView.testable` supplies the pool, and
its age exclusion is the load-bearing part: answering adds a *distinct
source*, so it can only move a candidate held back on sources or
conviction. Better than half the live candidate pool (144 of 261 when
this shipped) already clears both and is simply waiting out its kind's
engaged-day floor, and asking about one of those spends a question to
change nothing — the user gets quizzed about something Aiko was going
to conclude anyway.

Ranking is ``importance * unsettledness``. The turn-relevance term that
:func:`hypothesis_score` uses is deliberately absent, because this runs
off-turn with no user text and therefore no query vector; the *provider*
applies the topical gate later, when there is a turn to be relevant to.

**No LLM.** The cue is a hint — the belief, plus the fact that it is
unverified — and Aiko phrases the question herself, the same
``render_notice_cue`` division the knowledge-gap notice uses. That
matters more here than elsewhere: a pre-written question about someone's
own character would land as a survey item however warmly worded.

**Two pools, separate budgets (Phase B).** Once inventions existed, the
worker had to be able to ask about them too — a guess nobody ever puts
to the user can only expire. They are selected in their own pass rather
than merged into one ranking, because they would lose every time: L32
importance blends a kind prior with the emotional charge of grounded
topic clusters, and an invention has no grounded memories, so it falls
back to the bare kind prior. Giving each origin its own budget avoids
inventing an exploration bonus out of nowhere to correct for that.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.cue_producer import CueProducer, StoreProvider
from app.core.proactive.idle_worker import WorkSignal

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.concept_importance import ImportanceContext
    from app.core.concepts.concept_store import Concept
    from app.core.concepts.concept_view import ConceptView
    from app.core.concepts.hypothesis_store import Hypothesis, HypothesisStore


log = logging.getLogger("app.concept_hypothesis_worker")


#: How many testable rows to pull before importance ranking. Importance
#: is applied after the read (the view has no affect context), so reading
#: exactly ``max_per_run`` rows would rank whatever the unsettledness
#: sort happened to put on top rather than the ones that matter.
_OVERFETCH = 12


def render_hypothesis_cue(label: str, subject: str) -> str:
    """The prompt line for one untested hunch.

    Composed at production time rather than in the provider because the
    ``cue_pool`` row is what the provider renders and what the Cues debug
    panel shows. States the belief and that it is unverified, and stops
    there -- how to raise it lives in the persona section this cue type
    hoists into T6.
    """
    who = {
        "aiko": "about yourself",
        "relationship": "about the two of you",
    }.get(str(subject or ""), "about them")
    return (
        f"You've had a hunch {who} that you've never actually checked: "
        f"\"{(label or '').strip()}\". You're not sure enough to say it "
        "as a fact -- but one honest answer would settle it."
    )


def render_invented_cue(statement: str, subject: str) -> str:
    """The prompt line for a hunch that rests on *nothing*.

    Deliberately not :func:`render_hypothesis_cue`. That wording says
    "you've had a hunch ... you've never checked", which quietly claims
    an observation behind it. An invention has none: Aiko made it up, and
    the honest framing is a wondering rather than a suspicion. Getting
    this wrong is not a style nit -- it would have her assert a private
    guess as something she noticed about the person she is talking to.
    """
    who = {
        "aiko": "about yourself",
        "relationship": "about the two of you",
        "world": "about how something works",
    }.get(str(subject or ""), "about them")
    return (
        f"Something you've wondered {who}, with nothing behind it but the "
        f"wondering: \"{(statement or '').strip()}\". You made it up -- so "
        "hold it lightly, and only if the moment invites it."
    )


class ConceptHypothesisWorker:
    """IdleWorker that queues "I could just ask" cues for open beliefs."""

    name = "concept_hypothesis"

    def __init__(
        self,
        *,
        concept_view_provider: Callable[[], "ConceptView | None"],
        importance_context_provider: (
            Callable[[list["Concept"]], "ImportanceContext | None"] | None
        ) = None,
        enabled_provider: Callable[[], bool] | None = None,
        hypothesis_store_provider: (
            Callable[[], "HypothesisStore | None"] | None
        ) = None,
        cue_store_provider: StoreProvider | None = None,
        interval_seconds: float = 1800.0,
        max_per_run: int = 1,
        min_sources: int = 1,
        min_unsettled: float = 0.22,
        promote_min_sources: int = 2,
        promote_min_confidence: float = 0.6,
        promote_min_age_days: float = 0.0,
    ) -> None:
        self._concept_view_provider = concept_view_provider
        self._importance_context_provider = importance_context_provider
        self._enabled_provider = enabled_provider
        self._hypothesis_store_provider = hypothesis_store_provider
        self._cues = CueProducer("concept_hypothesis", cue_store_provider)
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._max_per_run = max(1, int(max_per_run))
        self._min_sources = max(0, int(min_sources))
        self._min_unsettled = max(0.0, float(min_unsettled))
        self._promote_min_sources = int(promote_min_sources)
        self._promote_min_confidence = float(promote_min_confidence)
        self._promote_min_age_days = float(promote_min_age_days)
        # MCP debug: let the next run() ignore the already-asked set.
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        # Hard veto only; stock decides the rest. See
        # ``docs/idle-workers.md``.
        return self._enabled()

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from the shortfall of unasked questions."""
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        return self._cues.demand()

    def run(self) -> dict[str, Any]:
        force = self._force_next
        self._force_next = False
        if not self._enabled():
            return {"drafted": 0, "disabled": True}

        grounded = self._draft_grounded(force)
        invented = self._draft_invented(force)
        out = dict(grounded)
        out["drafted"] = int(grounded.get("drafted", 0)) + len(invented)
        if invented:
            out["invented"] = invented
        return out

    # ── the grounded pool (Phase A) ───────────────────────────────────

    def _draft_grounded(self, force: bool) -> dict[str, Any]:
        view = self._safe_view()
        if view is None or not getattr(view, "enabled", False):
            return {"drafted": 0, "no_concepts": True}

        try:
            rows = view.testable(
                limit=_OVERFETCH,
                min_sources=self._min_sources,
                min_unsettled=self._min_unsettled,
                promote_min_sources=self._promote_min_sources,
                promote_min_confidence=self._promote_min_confidence,
                promote_min_age_days=self._promote_min_age_days,
            )
        except Exception:
            log.debug("concept_hypothesis: testable() failed", exc_info=True)
            return {"drafted": 0, "no_candidate": True}
        if not rows:
            return {"drafted": 0, "no_candidate": True}

        from app.core.proactive.cue_store import normalise_subject

        # Broader than "still pending": a belief she already asked about
        # and got no answer to, or asked about a week ago and let expire,
        # is the *last* one to raise again. ``max_asks=1`` says the same
        # thing on the surfacing side; this stops a second row ever being
        # written for it.
        asked = set() if force else self._cues.spoken_for()
        fresh = [
            (concept, unsettled)
            for concept, unsettled in rows
            if normalise_subject(str(getattr(concept, "label", "") or ""))
            not in asked
        ]
        if not fresh:
            return {"drafted": 0, "all_asked": True}

        importance = self._importance_for([c for c, _ in fresh])
        ranked = sorted(
            (
                (concept, unsettled, importance(concept))
                for concept, unsettled in fresh
            ),
            key=lambda row: (
                -(row[1] * row[2]),
                int(getattr(row[0], "concept_id", 0) or 0),
            ),
        )

        drafted: list[dict[str, Any]] = []
        for concept, unsettled, weight in ranked[: self._max_per_run]:
            cue_id = self._publish(concept, unsettled, weight)
            if cue_id:
                drafted.append(
                    {
                        "concept_id": int(concept.concept_id),
                        "label": str(concept.label)[:200],
                        "importance": round(float(weight), 4),
                        "unsettled": round(float(unsettled), 4),
                        "cue_id": cue_id,
                    }
                )
        return {"drafted": len(drafted), "questions": drafted}

    # ── the invented pool (Phase B) ───────────────────────────────────

    def _draft_invented(self, force: bool) -> list[dict[str, Any]]:
        """Queue an ask for a guess Aiko made up.

        Filters, and why each one is here:

        - **live** — a refuted or graduated row is finished.
        - **unlinked** — a row pointing at a concept has already been
          answered *and* matched to a belief she holds; the concept
          speaks for it now, and asking again would be asking about
          something settled.
        - **unasked** — ``asked_count == 0``. The same ``max_asks=1``
          policy the grounded side runs on. The counter moves when the
          question is actually *put* (``_stamp_hypothesis_ask``), not when
          this cue is queued, because the shelf surfaces roughly one of
          these a day and a cue that is never rendered has asked nothing.
        - **unclaimed** — no cue already drafted from this row. This is
          what keeps the queue from filling with re-drafts of the same
          guess, a job ``asked_count`` used to do by accident while it was
          being spent at publish time.
        """
        store = self._hypothesis_store()
        if store is None:
            return []
        try:
            rows = store.list_by(live=True, linked=False)
        except Exception:
            log.debug("concept_hypothesis: list_by failed", exc_info=True)
            return []
        claimed = set() if force else self._cues.claimed_sources()
        pool = [
            row
            for row in rows
            if force
            or (
                int(getattr(row, "asked_count", 0) or 0) <= 0
                and f"hypothesis:{int(row.hypothesis_id)}" not in claimed
            )
        ]
        if not pool:
            return []

        from app.core.concepts.concept_hypothesis import unsettledness

        ranked = sorted(
            ((row, unsettledness(row)) for row in pool),
            key=lambda pair: (
                -(pair[1] * _kind_importance(pair[0].kind)),
                int(pair[0].hypothesis_id),
            ),
        )
        drafted: list[dict[str, Any]] = []
        for row, unsettled in ranked[: self._max_per_run]:
            cue_id = self._publish_invented(row, unsettled)
            if cue_id:
                drafted.append(
                    {
                        "hypothesis_id": int(row.hypothesis_id),
                        "statement": str(row.statement)[:200],
                        "credence": round(float(row.credence), 4),
                        "unsettled": round(float(unsettled), 4),
                        "cue_id": cue_id,
                    }
                )
        return drafted

    def _publish_invented(
        self, row: "Hypothesis", unsettled: float,
    ) -> int:
        statement = str(row.statement or "").strip()
        if not statement:
            return 0
        subject = str(row.subject or "user")
        cue_id = self._cues.publish(
            statement,
            render_invented_cue(statement, subject),
            payload={
                "target_type": "hypothesis",
                "target_id": int(row.hypothesis_id),
                # What stops a second cue being queued for the same guess,
                # now that ``asked_count`` no longer moves at publish time.
                "source_id": f"hypothesis:{int(row.hypothesis_id)}",
                "label": statement[:300],
                "kind": str(row.kind or ""),
                "subject": subject,
                "origin": "invented",
                "credence": round(float(row.credence), 4),
                "unsettled": round(float(unsettled), 4),
            },
            embedding=getattr(row, "embedding", None),
        )
        if not cue_id:
            return 0
        # ``asked_count`` is deliberately NOT bumped here. Publishing a cue
        # is not asking: the shelf renders a ``concept_hypothesis`` roughly
        # once a day by policy, so most queued cues are never surfaced at
        # all, and stamping on publish spent each row's single ask on a
        # question that was never put. That wedged the layer shut -- see
        # ``SessionController._stamp_hypothesis_ask``, which owns the
        # counter now, and the L30 note in the backlog.
        log.info(
            "invented hypothesis queued to ask: hid=%s unsettled=%.2f "
            "statement=%r cue=%s",
            row.hypothesis_id,
            unsettled,
            statement[:80],
            cue_id,
        )
        return cue_id

    def _hypothesis_store(self) -> "HypothesisStore | None":
        if self._hypothesis_store_provider is None:
            return None
        try:
            return self._hypothesis_store_provider()
        except Exception:
            return None

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to ignore the already-asked set."""
        self._force_next = True

    # ── gates / helpers ───────────────────────────────────────────────

    def _publish(
        self, concept: "Concept", unsettled: float, importance: float,
    ) -> int:
        label = str(getattr(concept, "label", "") or "").strip()
        if not label:
            return 0
        subject = str(getattr(concept, "subject", "user") or "user")
        payload = {
            # Phase A only ever writes ``concept``; the resolver routes on
            # this so Phase B can point the same loop at invented rows
            # without touching the adjudicator.
            "target_type": "concept",
            "target_id": int(getattr(concept, "concept_id", 0) or 0),
            "label": label[:300],
            "kind": str(getattr(concept, "kind", "") or ""),
            "subject": subject,
            "importance": round(float(importance), 4),
            "unsettled": round(float(unsettled), 4),
        }
        cue_id = self._cues.publish(
            label,
            render_hypothesis_cue(label, subject),
            payload=payload,
            # The concept's own label vector, so the cosine half of the
            # answer match costs nothing: the belief is already embedded.
            embedding=getattr(concept, "embedding", None),
        )
        log.info(
            "concept-hypothesis drafted: concept=%s importance=%.2f "
            "unsettled=%.2f label=%r cue=%s",
            payload["target_id"],
            importance,
            unsettled,
            label[:80],
            cue_id,
        )
        return cue_id

    def _importance_for(
        self, concepts: list["Concept"],
    ) -> Callable[["Concept"], float]:
        """An importance lookup for this batch, or a neutral one.

        Importance is a *lens*, not data (L32) -- when the affect join is
        unavailable the ranking degrades to pure unsettledness rather than
        the lane going quiet.
        """
        from app.core.concepts.concept_importance import IMPORTANCE_NEUTRAL

        ctx = None
        if self._importance_context_provider is not None:
            try:
                ctx = self._importance_context_provider(concepts)
            except Exception:
                log.debug(
                    "concept_hypothesis: importance context failed",
                    exc_info=True,
                )
        if ctx is None:
            return lambda _concept: IMPORTANCE_NEUTRAL

        def _lookup(concept: "Concept") -> float:
            try:
                return float(ctx.for_concept(concept))
            except Exception:
                return IMPORTANCE_NEUTRAL

        return _lookup

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _safe_view(self) -> "ConceptView | None":
        try:
            return self._concept_view_provider()
        except Exception:
            return None


def _kind_importance(kind: str) -> float:
    """The kind's L32 stakes prior, with no affect lift available.

    An invention has no grounded topic clusters, so the full importance
    join has nothing to read for it -- the bare prior is not a
    degradation here, it is all there honestly is.
    """
    from app.core.concepts.concept_importance import IMPORTANCE_NEUTRAL
    from app.core.concepts.concept_kinds import get_kind

    spec = get_kind(str(kind or ""))
    if spec is None:
        return IMPORTANCE_NEUTRAL
    return float(getattr(spec, "importance", IMPORTANCE_NEUTRAL))


__all__ = [
    "ConceptHypothesisWorker",
    "render_hypothesis_cue",
    "render_invented_cue",
]

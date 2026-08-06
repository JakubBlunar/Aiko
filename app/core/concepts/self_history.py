"""L19: Aiko's autobiography -- her own history as a traversable arc.

L17 records *that* a belief changed and why, one change at a time. This
module answers the question a person actually asks: "have you changed?",
"what were you like before?". It walks a subject's concepts, the learning
events on them, and the alias map that keeps merged-away beliefs reachable,
and returns eras of classified change.

Two design commitments:

- **Grounded, not generated.** Nothing here composes prose. Every entry
  carries its ``concept_id`` and the ids of the learning events behind it,
  so any line she says can be checked against the record. The narration
  happens in the model, from this data, and the data is the limit of what
  she can honestly claim.
- **A thin record says so.** :attr:`SelfHistoryArc.thin_record` is the
  whole reason this returns a structure rather than a string. A young or
  sparse history must make her say she does not remember, because the
  failure mode of a self-history feature is a confident invented past.

Both commitments rest on one invariant, shared with the L17b classifier:
**a belief she never held cannot be lost.** A concept that is no longer
carried and has no recorded ``loss`` behind it is left out of the arc
entirely -- it was a one-shot inference that decayed, or a row some
maintenance pass parked, and either way it is not something she changed
her mind about. Without that, a decay retune or a bulk sweep would fill
the story with hundreds of dated regrets and, worse, clear
``thin_record`` on the strength of them.

Read cost is deliberately flat in the number of concepts: the concept
mirror is already in memory, and the learning events and aliases are each
read **once** in bulk and grouped here. A per-concept query would be
hundreds of round trips on a mature store, which is not affordable on a
tool-call path.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.concepts.concept_learning_event_store import (
        ConceptLearningEventStore,
        LearningEvent,
    )
    from app.core.concepts.concept_store import Concept, ConceptStore


log = logging.getLogger("app.self_history")

# How a concept's arc is characterised. Ordered by how much it says: a
# belief that was replaced tells you more than one that merely persisted.
CHANGE_KINDS = ("flipped", "faded", "revived", "born", "settled")

# Shapes that mean the belief itself was superseded or rewritten, as
# opposed to merely appearing or fading.
_FLIP_SHAPES = frozenset({"succession", "relabel"})
_FADED_STATUSES = frozenset({"dormant", "retired"})

# Bucketing. A five-week history read in monthly eras is one era, which is
# not a story; a two-year history read in weeks is a hundred. So the bucket
# follows the span.
_WEEK_SPAN_DAYS = 70.0


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One belief's place in the story, with the ids that prove it."""

    concept_id: int
    label: str
    kind: str
    status: str
    change: str
    at: str = ""
    because: str = ""
    prior_label: str = ""
    learning_event_ids: tuple[int, ...] = ()
    absorbed_labels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "concept_id": self.concept_id,
            "label": self.label,
            "kind": self.kind,
            "status": self.status,
            "change": self.change,
            "at": self.at,
        }
        # Omit the optional fields when empty: this payload goes into a
        # tool result the model reads, and empty keys are noise it can
        # misread as "I looked and there was nothing".
        if self.because:
            out["because"] = self.because
        if self.prior_label:
            out["prior_label"] = self.prior_label
        if self.learning_event_ids:
            out["learning_event_ids"] = list(self.learning_event_ids)
        if self.absorbed_labels:
            out["absorbed_labels"] = list(self.absorbed_labels)
        return out


@dataclass(frozen=True, slots=True)
class HistoryEra:
    """A stretch of time and what happened to her understanding in it."""

    label: str
    start: str
    end: str
    entries: tuple[HistoryEntry, ...] = ()
    truncated: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "entries": [entry.as_dict() for entry in self.entries],
        }
        if self.truncated:
            out["truncated"] = self.truncated
        return out


@dataclass(frozen=True, slots=True)
class SelfHistoryArc:
    """The whole arc for one subject, oldest era first."""

    subject: str
    thin_record: bool = True
    span_days: float = 0.0
    first_evidence_at: str = ""
    total_concepts: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    eras: tuple[HistoryEra, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            # The field the caller must honour: a thin trail means she
            # says she does not remember, not that she improvises.
            "thin_record": self.thin_record,
            "span_days": round(float(self.span_days), 1),
            "first_evidence_at": self.first_evidence_at,
            "total_concepts": self.total_concepts,
            "counts": dict(self.counts),
            "eras": [era.as_dict() for era in self.eras],
        }


def _era_label(start: datetime, *, weekly: bool) -> str:
    # Built from parts rather than a ``%-d`` / ``%#d`` format string, which
    # is platform-specific.
    if weekly:
        return f"week of {start.strftime('%b')} {start.day}"
    return start.strftime("%B %Y")


def _bucket_start(moment: datetime, *, weekly: bool) -> datetime:
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if weekly:
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def _classify(
    concept: "Concept", events: "list[LearningEvent]"
) -> tuple[str, str, str]:
    """Return ``(change, prior_label, because)`` for one belief.

    Ordered by how much each outcome says. A belief that was superseded is
    the most informative thing that can happen to it, so a flip wins over
    the fade that a succession's losing side also technically shows.

    An empty ``change`` means **leave this belief out of the story**: it is
    no longer carried and nothing in the record says she ever held it. See
    the fade branch below.
    """
    newest_flip: "LearningEvent | None" = None
    newest_revival: "LearningEvent | None" = None
    newest_any: "LearningEvent | None" = None
    for event in events:
        newest_any = event
        if event.shape in _FLIP_SHAPES:
            newest_flip = event
        elif event.shape == "revival":
            newest_revival = event

    if newest_flip is not None:
        return (
            "flipped",
            str(newest_flip.old_label or ""),
            str(newest_flip.because or ""),
        )
    if concept.status in _FADED_STATUSES:
        for event in reversed(events):
            if event.shape == "loss":
                return "faded", "", str(event.because or "")
        # A fade with no recorded loss is not a belief she gave up -- the
        # L17b classifier only mints a loss for a belief that was actually
        # reinforced, so what is left here is a one-shot inference that
        # decayed, or a row a maintenance pass parked. Either way it never
        # entered the story, so it must not be narrated as an abandoned
        # conviction, and its volume must not talk ``thin_record`` into
        # claiming a history that isn't there.
        return "", "", ""
    if newest_revival is not None:
        return "revived", "", str(newest_revival.because or "")
    if newest_any is not None and newest_any.shape == "emergence":
        return "born", "", str(newest_any.because or "")
    # No recorded change and still standing: she has thought this all
    # along, which is its own answer to "what were you like before".
    return "settled", "", ""


def build_self_history(
    *,
    concept_store: "ConceptStore",
    learning_store: "ConceptLearningEventStore",
    subject: str = "aiko",
    now: datetime | None = None,
    min_entries: int = 3,
    max_entries_per_era: int = 8,
    max_eras: int = 12,
    event_limit: int = 2000,
) -> SelfHistoryArc:
    """Walk a subject's beliefs into eras of classified change.

    Includes **every** status, retired ones especially: a belief she no
    longer holds is the part of the story that answers "what were you like
    before". Merged-away rows are surfaced through their surviving concept
    via ``absorbed_labels``, since the row itself is gone.
    """
    moment = now or timephrase.utcnow()
    try:
        concepts = [
            c
            for c in concept_store.list_by(subject=subject)
            if int(c.concept_id) > 0
        ]
    except Exception:
        log.debug("self-history concept read failed", exc_info=True)
        return SelfHistoryArc(subject=subject)
    if not concepts:
        return SelfHistoryArc(subject=subject)

    events_by_concept = _group_events(learning_store, subject, event_limit)
    absorbed_by_concept = _group_aliases(learning_store, concepts)

    entries: list[tuple[datetime, HistoryEntry]] = []
    for concept in concepts:
        cid = int(concept.concept_id)
        events = events_by_concept.get(cid, [])
        change, prior, because = _classify(concept, events)
        if not change:
            continue
        born = _parse(concept.first_evidence_at) or _parse(
            events[0].created_at if events else None
        )
        if born is None:
            # No datable origin means it cannot be placed in an era, and a
            # guessed date in a self-history is worse than an omission.
            continue
        # A change is dated when it happened, not when the belief started:
        # "I used to think X" belongs in the era where it stopped.
        at = born
        if change != "settled" and events:
            at = _parse(events[-1].created_at) or born
        entries.append(
            (
                at,
                HistoryEntry(
                    concept_id=cid,
                    label=str(concept.label or ""),
                    kind=str(concept.kind or ""),
                    status=str(concept.status or ""),
                    change=change,
                    at=at.isoformat(),
                    because=because,
                    prior_label=prior,
                    learning_event_ids=tuple(
                        int(e.event_id) for e in events
                    )[:6],
                    absorbed_labels=tuple(absorbed_by_concept.get(cid, ()))[
                        :4
                    ],
                ),
            )
        )

    if not entries:
        return SelfHistoryArc(
            subject=subject, total_concepts=len(concepts)
        )

    entries.sort(key=lambda pair: pair[0])
    earliest = entries[0][0]
    span_days = max(0.0, (moment - earliest).total_seconds() / 86400.0)
    weekly = span_days <= _WEEK_SPAN_DAYS

    eras = _build_eras(
        entries,
        weekly=weekly,
        max_entries_per_era=max_entries_per_era,
        max_eras=max_eras,
    )
    counts = Counter(entry.change for _at, entry in entries)
    # The floor is on *substantive* entries: a store full of beliefs she
    # has simply held is not a history of changing.
    substantive = sum(
        n for change, n in counts.items() if change != "settled"
    )
    return SelfHistoryArc(
        subject=subject,
        thin_record=substantive < max(1, int(min_entries)),
        span_days=span_days,
        first_evidence_at=earliest.isoformat(),
        total_concepts=len(concepts),
        counts=dict(counts),
        eras=eras,
    )


def _group_events(
    learning_store: "ConceptLearningEventStore",
    subject: str,
    event_limit: int,
) -> dict[int, "list[LearningEvent]"]:
    """One bulk read, grouped in memory, oldest-first per concept.

    Both endpoints are indexed: a succession is part of the story of the
    belief that faded *and* the one that replaced it.
    """
    try:
        rows = learning_store.list(subject=subject, limit=event_limit)
    except Exception:
        log.debug("self-history learning read failed", exc_info=True)
        return {}
    grouped: dict[int, list[Any]] = defaultdict(list)
    for event in rows:
        for cid in (event.concept_id, event.prior_concept_id):
            if cid:
                grouped[int(cid)].append(event)
    for cid in grouped:
        grouped[cid].sort(key=lambda e: (e.created_at, e.event_id))
    return dict(grouped)


def _group_aliases(
    learning_store: "ConceptLearningEventStore",
    concepts: "list[Concept]",
) -> dict[int, list[str]]:
    """Labels of beliefs that were folded into each surviving concept."""
    live = {int(c.concept_id) for c in concepts}
    try:
        aliases = learning_store.list_aliases()
    except Exception:
        return {}
    grouped: dict[int, list[str]] = defaultdict(list)
    for alias in aliases:
        canonical = int(alias.canonical_id)
        if canonical in live and alias.absorbed_label:
            grouped[canonical].append(str(alias.absorbed_label))
    return dict(grouped)


def _build_eras(
    entries: list[tuple[datetime, HistoryEntry]],
    *,
    weekly: bool,
    max_entries_per_era: int,
    max_eras: int,
) -> tuple[HistoryEra, ...]:
    buckets: dict[datetime, list[HistoryEntry]] = defaultdict(list)
    for at, entry in entries:
        buckets[_bucket_start(at, weekly=weekly)].append(entry)

    ordered = sorted(buckets.items())
    if len(ordered) > max_eras:
        # Keep the most recent eras: "what were you like before" is asked
        # about a past that is still in living memory for the asker.
        ordered = ordered[-max_eras:]

    cap = max(1, int(max_entries_per_era))
    eras: list[HistoryEra] = []
    for start, bucket in ordered:
        # Within an era, the changes that say the most come first, so a
        # truncated era keeps its most informative lines.
        bucket.sort(
            key=lambda e: (
                CHANGE_KINDS.index(e.change)
                if e.change in CHANGE_KINDS
                else len(CHANGE_KINDS),
                e.at,
            )
        )
        kept = tuple(bucket[:cap])
        span = timedelta(days=7) if weekly else timedelta(days=31)
        eras.append(
            HistoryEra(
                label=_era_label(start, weekly=weekly),
                start=start.isoformat(),
                end=(start + span).isoformat(),
                entries=kept,
                truncated=max(0, len(bucket) - len(kept)),
            )
        )
    return tuple(eras)


__all__ = [
    "CHANGE_KINDS",
    "HistoryEntry",
    "HistoryEra",
    "SelfHistoryArc",
    "build_self_history",
]

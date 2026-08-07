"""L30 Phase B — letting an invented row ride the L30a surfacing lane.

The lane in ``inner_life_part1`` was written for ``Concept`` rows: it
reads ``label`` / ``subject`` / ``kind``, keys habituation and dedupe on
``concept_id``, and hands the row to :func:`unsettledness`. An invented
hypothesis is a different object with different column names, and there
were two ways to let it in.

Teaching every reader both shapes was the wrong one — that is exactly the
"one missed check puts an invention into the prompt as a belief" failure
the separate table exists to prevent. So the row is adapted *here*, at
the single point where an invention enters a surfacing path, into
something the lane can read without knowing it is not a concept.

The negative id
---------------
:attr:`InventedRow.concept_id` is ``-hypothesis_id``. It looks like a
hack and earns its place: habituation state and the lane's
already-surfaced set are both keyed by concept id, and a negative key
cannot collide with a real concept while still giving each invented row
its *own* habituation slot. That is what makes the two lane slots rotate
independently rather than one pool's freshness suppressing the other's.

What is deliberately not exposed
--------------------------------
``confidence`` and ``distinct_source_count``. A caller reaching for those
on an invention is asking a question with no honest answer — an invented
belief has no evidence graph, and answering with credence would let it be
ranked as though it did. :func:`unsettledness` detects the shape by
``credence`` and reads the right pair; anything else gets ``0`` and can
be found by grep rather than by a surprising number in the prompt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.hypothesis_store import Hypothesis, HypothesisStore


log = logging.getLogger("app.hypothesis_lane")

#: Marks a lane row's provenance. The lane offers at most one candidate
#: per origin, so an invention cannot be crowded out by evidenced
#: candidates it would lose to on importance every time.
ORIGIN_GROUNDED = "grounded"
ORIGIN_INVENTED = "invented"


@dataclass(frozen=True, slots=True)
class InventedRow:
    """An invented hypothesis wearing just enough of a concept's shape."""

    hypothesis_id: int
    label: str
    subject: str
    kind: str
    credence: float
    support_count: int
    embedding: Any
    origin: str = ORIGIN_INVENTED

    @property
    def concept_id(self) -> int:
        """A lane key that cannot collide with a real concept."""
        return -int(self.hypothesis_id)

    @property
    def confidence(self) -> float:
        """Always ``0.0``. See the module docstring."""
        return 0.0

    @property
    def distinct_source_count(self) -> int:
        """Always ``0``. See the module docstring."""
        return 0


def adapt(row: "Hypothesis") -> InventedRow:
    return InventedRow(
        hypothesis_id=int(row.hypothesis_id),
        label=str(row.statement or "").strip(),
        subject=str(row.subject or "user"),
        kind=str(row.kind or ""),
        credence=float(row.credence or 0.0),
        support_count=int(row.support_count or 0),
        embedding=row.embedding,
    )


def nearest_invented(
    store: "HypothesisStore | None",
    embedding: "np.ndarray | None",
    *,
    k: int = 6,
    min_sim: float = 0.0,
) -> list[tuple[InventedRow, float]]:
    """Up to ``k`` ``(row, cosine)`` pairs for the lane, nearest first.

    Only **live and unlinked** rows are offered. The linked exclusion is
    the surfacing half of the duplicate race: while a hypothesis sits at
    one confirmation, L2 may already have minted a concept from its own
    answer memory, and both would then render as open questions about one
    belief. A stamped ``linked_concept_id`` says the concept speaks for it
    now, so the guess goes quiet.

    Never raises; a failed read means the invented slot is simply empty
    this turn.
    """
    if store is None or embedding is None or k <= 0:
        return []
    try:
        hits = store.nearest(embedding, k=int(k) * 3, live_only=True)
    except Exception:
        log.debug("invented lane: nearest failed", exc_info=True)
        return []
    out: list[tuple[InventedRow, float]] = []
    for row, sim in hits:
        if row.linked_concept_id is not None:
            continue
        if float(sim) < float(min_sim):
            continue
        adapted = adapt(row)
        if not adapted.label:
            continue
        out.append((adapted, float(sim)))
        if len(out) >= int(k):
            break
    return out


def one_per_origin(candidates: list) -> list:
    """Keep the best candidate of each origin, in the given order.

    The "one per origin" rule, applied *before* the context budget rather
    than after it, so a slot the budget grants is never spent on a second
    row of an origin already represented. Competing on score alone would
    bury the inventions: L32 importance blends a kind prior with the
    emotional charge of grounded topic clusters, and an invention has no
    grounded memories, so it falls back to the bare prior and loses to
    evidenced candidates nearly every time. One slot each sidesteps that
    without inventing an exploration bonus from nowhere.
    """
    seen: set[str] = set()
    kept: list = []
    for candidate in candidates:
        origin = getattr(
            getattr(candidate, "payload", None), "origin", ORIGIN_GROUNDED,
        )
        if origin in seen:
            continue
        seen.add(str(origin))
        kept.append(candidate)
    return kept


__all__ = [
    "ORIGIN_GROUNDED",
    "ORIGIN_INVENTED",
    "InventedRow",
    "adapt",
    "nearest_invented",
    "one_per_origin",
]

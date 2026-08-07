"""The one bar for "is this belief already in the graph?".

Extracted from the L2 synthesis worker's ``_find_duplicate`` when L30's
graduation path became a second caller. Two independently-tuned dedupe
thresholds would be a slow-motion bug: they would agree for months, and
then one of them would be adjusted for a good local reason and the graph
would quietly start accepting near-twins from one entry path but not the
other. There is one number and one query, here.

Where the number came from
--------------------------
:data:`DEDUPE_COS` was measured over a month of real use, not chosen.
Pairs from **0.86** up are restatements of the same belief ("the
architectural refinement of Aiko's memory systems as a ritualistic
anchor" against "the architectural integrity of Aiko's memory system as a
prerequisite"); 0.82 and below is where genuinely different subjects
start sharing a sentence template. Nothing adjudicates a hit on this path
— it silently reinforces instead of creating — so the bar sits at the
conservative end of the twin range and leaves the ``[0.78, 0.86)`` band
to the :class:`~app.core.concepts.concept_consolidation_worker.ConceptConsolidationWorker`,
which does adjudicate.

``status=None`` is deliberate
-----------------------------
The lookup matches ``retired`` and ``dormant`` rows too. A belief Aiko
used to hold and let fade is still *the same belief*; arriving at it
again should revive that concept and its history rather than fork a
second row that starts from nothing.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.concept_store import Concept, ConceptStore


log = logging.getLogger("app.concept_dedupe")

#: Cosine at or above which two labels are the same belief. See the
#: module docstring for the measurement behind it.
DEDUPE_COS = 0.86

#: How many neighbours to pull. Only the top one can be a duplicate; the
#: rest are read for the ``top_cosine`` the caller derives novelty from.
_K = 5


def find_duplicate(
    store: "ConceptStore",
    vec: Any,
    *,
    subject: str,
    kind: str | None,
    threshold: float = DEDUPE_COS,
    k: int = _K,
) -> tuple["Concept | None", float]:
    """``(duplicate_or_none, top_cosine)`` for a proposed belief.

    The top cosine comes back even when it is below the bar, so a caller
    can derive a discovery ``novelty`` from the same lookup rather than
    paying for a second query.

    ``kind=None`` widens the search across kinds within the subject, and
    that is exactly what the hypothesis path wants. A guess carries a
    kind the proposer picked while speculating, which need not be the
    kind L2 chose for the same belief when it derived it from evidence —
    filtering on it would miss the duplicate and fork the graph on a
    taxonomy disagreement. The synthesis path keeps the kind filter,
    because there the kind was chosen from the evidence itself.

    Never raises: a failed lookup reports "no duplicate", which risks one
    extra row rather than losing the write entirely.
    """
    try:
        hits = store.nearest(
            vec, subject=subject, kind=kind, status=None, k=int(k),
        )
    except Exception:
        log.debug("concept nearest failed", exc_info=True)
        return None, 0.0
    if not hits:
        return None, 0.0
    top_sim = float(hits[0][1])
    if top_sim >= float(threshold):
        return hits[0][0], top_sim
    return None, top_sim


__all__ = ["DEDUPE_COS", "find_duplicate"]

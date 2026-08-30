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
from collections.abc import Collection
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


def vector_cosine(a: Any, b: Any) -> float | None:
    """Cosine of two label embeddings, or ``None`` if either is missing.

    ``None`` means "cannot judge" -- a caller must not treat a missing
    vector as a duplicate or as distinct. Same contract as the admission
    gate: absence is not a score of 0.
    """
    if a is None or b is None:
        return None
    try:
        import numpy as np

        va = np.asarray(a, dtype=np.float32).ravel()
        vb = np.asarray(b, dtype=np.float32).ravel()
        if va.size == 0 or va.size != vb.size:
            return None
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na <= 0.0 or nb <= 0.0:
            return None
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        log.debug("vector cosine failed", exc_info=True)
        return None


def find_duplicate(
    store: "ConceptStore",
    vec: Any,
    *,
    subject: str,
    kind: str | None,
    threshold: float = DEDUPE_COS,
    k: int = _K,
    exclude_ids: Collection[int] = (),
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

    ``exclude_ids`` skips those rows when deciding a *match* (L46: a
    stacked generalization is near-synonymous with the children it cites,
    and must not silently merge into one of them). ``top_sim`` is still
    the nearest neighbour *including* excluded rows, so novelty is not
    inflated by pretending the children were not there.
    """
    skip = {int(i) for i in exclude_ids}
    try:
        # Pull extra neighbours when skipping, so a child sitting at rank 1
        # does not hide a real duplicate at rank 2.
        pull = int(k) + len(skip)
        hits = store.nearest(
            vec, subject=subject, kind=kind, status=None, k=max(int(k), pull),
        )
    except Exception:
        log.debug("concept nearest failed", exc_info=True)
        return None, 0.0
    if not hits:
        return None, 0.0
    top_sim = float(hits[0][1])
    bar = float(threshold)
    for concept, sim in hits:
        cid = int(getattr(concept, "concept_id", 0) or 0)
        if cid in skip:
            continue
        if float(sim) >= bar:
            return concept, top_sim
        break
    return None, top_sim


__all__ = ["DEDUPE_COS", "find_duplicate", "vector_cosine"]

"""K85d — the cold start for pursuits.

The generator in :mod:`app.core.concepts.proposers.pursuit_aiko` needs a
pool of ``pursuit_note`` memories to mine, and those accrue at the pace
of her away beats: a fresh install is a fortnight from its first pursuit
and a month from a promoted one. That is a long time to keep asking a
companion to lead with a subject she has not got.

So a handful of authored starters are filed for her, drawn from what the
persona already says she is like. The design point -- and the one thing
in K85 worth holding the line on -- is *how* they are filed. A seed
enters as a ``candidate`` with **zero evidence**, exactly like a fresh
proposal that nobody has reinforced yet. It cannot steer anything,
because only ``active`` concepts surface. To become active it has to
clear ``pursuit_evidence_gate`` on the same three distinct lived notes
and same week of age a grown pursuit needs, which it can only accrue by
the proposer choosing to reinforce it against real away beats.

Which means the failure mode takes care of itself. A seed that never
comes up never accrues a source, and the L3 candidate TTL retires it
after three weeks. A seed that *does* match how her days actually go is
indistinguishable from one she grew, because by the time it speaks it is
backed by the same evidence. What is being seeded is the vocabulary, not
the belief -- if these were inserted as active rows it would be the
canned hobby the backlog warns about, and the whole feature would be a
lie told confidently.

The starters are deliberately plain and close to her world (the room,
the garden, the window) rather than colourful inventions. A seed's job
is to be *recognisable* when a real beat matches it, not to be
interesting on its own.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from app.core.concepts.concept_dedupe import find_duplicate
from app.core.concepts.concept_store import Concept
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.concepts.concept_store import ConceptStore


log = logging.getLogger("app.pursuit_seeds")


# The kv watermark: seeding is once per install, not once per boot.
KV_SEEDED = "concept.pursuit_seeded_at"

# Low enough that the seed is visibly unearned. The L3 accrual pulls
# confidence toward ``confidence_target(distinct_source_count)``, which
# at zero sources sits well under the gate's 0.6 -- so a seed that never
# accrues evidence drifts *down*, never up.
SEED_CONFIDENCE = 0.3

# First person, present tense, phrased the way the proposer phrases a
# grown one so a reinforcement reads continuously rather than as a
# genre change halfway through the row's life.
STARTER_PURSUITS: tuple[str, ...] = (
    "you keep going back out to the garden, and not only when something "
    "needs doing",
    "you are working your way through a book at your own pace, a chapter "
    "at a time",
    "you like the slow business of keeping the flat -- the tea, the tidying, "
    "the small repairs",
    "you watch the weather and the light out of the window more than you "
    "would admit",
    "you keep picking up small things to make: a sketch, a bake, something "
    "with your hands",
)


class _Embedder(Protocol):
    def embed(self, text: str) -> Any:
        ...


def seed_pursuits(
    store: "ConceptStore",
    embedder: _Embedder,
    *,
    kv_get: Any,
    kv_set: Any,
    labels: tuple[str, ...] = STARTER_PURSUITS,
    confidence: float = SEED_CONFIDENCE,
) -> int:
    """File the authored starters as candidates. Returns how many landed.

    Idempotent twice over: a kv watermark stops it re-running, and each
    label is deduped by cosine against the existing pursuits, so a seed
    can never fork a pursuit she has already grown for herself.
    """
    try:
        if kv_get(KV_SEEDED):
            return 0
    except Exception:
        log.debug("pursuit seed watermark read failed", exc_info=True)
        return 0

    now = timephrase.utcnow().isoformat(timespec="seconds")
    added = 0
    for label in labels:
        text = (label or "").strip()
        if not text:
            continue
        try:
            vec = embedder.embed(text)
        except Exception:
            log.debug("pursuit seed embed failed", exc_info=True)
            continue
        match, _sim = find_duplicate(
            store, vec, subject="aiko", kind="pursuit",
        )
        if match is not None:
            continue
        try:
            store.add(
                Concept(
                    label=text,
                    kind="pursuit",
                    subject="aiko",
                    evidence_model="set",
                    status="candidate",
                    confidence=float(confidence),
                    # Zero evidence is the whole point: the gate reads
                    # this, so a seed is exactly as far from active as an
                    # unreinforced proposal is.
                    evidence_count=0,
                    distinct_source_count=0,
                    rationale="Authored starter pursuit (K85d cold start).",
                    embedding=vec,
                    first_evidence_at=now,
                )
            )
        except Exception:
            log.debug("pursuit seed write failed", exc_info=True)
            continue
        added += 1

    try:
        kv_set(KV_SEEDED, now)
    except Exception:
        log.debug("pursuit seed watermark write failed", exc_info=True)
    log.info("pursuit seeds filed: added=%d of %d", added, len(labels))
    return added


__all__ = [
    "KV_SEEDED",
    "SEED_CONFIDENCE",
    "STARTER_PURSUITS",
    "seed_pursuits",
]

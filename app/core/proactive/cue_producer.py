"""The two halves of a pooled cue: producing it, and surfacing it.

Seven workers write cues and seven providers render them, and before the
pool each pair invented its own bookkeeping -- a journal ring here, a
``surfaced_keys`` set there, a per-topic cooldown map beside it. The logic
was the same every time and different enough each time that no two could
be compared. This module is the shared version.

:class:`CueProducer` is the worker's side. It answers "how much stock do I
have" (which is the whole of ``demand()`` for these workers), "which
subjects are already spoken for" (which replaces the per-topic cooldown
maps), and "queue this one".

:func:`pick_pool_cue` is the provider's side: hand it the pool and a
relevance test and it returns the cue to render, or ``None``.

Both degrade to ``None`` when no store is available, so a worker or
provider constructed without one behaves exactly as it did before the
pool existed. That fallback is what lets the seven migrate one at a time.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.core.proactive.cue_accounting import CuePolicy, policy_for
from app.core.proactive.idle_worker import WorkSignal, pressure_from_deficit

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.proactive.cue_store import CueRow, CueStore


log = logging.getLogger("app.cue_producer")


StoreProvider = Callable[[], "CueStore | None"]


class CueProducer:
    """Pool-backed inventory for one cue type.

    Held by the worker rather than inherited, because the five topic-gated
    workers already have deep inheritance-free constructors and a member is
    easier to leave unwired in a test than a base class is.
    """

    def __init__(
        self,
        cue_type: str,
        store_provider: StoreProvider | None = None,
        *,
        inventory_target: int | None = None,
    ) -> None:
        self._cue_type = str(cue_type)
        self._store_provider = store_provider
        self._policy = policy_for(cue_type) or CuePolicy(name=str(cue_type))
        # Only ``curiosity_seed`` overrides this, from the config key that
        # predates the registry.
        self._inventory_target = (
            max(1, int(inventory_target))
            if inventory_target is not None
            else self._policy.inventory_target
        )

    @property
    def cue_type(self) -> str:
        return self._cue_type

    @property
    def policy(self) -> CuePolicy:
        return self._policy

    @property
    def inventory_target(self) -> int:
        return self._inventory_target

    def store(self) -> "CueStore | None":
        if self._store_provider is None:
            return None
        try:
            return self._store_provider()
        except Exception:
            return None

    # ── the worker's demand() ─────────────────────────────────────────

    def stock(self) -> int:
        """Pending cues of this type. Cheap enough for a per-tick probe."""
        store = self.store()
        if store is None:
            return 0
        return store.count_pending(self._cue_type)

    def stock_rows(
        self, *, limit: int = 50, with_embedding: bool = False,
    ) -> list["CueRow"]:
        """The pending cues themselves, for a worker that reads its own stock.

        Only ``curiosity_seed`` needs this -- it dedupes new candidates
        against the vectors of the ones already queued, so a count is not
        enough.
        """
        store = self.store()
        if store is None:
            return []
        try:
            return store.pending(
                self._cue_type,
                limit=max(1, int(limit)),
                with_embedding=with_embedding,
            )
        except Exception:
            log.debug(
                "cue stock read failed: type=%s", self._cue_type, exc_info=True,
            )
            return []

    def demand(self, *, needs_llm: bool = False) -> WorkSignal | None:
        """Pressure from the shortfall against ``inventory_target``.

        ``None`` when there is no pool to count, which drops the worker
        back to plain interval scheduling.
        """
        if self.store() is None:
            return None
        have = self.stock()
        want = self._inventory_target
        return WorkSignal(
            pressure=pressure_from_deficit(have, want=want),
            reason=f"{have}/{want} stocked",
            needs_llm=needs_llm,
        )

    # ── production ────────────────────────────────────────────────────

    def spoken_for(self, *, within_hours: float | None = None) -> set[str]:
        """Subjects a cue already exists for, in any state.

        The pool's answer to the per-topic cooldown maps. Broader than
        those were, deliberately: a subject that was *used*, or that
        expired because nobody wanted it, is at least as poor a candidate
        for a fresh cue as one still sitting pending.
        """
        store = self.store()
        if store is None:
            return set()
        window = (
            float(within_hours)
            if within_hours is not None
            else self._policy.ttl_hours * 2.0
        )
        return store.recent_subjects(self._cue_type, within_hours=window)

    def publish(
        self,
        subject: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        embedding: Any = None,
    ) -> int:
        """Queue one cue under this type's TTL. Returns the row id or 0."""
        store = self.store()
        if store is None:
            return 0
        return store.add(
            self._cue_type,
            subject,
            text,
            payload=payload,
            ttl_hours=self._policy.ttl_hours,
            embedding=embedding,
        )


def pick_pool_cue(
    store: "CueStore | None",
    cue_type: str,
    *,
    relevant: Callable[[dict[str, Any]], bool] | None = None,
    force: bool = False,
    limit: int = 8,
    allow_first_claim: bool = True,
) -> "CueRow | None":
    """The best pending cue of this type that fits the moment.

    ``relevant`` is the provider's own gate, applied to the cue's payload
    -- the topic-overlap tests these providers already run. ``force``
    bypasses it for the MCP debug surfaces, which is the same escape hatch
    the ring-based versions had.

    ``allow_first_claim=False`` restricts the pick to rows that have
    already surfaced at least once, which is how the per-type surfacing
    cadence is enforced without also delaying a retry. It is a filter
    rather than a check on the winner because ``pending`` sorts unseen
    cues first: rejecting the row it returns would hide a legitimate
    retry sitting directly behind a fresh cue that is merely early.

    Does **not** mark the cue surfaced. The provider does that, after it
    has decided the cue is actually going into the prompt, because a cue
    inspected and rejected must not spend one of its two surfacings.
    """
    if store is None:
        return None
    try:
        # With embeddings: the row is handed to post-turn matching after
        # the turn, and re-reading it there would mean a second query per
        # surfaced cue for a few kilobytes we already have in hand.
        rows = store.pending(
            cue_type, limit=max(1, int(limit)), with_embedding=True,
        )
    except Exception:
        log.debug("cue pool read failed: type=%s", cue_type, exc_info=True)
        return None
    for row in rows:
        if not allow_first_claim and row.surfaced_count <= 0:
            continue
        if force or relevant is None or relevant(row.payload):
            return row
    return None


__all__ = ["CueProducer", "pick_pool_cue"]

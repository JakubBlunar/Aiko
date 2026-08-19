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
relevance test and it returns the cue to render, or ``None`` -- wrapped in
a :class:`CuePick` that also says what the walk did, because "no cue" has
several causes and G4 counts them separately.

Both degrade to ``None`` when no store is available, so a worker or
provider constructed without one behaves exactly as it did before the
pool existed. That fallback is what lets the seven migrate one at a time.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.proactive import topic_match
from app.core.proactive.cue_accounting import (
    PICK_OLDEST,
    CuePolicy,
    policy_for,
)
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

    def claimed_sources(self, *, within_hours: float | None = None) -> set[str]:
        """``payload.source_id`` values already drafted from, in any state.

        For the workers that draft from a memory row rather than from a
        topic label. Their own journal ring is a short window and the
        source came back the moment it rotated out.
        """
        store = self.store()
        if store is None:
            return set()
        window = (
            float(within_hours)
            if within_hours is not None
            else self._policy.ttl_hours * 2.0
        )
        try:
            return store.claimed_source_ids(self._cue_type, within_hours=window)
        except Exception:
            log.debug(
                "cue source read failed: type=%s", self._cue_type, exc_info=True,
            )
            return set()

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


@dataclass(frozen=True, slots=True)
class CuePick:
    """The chosen cue, plus what the walk had to step over to not find one.

    An empty pick used to be a bare ``None``, and the caller reconstructed
    the reason from state it could still see: "did the predicate reject
    anything" and "is the type cadence-blocked". Those two overlap, and
    the overlap was scored wrong. When a cadence hold restricts the pick
    to retries, it removes most of the shelf *before* the predicate sees
    it; the one or two survivors then fail a topic test, and the turn was
    recorded as ``topic_miss`` -- an **eligible** decline -- when the cue
    had in fact been under its own clock, which is **ineligible**.

    That mislabelling only ever pushes one way. ``topic_miss`` is by far
    the largest bucket in the ledger (1,873 of 1,981 eligible declines),
    so every turn wrongly in it inflates the denominator that reach is
    measured against, and makes five cue types look more starved than
    they are. Hence the counts: the caller no longer has to infer.

    ``considered`` is also the only record anywhere of how deep the shelf
    was on a given turn. Nothing else keeps it -- ``state``, ``not_before``
    and ``surfaced_count`` are all last-value-only, so the availability
    history is unrecoverable after the fact. It is logged for that reason.
    """

    row: "CueRow | None" = None
    # Rows the query actually returned, i.e. the live shelf up to ``limit``.
    considered: int = 0
    # Skipped before the predicate ran, because a cadence hold restricted
    # this pick to cues that had already had a showing.
    held_for_cadence: int = 0
    # Rows the provider's own predicate looked at and refused.
    rejected: int = 0
    # How many cleared admission, i.e. the set the winner was chosen from.
    admitted: int = 0
    # Which arm admitted the winner, and how close it was to the live
    # message. ``None`` when there was nothing to compare against.
    arm: str = topic_match.ARM_NONE
    cosine: float | None = None

    def __bool__(self) -> bool:
        return self.row is not None


def pick_pool_cue(
    store: "CueStore | None",
    cue_type: str,
    *,
    relevant: Callable[[dict[str, Any]], bool] | None = None,
    force: bool = False,
    limit: int = 8,
    allow_first_claim: bool = True,
    user_vec: Any = None,
    min_cosine: float | None = None,
) -> CuePick:
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

    **"Best" used to mean "first".** With ``user_vec`` supplied this walks
    the whole window and returns the admitted row whose subject sits
    closest to the live message, instead of whichever one the
    surfacings-then-recency order happened to put in front. H43 is why:
    the providers' own topic predicate accepts a third of all
    subject-message pairs, so first-past-the-post was choosing among
    several nominal matches on a criterion unrelated to what he said, and
    the cue that surfaced was regularly the one sharing only a function
    word with him. Ordering by relevance leaves the acceptance set alone
    -- so reach cannot fall -- and only changes *which* of the accepted
    cues she is handed.

    ``min_cosine`` additionally admits a row the predicate refused when
    its subject is close enough to the message anyway, which is the case
    a word-overlap test structurally cannot see. Purely additive.

    Degrades exactly to the old behaviour: with no ``user_vec`` (no
    embedder, or a message too short to embed) every cosine is ``None``,
    the sort is stable, and the first admitted row wins as before.

    Does **not** mark the cue surfaced. The provider does that, after it
    has decided the cue is actually going into the prompt, because a cue
    inspected and rejected must not spend one of its two surfacings.
    """
    if store is None:
        return CuePick()
    policy = policy_for(cue_type)
    try:
        # With embeddings: the row is handed to post-turn matching after
        # the turn, and re-reading it there would mean a second query per
        # surfaced cue for a few kilobytes we already have in hand.
        rows = store.pending(
            cue_type,
            limit=max(1, int(limit)),
            with_embedding=True,
            oldest_first=(
                policy is not None and policy.pick_order == PICK_OLDEST
            ),
        )
    except Exception:
        log.debug("cue pool read failed: type=%s", cue_type, exc_info=True)
        return CuePick()
    held = 0
    rejected = 0
    # (row, arm, cosine) for everything that cleared admission, kept in
    # shelf order because that is the tie-break.
    admitted: list[tuple["CueRow", str, float | None]] = []
    for row in rows:
        if not allow_first_claim and row.surfaced_count <= 0:
            held += 1
            continue
        score = topic_match.cosine(row.embedding, user_vec)
        if force or relevant is None or relevant(row.payload):
            admitted.append((row, topic_match.ARM_LEXICAL, score))
            continue
        if (
            min_cosine is not None
            and score is not None
            and score >= float(min_cosine)
        ):
            admitted.append((row, topic_match.ARM_COSINE, score))
            continue
        rejected += 1
    if not admitted:
        return CuePick(
            considered=len(rows), held_for_cadence=held, rejected=rejected,
        )
    # ``max`` returns the *first* maximal element, so with every cosine
    # absent this is the shelf's own first admitted row -- the previous
    # behaviour, reached without a special case for it.
    best = max(
        admitted,
        key=lambda item: item[2] if item[2] is not None else float("-inf"),
    )
    return CuePick(
        row=best[0],
        considered=len(rows),
        held_for_cadence=held,
        rejected=rejected,
        admitted=len(admitted),
        arm=best[1],
        cosine=best[2],
    )


__all__ = ["CueProducer", "CuePick", "pick_pool_cue"]

"""L29(a) - episodic shared-arc grouping (pure, time-aware).

An *episodic shared arc* is a closed joint project compressed into one named
story -- "the month we rebuilt the memory system", "our push to get voice
mode working". Its evidence is ``shared_moment`` memories in temporal order,
and the concept it becomes is an ordinary ``narrative`` (``evidence_model =
"sequence"``) carrying ``subject="relationship"``.

Why this is not :mod:`ritual_grouping`. That module answers a different
question with the same input: single-link cosine over *all* moments, no time
axis, looking for **recurrence** -- the same activity done again and again
("Friday debugging evenings"). An arc is the opposite shape: **distinct steps
in one bounded stretch of time**, each different from the last, which
single-link would either merge into one blob or refuse to link at all.

Why not the topic graph, which is how L8 sources its user/aiko arcs. Cluster
membership has no time axis either, so two separate pushes at the same topic
six months apart land in one cluster and read as a single incoherent arc.
Sourcing also assumed moments cluster topically, which only became true once
``SharedMomentsStore`` stopped embedding the ``"Shared moment (<vibe>): "``
prefix (see that module's header).

**Vectors are mean-centered before anything is compared**, via the shared
:func:`~app.core.concepts.ritual_grouping.center_vectors` (measurements and the
degenerate-corpus guard live there). Without it, 74% of all pairs in a real
145-moment corpus cleared 0.55 and every threshold from 0.55 to 0.80 produced
one snowballing 83-to-132 member blob. This is the same failure as the vibe
prefix ``SharedMomentsStore`` used to embed -- a shared component drowning the
signal -- except intrinsic to the corpus rather than injected. **Thresholds
here are therefore on the centered scale**; they happen to share the ritual
pass's default because both were calibrated on the same corpus, not because the
two passes are interchangeable.

The algorithm is then a **seed-and-sweep** over the time-ordered stream:

1. Take the earliest unassigned moment as a seed.
2. Sweep forward, absorbing an unassigned moment when it is *both* close to
   the running centroid (topical coherence) and within ``gap_days`` of the
   episode's last member (temporal contiguity). A moment that fails the
   coherence test is **skipped, not fatal** -- with several moments a day
   across unrelated topics, interleaving is the norm, and closing on the
   first mismatch would never build a chain longer than two.
3. The episode closes when its topic goes quiet: the next unassigned moment
   in time sits more than ``gap_days`` past the last member.
4. Repeat from the next unassigned seed, so concurrent threads each get
   their own episode.

Survivors need ``>= min_chain`` members and a last member at least
``quiet_days`` old -- a project still in motion is not a closed arc, and the
proposer's ``closed`` gate should never be asked to adjudicate a story that
is still happening.

Pure + dependency-light (numpy + stdlib): no store / settings / LLM imports,
so it unit-tests in isolation. The worker does the memory I/O and hands in
:class:`MomentInput` rows built by :func:`moment_from_memory`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np

from app.core.concepts.ritual_grouping import (
    MomentInput,
    MomentLite,
    _dominant_vibe,
    _normalise,
    _trim,
    _weekday_of,
    center_vectors,
)


# Cosine floor for "the same thread", on the *mean-centered* scale -- not
# comparable to the ritual pass's 0.6 on raw vectors. Calibrated against a real
# 145-moment corpus, where centered pairs run mean -0.006 / p90 0.165 / p99
# 0.371: 0.45 yielded a handful of readable threads, while 0.30 was back to
# chaining a third of the corpus into one run.
_DEFAULT_SIMILARITY = 0.45
# A thread that goes silent this long has ended, even if it resumes later --
# the resumption becomes its own episode, which is the honest reading.
_DEFAULT_GAP_DAYS = 10.0
# Chain floor, mirroring ``narrative_min_chain``: three steps is a story.
_DEFAULT_MIN_CHAIN = 3
# How recently the thread may have been touched and still count as closed.
_DEFAULT_QUIET_DAYS = 3.0

_DAY_SECONDS = 86400.0


@dataclass(slots=True, frozen=True)
class ArcEpisode:
    """A bounded, topically coherent run of shared moments in temporal order.

    ``member_ids`` and ``members`` are both oldest-first -- the chain order
    the proposer cites as ordered ``sequence`` evidence. ``dominant_vibe``
    comes from the ``vibe`` *field* (never the embedding), which is the whole
    point of the topic/vibe split.
    """

    member_ids: tuple[int, ...]
    members: tuple[MomentLite, ...]
    dominant_vibe: str
    first_when: str
    last_when: str

    @property
    def size(self) -> int:
        return len(self.member_ids)

    @property
    def span_days(self) -> float:
        start = _parse_when(self.first_when)
        end = _parse_when(self.last_when)
        if start is None or end is None:
            return 0.0
        return max(0.0, (end - start).total_seconds() / _DAY_SECONDS)


def _parse_when(when: str) -> datetime | None:
    """Parse an ISO-8601 ``when``, tolerating a trailing ``Z`` and naive
    stamps (assumed UTC). ``None`` when it cannot be read at all."""
    text = (when or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        try:
            ts = datetime.fromisoformat(re.sub(r"Z$", "+00:00", text))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def group_episodes(
    moments: Sequence[MomentInput],
    *,
    min_chain: int = _DEFAULT_MIN_CHAIN,
    similarity: float = _DEFAULT_SIMILARITY,
    gap_days: float = _DEFAULT_GAP_DAYS,
    quiet_days: float = _DEFAULT_QUIET_DAYS,
    now: datetime | None = None,
) -> list[ArcEpisode]:
    """Group shared moments into candidate closed arcs. See the module header
    for the seed-and-sweep algorithm and why the two thresholds differ from
    the ritual pass.

    Episodes come back largest-first (ties broken oldest-first) so downstream
    capping is deterministic. Moments whose ``when`` cannot be parsed are
    dropped: an arc is a claim about time, so a member with no place in it
    has nothing to contribute.
    """
    dated: list[tuple[datetime, MomentInput]] = []
    for m in moments:
        if m is None:
            continue
        ts = _parse_when(m.when)
        if ts is None:
            continue
        dated.append((ts, m))
    min_chain = max(2, int(min_chain))
    if len(dated) < min_chain:
        return []
    # Oldest first; id breaks ties so two moments logged in the same second
    # keep a stable order across runs.
    dated.sort(key=lambda pair: (pair[0], pair[1].id))

    thr = max(-1.0, min(1.0, float(similarity)))
    gap_seconds = max(0.0, float(gap_days)) * _DAY_SECONDS
    reference = now or datetime.now(timezone.utc)
    quiet_cutoff = max(0.0, float(quiet_days)) * _DAY_SECONDS

    vectors = center_vectors([_normalise(m.embedding) for _, m in dated])
    taken = [False] * len(dated)
    episodes: list[ArcEpisode] = []

    for seed in range(len(dated)):
        if taken[seed]:
            continue
        taken[seed] = True
        members = [seed]
        # Running centroid as a raw sum of unit vectors, normalised only for
        # the comparison -- normalising in place each step would drift toward
        # the most recent member instead of the mean of the whole chain.
        accumulator = vectors[seed].copy()
        centroid = _normalise(accumulator)
        last_ts = dated[seed][0]

        for idx in range(seed + 1, len(dated)):
            if taken[idx]:
                continue
            ts = dated[idx][0]
            if (ts - last_ts).total_seconds() > gap_seconds:
                # The thread has gone quiet: nothing later can belong to
                # *this* episode, and a resumption starts a fresh one.
                break
            vec = vectors[idx]
            if float(np.dot(centroid, vec)) < thr:
                continue
            taken[idx] = True
            members.append(idx)
            last_ts = ts
            accumulator = accumulator + vec
            centroid = _normalise(accumulator)

        if len(members) < min_chain:
            # Release the followers so a later seed can still use them; the
            # seed itself stays taken so the sweep terminates.
            for idx in members[1:]:
                taken[idx] = False
            continue

        rows = [dated[i][1] for i in members]
        last_member_ts = dated[members[-1]][0]
        if (reference - last_member_ts).total_seconds() < quiet_cutoff:
            continue

        episodes.append(
            ArcEpisode(
                member_ids=tuple(m.id for m in rows),
                members=tuple(
                    MomentLite(
                        id=m.id,
                        text=_trim(m.text),
                        vibe=m.vibe,
                        weekday=_weekday_of(m.when),
                    )
                    for m in rows
                ),
                dominant_vibe=_dominant_vibe([m.vibe for m in rows]),
                first_when=rows[0].when,
                last_when=rows[-1].when,
            )
        )

    episodes.sort(key=lambda e: (-e.size, e.first_when))
    return episodes


__all__ = [
    "ArcEpisode",
    "group_episodes",
]

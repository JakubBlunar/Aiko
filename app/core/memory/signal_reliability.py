"""H18's reliability rule, as a callable instrument rather than advice.

H18 found that L38's earned standing had spent three months ranking 466
concepts off a signal containing no information, and only because someone
asked a different question than "does this run". The rule it left behind
is the valuable part:

    Before a signal is allowed to rank anything, measure its split-half
    reliability against the null of shuffling it. A signal with
    reliability under ~0.2 is a constant with extra steps.

It left that as a sentence, and a sentence does not get re-run. This is
the sentence as code, over any per-item series of 0/1 observations.

Why one correlation is not enough, measured
-------------------------------------------
H18 left the cluster engaged rate open as "not yet a finding" on 38
items. Re-measured with twice the corpus it scores **0.233** at an
evidence floor of 8 -- over H18's own 0.2 line, so a re-run of H18's test
would have promoted it to a usable signal. It is not one, and neither of
the two checks that show this is a bigger correlation:

- **A shape-matched null.** Keep every item's row *count* and draw every
  row from one global rate, so the series has no item-level structure by
  construction, and re-run the identical split. Whatever comes back is
  what the method manufactures on this population shape. Cluster rows
  come back at 0.065 against a real 0.319 at floor 4 -- but see below,
  because the margin is not the end of it.
- **Excess spread.** If every item shared one true rate, between-item
  spread in observed rates would be exactly ``sqrt(p(1-p)/n)``. Compare
  that with the spread actually present, bucketed by row count. For
  clusters the ratio is 1.02, 0.42, 1.33, 1.45, 0.88, 1.19 across
  buckets -- scattered either side of 1, with no consistent excess.
  There is nothing there for a ranking term to read.

Run over the four signals L38 has read, excess spread turns out to
separate them more cleanly than any correlation does, and the *slope*
across row-count buckets is what does it:

    concept/echoed   1.27  1.43  1.81  2.01  2.50  3.61   real
    memory/echoed    1.19  1.38  1.23  1.57  2.11  3.61   real
    concept/engaged  1.12  1.04  1.07  1.02  1.00  1.02   empty
    memory/engaged   1.19  1.06  1.12  1.28  1.07  1.36   empty

A real per-item signal pulls further away from the single-rate
prediction as its items accumulate evidence, because sampling noise
shrinks while genuine between-item differences do not. An empty one is
pinned at 1.0 no matter how much evidence you give it -- and being
pinned at 1.0 over 132 items averaging 98 observations each is a far
stronger statement than any p-value, because there is no sample size at
which it could improve.

The population shape is the other half of the story and the reason the
bucket table is reported rather than summarised. Of 80 cluster items past
a floor of 8, **50 carry 60+ rows (mean 472) and the rest are a
scattering of 2 to 8** -- and the sparse ones engage at 0.117 against the
dense ones' 0.214. A split-half correlation over that population is
partly reading "rare items land worse than common ones", which is a fact
about row counts and not a per-item rate. One number cannot say so; the
buckets can.

A note on an instrument that was tried and rejected
---------------------------------------------------
An earlier version of this module keyed its verdict on the *slope* of
reliability across evidence floors, on the theory that borrowed
turn-level agreement washes out as items accumulate evidence. The slope
is real in the live data (0.319 down to -0.113) but the explanation was
wrong, and the test written to prove it disproved it: splitting an item's
own rows cannot break the turn structure those rows came from, because
both halves keep sampling the same turns, so a purely turn-driven series
generated on purpose stays at r = 0.9 at every floor. The sweep is still
reported -- a collapse to zero at the top is worth seeing -- but nothing
depends on a mechanism that could not be reproduced.

What this cannot tell you
-------------------------
Nothing here says a reliable signal is the *right* signal. H18's switch
from ``engaged`` to ``echoed`` was a judgement -- echo is Aiko's verdict,
not the user's, so rewarding it risks favouring what she already reaches
for -- taken knowingly against a 12x reliability gap. This module
measures whether a number carries information, which is a precondition
for that argument rather than a substitute for it.
"""
from __future__ import annotations

import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# H18's line: below this, an item-level rate is not ordering anything.
NOISE_CEILING = 0.20
# Above this a signal is worth ranking on without further argument.
SIGNAL_FLOOR = 0.40
# Fewer items than this and a correlation is not worth quoting.
MIN_ITEMS = 8
# Evidence floors swept for the report.
DEFAULT_FLOORS: tuple[int, ...] = (4, 8, 12, 20, 30)
# How far past the shape-matched null a correlation must land before it
# counts as measuring the items rather than the method.
NULL_MARGIN = 0.15
# Between-item spread must exceed the single-rate prediction by this much
# for there to be anything to rank on.
MIN_EXCESS_SPREAD = 1.25

VERDICT_NOISE = "noise"
VERDICT_WEAK = "weak"
VERDICT_SIGNAL = "signal"
VERDICT_UNDERPOWERED = "underpowered"


@dataclass(frozen=True, slots=True)
class Reliability:
    """One split-half measurement at one evidence floor.

    ``sd`` is the spread across resampling trials, not a standard error
    of the correlation: it says how much the number moves when the split
    is redrawn, which is what tells you whether two floors really differ.
    """

    floor: int
    items: int
    r: float | None
    sd: float = 0.0

    @property
    def quotable(self) -> bool:
        return self.r is not None and self.items >= MIN_ITEMS


@dataclass(frozen=True, slots=True)
class Bucket:
    """Items grouped by how much evidence they carry.

    ``excess`` is observed between-item spread over the spread a single
    shared rate would produce. Near 1 means the items are
    indistinguishable from each other.
    """

    label: str
    items: int
    mean_rows: float
    mean_rate: float
    observed_sd: float
    expected_sd: float

    @property
    def excess(self) -> float | None:
        if self.expected_sd <= 0 or self.items < 2:
            return None
        return self.observed_sd / self.expected_sd


@dataclass(frozen=True, slots=True)
class SignalVerdict:
    """Every reading the rule takes, plus the one-word answer."""

    name: str
    sweep: tuple[Reliability, ...] = field(default_factory=tuple)
    peak: Reliability | None = None
    null_r: float | None = None
    buckets: tuple[Bucket, ...] = field(default_factory=tuple)
    excess: float | None = None
    p_value: float | None = None
    null_ratio: float | None = None
    verdict: str = VERDICT_UNDERPOWERED
    detail: str = ""

    @property
    def usable(self) -> bool:
        """May a ranking term read this signal?"""
        return self.verdict in (VERDICT_WEAK, VERDICT_SIGNAL)


def _rate(values: Sequence[int]) -> float:
    return sum(1 for v in values if v) / len(values)


def split_half(
    series: Mapping[str, Sequence[int]],
    *,
    floor: int = 8,
    trials: int = 40,
    rng: random.Random | None = None,
) -> Reliability:
    """Correlate each item's rate in one random half against the other.

    The split is over an item's own observations, so the question is
    strictly "does this item behave consistently", independent of how
    many observations any other item has.
    """
    rand = rng or random.Random(0)
    items = {
        str(k): list(v)
        for k, v in series.items()
        if len(v) >= max(2, int(floor))
    }
    if len(items) < MIN_ITEMS:
        return Reliability(floor=int(floor), items=len(items), r=None)
    scores: list[float] = []
    for _ in range(max(1, int(trials))):
        left: list[float] = []
        right: list[float] = []
        for values in items.values():
            shuffled = list(values)
            rand.shuffle(shuffled)
            mid = len(shuffled) // 2
            a, b = shuffled[:mid], shuffled[mid:]
            if not a or not b:
                continue
            left.append(_rate(a))
            right.append(_rate(b))
        if len(left) < MIN_ITEMS:
            continue
        # A half with no variance means every item scored identically:
        # there is nothing to correlate, and pretending otherwise raises.
        if not statistics.pstdev(left) or not statistics.pstdev(right):
            continue
        scores.append(statistics.correlation(left, right))
    if not scores:
        return Reliability(floor=int(floor), items=len(items), r=None)
    return Reliability(
        floor=int(floor),
        items=len(items),
        r=statistics.mean(scores),
        sd=statistics.pstdev(scores) if len(scores) > 1 else 0.0,
    )


def sweep(
    series: Mapping[str, Sequence[int]],
    *,
    floors: Sequence[int] = DEFAULT_FLOORS,
    trials: int = 40,
    rng: random.Random | None = None,
) -> tuple[Reliability, ...]:
    """:func:`split_half` at each floor, so the shape is visible."""
    rand = rng or random.Random(0)
    return tuple(
        split_half(series, floor=f, trials=trials, rng=rand) for f in floors
    )


def shape_matched_null(
    series: Mapping[str, Sequence[int]],
    *,
    floor: int = 8,
    trials: int = 40,
    rng: random.Random | None = None,
) -> float | None:
    """Split-half of a structureless series with the same row counts.

    The control H18's test did not have. Every item keeps its number of
    observations and every observation is drawn from one global rate, so
    there is no item-level signal by construction and any correlation
    returned is what the method manufactures on this population's shape.
    """
    rand = rng or random.Random(0)
    rows = [v for v in series.values() if v]
    if not rows:
        return None
    total = sum(len(v) for v in rows)
    p = sum(sum(1 for x in v if x) for v in rows) / max(1, total)
    fake = {
        f"null{i}": [1 if rand.random() < p else 0 for _ in range(len(v))]
        for i, v in enumerate(rows)
    }
    return split_half(fake, floor=floor, trials=trials, rng=rand).r


def bucket_spread(
    series: Mapping[str, Sequence[int]],
    *,
    edges: Sequence[int] = (4, 8, 12, 20, 30, 60),
) -> tuple[Bucket, ...]:
    """Between-item spread by row count, against the single-rate prediction.

    Bucketing is not decoration. A population where most items carry
    hundreds of rows and a handful carry four is one where a single
    correlation partly measures the row counts, and that only shows up
    when the groups are listed side by side.
    """
    rows = {str(k): list(v) for k, v in series.items() if v}
    if not rows:
        return ()
    total = sum(len(v) for v in rows.values())
    p = sum(sum(1 for x in v if x) for v in rows.values()) / max(1, total)
    bounds = list(edges) + [10**9]
    out: list[Bucket] = []
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        chosen = [v for v in rows.values() if lo <= len(v) < hi]
        if not chosen:
            continue
        rates = [_rate(v) for v in chosen]
        mean_rows = statistics.mean(len(v) for v in chosen)
        out.append(Bucket(
            label=(f"{lo}-{hi - 1}" if hi < 10**9 else f"{lo}+"),
            items=len(chosen),
            mean_rows=mean_rows,
            mean_rate=statistics.mean(rates),
            observed_sd=(
                statistics.pstdev(rates) if len(rates) > 1 else 0.0
            ),
            expected_sd=(p * (1.0 - p) / mean_rows) ** 0.5,
        ))
    return tuple(out)


def excess_spread(
    series: Mapping[str, Sequence[int]], *, floor: int = 8
) -> float | None:
    """Observed between-item spread over the single-rate prediction.

    The whole rule in one number: at 1.0 every item is a draw from the
    same rate, and there is nothing for a ranking term to order.
    """
    rows = [list(v) for v in series.values() if len(v) >= max(2, int(floor))]
    if len(rows) < 2:
        return None
    total = sum(len(v) for v in rows)
    p = sum(sum(1 for x in v if x) for v in rows) / max(1, total)
    if p <= 0.0 or p >= 1.0:
        return None
    rates = [_rate(v) for v in rows]
    # Harmonic mean: the prediction is per item and averaging variances
    # is what makes a population of mixed row counts comparable.
    inv = sum(1.0 / len(v) for v in rows) / len(rows)
    expected = (p * (1.0 - p) * inv) ** 0.5
    if expected <= 0:
        return None
    return statistics.pstdev(rates) / expected


def permutation_test(
    per_turn_items: Mapping[int, Sequence[str]],
    turn_labels: Mapping[int, bool],
    *,
    floor: int = 8,
    trials: int = 400,
    rng: random.Random | None = None,
) -> tuple[float | None, float | None]:
    """Between-item spread against the null of reshuffled turn labels.

    H18's own second instrument. Keeps every turn's item set and the
    multiset of labels and breaks only the pairing, which destroys any
    item-level signal while preserving how many good turns there were and
    how large their item sets are. Returns ``(p_value, observed / null
    median)``.
    """
    rand = rng or random.Random(0)

    def spread(labels: Mapping[int, bool]) -> float | None:
        tally: dict[str, list[int]] = {}
        for turn, items in per_turn_items.items():
            hit = 1 if labels.get(turn) else 0
            for item in items:
                tally.setdefault(str(item), []).append(hit)
        rates = [
            _rate(v) for v in tally.values() if len(v) >= max(2, int(floor))
        ]
        if len(rates) < MIN_ITEMS:
            return None
        return statistics.pvariance(rates)

    observed = spread(turn_labels)
    if observed is None:
        return None, None
    turns = list(turn_labels)
    pool = [bool(turn_labels[t]) for t in turns]
    null: list[float] = []
    for _ in range(max(1, int(trials))):
        rand.shuffle(pool)
        value = spread(dict(zip(turns, pool, strict=True)))
        if value is not None:
            null.append(value)
    if not null:
        return None, None
    null.sort()
    above = sum(1 for v in null if v >= observed)
    median = null[len(null) // 2]
    return (
        (above + 1) / (len(null) + 1),
        (observed / median) if median else None,
    )


def classify(
    name: str,
    series: Mapping[str, Sequence[int]],
    *,
    per_turn_items: Mapping[int, Sequence[str]] | None = None,
    turn_labels: Mapping[int, bool] | None = None,
    floor: int = 8,
    floors: Sequence[int] = DEFAULT_FLOORS,
    trials: int = 40,
    rng: random.Random | None = None,
) -> SignalVerdict:
    """The whole rule in one call.

    Order of judgement is deliberate. ``underpowered`` outranks every
    other answer, because a thin corpus reported as evidence of absence
    is what left H18's cluster case open for two months. The
    shape-matched null and the excess-spread check then both have to pass
    before a correlation is believed: they fail independently, the
    cluster series passes the raw 0.2 line and fails both, and either one
    alone would have called it a signal.
    """
    rand = rng or random.Random(0)
    readings = sweep(series, floors=floors, trials=trials, rng=rand)
    quotable = [r for r in readings if r.quotable]
    buckets = bucket_spread(series)
    excess = excess_spread(series, floor=floor)
    null_r = shape_matched_null(series, floor=floor, trials=trials, rng=rand)
    p_value = null_ratio = None
    if per_turn_items and turn_labels:
        p_value, null_ratio = permutation_test(
            per_turn_items, turn_labels, floor=floor, rng=rand
        )
    base = {
        "name": name,
        "sweep": readings,
        "null_r": null_r,
        "buckets": buckets,
        "excess": excess,
        "p_value": p_value,
        "null_ratio": null_ratio,
    }
    if not quotable:
        return SignalVerdict(
            **base,
            verdict=VERDICT_UNDERPOWERED,
            detail=(
                "no evidence floor admitted "
                f"{MIN_ITEMS} items with variance to correlate"
            ),
        )
    peak = max(quotable, key=lambda r: r.r or -1.0)
    assert peak.r is not None
    if peak.r < NOISE_CEILING:
        return SignalVerdict(
            **base,
            peak=peak,
            verdict=VERDICT_NOISE,
            detail=(
                f"peak {peak.r:.3f} is under the {NOISE_CEILING:.2f} line; "
                "a constant with extra steps"
            ),
        )
    if null_r is not None and (peak.r - null_r) < NULL_MARGIN:
        return SignalVerdict(
            **base,
            peak=peak,
            verdict=VERDICT_NOISE,
            detail=(
                f"peak {peak.r:.3f} is within {NULL_MARGIN:.2f} of the "
                f"{null_r:.3f} a structureless series of the same row "
                "counts produces, so it is measuring the method"
            ),
        )
    if excess is not None and excess < MIN_EXCESS_SPREAD:
        return SignalVerdict(
            **base,
            peak=peak,
            verdict=VERDICT_NOISE,
            detail=(
                f"between-item spread is {excess:.2f}x the spread one "
                "shared rate would produce, so the items are not "
                "distinguishable from each other"
            ),
        )
    return SignalVerdict(
        **base,
        peak=peak,
        verdict=(
            VERDICT_SIGNAL if peak.r >= SIGNAL_FLOOR else VERDICT_WEAK
        ),
        detail=(
            f"peak {peak.r:.3f} at floor {peak.floor} over {peak.items} "
            f"items, {excess:.2f}x the single-rate spread"
            if excess is not None
            else f"peak {peak.r:.3f} at floor {peak.floor}"
        ),
    )


__all__ = [
    "DEFAULT_FLOORS",
    "MIN_EXCESS_SPREAD",
    "MIN_ITEMS",
    "NOISE_CEILING",
    "NULL_MARGIN",
    "SIGNAL_FLOOR",
    "Bucket",
    "Reliability",
    "SignalVerdict",
    "VERDICT_NOISE",
    "VERDICT_SIGNAL",
    "VERDICT_UNDERPOWERED",
    "VERDICT_WEAK",
    "bucket_spread",
    "classify",
    "excess_spread",
    "permutation_test",
    "shape_matched_null",
    "split_half",
    "sweep",
]

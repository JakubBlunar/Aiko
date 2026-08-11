"""L45 measurement -- turn loaded concept rows into distributions.

Two outputs, from one pass over the rows:

- :func:`populations` -- the sample lists each :class:`GateSpec` names, so
  :func:`~app.core.concepts.gate_tuning.solve_all` can do its arithmetic
  without knowing anything about concepts.
- :func:`snapshot` -- one line for ``concept_population.jsonl``. It proposes
  nothing. It exists because every threshold retune so far began by measuring
  the graph from scratch, which meant reasoning from a single day's shape; a
  rolling record turns the same question into a trend read, and the later
  phases (the cosine bars, the per-kind floors) are designed against it.

Pure apart from ``numpy``: the caller loads the rows and supplies the ledger
and event counts. That keeps the arithmetic testable without a database, the
same split :mod:`app.core.concepts.concept_quality` uses.
"""
from __future__ import annotations

import logging
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from app.core.concepts.concept_kinds import (
    CONCEPT_KINDS,
    ROLE_ANCHOR,
    ROLE_GENERATIVE,
    ROLE_GUIDE,
    renders_in_static_block,
)
from app.core.concepts.concept_lifecycle import KIND_PROMOTION_FLOORS
from app.core.concepts.gate_tuning import (
    POP_ACTIVE_CONFIDENCE,
    POP_CANDIDATE_CONFIDENCE,
    POP_CLUSTER_ENGAGED_RATE,
    POP_CORE_POOL,
    POP_DORMANT_QUIET_DAYS,
    POP_EVIDENCE_FIT,
    POP_FADED_CONFIDENCE,
    POP_OPENNESS_POOL,
    POP_PAIR_COSINE,
    POP_PROFILE_POOL,
    describe,
    kind_population,
)
from app.core.infra import timephrase

log = logging.getLogger("app.gate_tuning")

#: Statuses whose confidence reads as "faded but not gone" -- the pool the
#: retire floor reaches into.
_FADED_STATUSES = ("dormant", "contradicted")

#: The kinds the T0 profile block can draw from. Mirrors the ``profile_block``
#: diet target in ``ConceptView.for_target``; kept as a constant here so the
#: measurement of that lane's pool cannot drift from the lane itself without
#: this line looking wrong.
_PROFILE_KINDS = ("identity", "value")


def _role_of(kind: str) -> str:
    spec = CONCEPT_KINDS.get(str(kind))
    return str(getattr(spec, "role", ROLE_ANCHOR)) if spec else ROLE_ANCHOR


def _confidences(rows: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for row in rows:
        try:
            out.append(float(row.confidence))
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _is_core_kind(kind: str) -> bool:
    spec = CONCEPT_KINDS.get(str(kind))
    return bool(spec) and bool(getattr(spec, "core_always_on", False))


def sample_pair_cosine(
    rows: Sequence[Any],
    *,
    pairs: int,
    rng: random.Random | None = None,
) -> list[float]:
    """Cosine similarity for a bounded random sample of concept pairs.

    Exhaustive comparison is quadratic -- around 420,000 pairs at the current
    graph size, and growing -- while the thing we actually want is the *shape*
    of the similarity distribution, which a random sample estimates perfectly
    well. Sampling also keeps the run in the low seconds, which matters
    because the scheduler admits work against an EMA of past durations: a
    worker that grows slower than its lane budget stops being admitted.
    Successive runs draw fresh pairs, so the picture sharpens over time
    without any single run paying for it.
    """
    wanted = max(0, int(pairs))
    if wanted <= 0:
        return []
    vectors: list[np.ndarray] = []
    for row in rows:
        vec = getattr(row, "embedding", None)
        if vec is None or getattr(vec, "size", 0) == 0:
            continue
        arr = np.asarray(vec, dtype=np.float32).ravel()
        norm = float(np.linalg.norm(arr))
        if norm <= 0.0:
            continue
        vectors.append(arr / norm)
    if len(vectors) < 2:
        return []

    dim = Counter(int(v.shape[0]) for v in vectors).most_common(1)[0][0]
    usable = [v for v in vectors if int(v.shape[0]) == dim]
    if len(usable) < 2:
        return []

    picker = rng or random.Random()
    total = len(usable)
    out: list[float] = []
    seen: set[tuple[int, int]] = set()
    # Bounded attempts: with a small graph the pair space can be smaller than
    # ``wanted``, and retrying forever for distinct pairs would hang.
    attempts = wanted * 3
    while len(out) < wanted and attempts > 0:
        attempts -= 1
        i = picker.randrange(total)
        j = picker.randrange(total)
        if i == j:
            continue
        key = (i, j) if i < j else (j, i)
        if key in seen:
            continue
        seen.add(key)
        out.append(float(np.dot(usable[key[0]], usable[key[1]])))
    return out


def populations(
    rows: Sequence[Any],
    *,
    cluster_engaged_rates: Sequence[float] = (),
    evidence_fit: Sequence[float] = (),
    cosine_pairs: int = 0,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, list[float]]:
    """Every sample list the registry's specs ask for.

    A population that cannot be measured is *omitted* rather than supplied
    empty, because ``solve_all`` skips missing populations while an empty one
    would look like a real measurement with no samples in it.
    """
    when = now or timephrase.utcnow()
    by_status: dict[str, list[Any]] = defaultdict(list)
    by_kind: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_status[str(getattr(row, "status", ""))].append(row)
        by_kind[str(getattr(row, "kind", ""))].append(row)

    actives = by_status.get("active", [])
    candidates = by_status.get("candidate", [])
    faded = [
        row for status in _FADED_STATUSES for row in by_status.get(status, [])
    ]

    out: dict[str, list[float]] = {
        POP_CANDIDATE_CONFIDENCE: _confidences(candidates),
        POP_ACTIVE_CONFIDENCE: _confidences(actives),
        POP_FADED_CONFIDENCE: _confidences(faded),
        POP_CORE_POOL: _confidences(
            row for row in actives if _is_core_kind(row.kind)
        ),
        POP_OPENNESS_POOL: _confidences(
            row
            for row in actives
            if _role_of(row.kind) == ROLE_GENERATIVE
            and renders_in_static_block(row.kind)
        ),
        POP_PROFILE_POOL: _confidences(
            row
            for row in actives
            if str(getattr(row, "subject", "")) == "user"
            and str(getattr(row, "kind", "")) in _PROFILE_KINDS
        ),
    }

    for kind in KIND_PROMOTION_FLOORS:
        pool = [
            row
            for row in by_kind.get(kind, [])
            if str(getattr(row, "status", "")) in ("active", "candidate")
        ]
        out[kind_population(kind)] = _confidences(pool)

    rates = [float(r) for r in cluster_engaged_rates if r is not None]
    if rates:
        out[POP_CLUSTER_ENGAGED_RATE] = rates

    if cosine_pairs > 0:
        sample = sample_pair_cosine(actives, pairs=cosine_pairs, rng=rng)
        if sample:
            out[POP_PAIR_COSINE] = sample

    quiet = _quiet_days(by_status.get("dormant", []), when)
    if quiet:
        out[POP_DORMANT_QUIET_DAYS] = quiet

    # L31. Passed in rather than measured here: these are the cosines the
    # admission gate already computed as evidence arrived, rolled through
    # ``kv_meta``. Re-deriving them from the stored graph would measure the
    # wrong thing (evidence that got in) and cost a full memory-embedding
    # scan to do it.
    fit = [float(v) for v in evidence_fit if v is not None]
    if fit:
        out[POP_EVIDENCE_FIT] = fit

    return out


def _quiet_days(rows: Iterable[Any], now: datetime) -> list[float]:
    """Wall-clock days since each row was last reinforced.

    Falls back to ``created_at`` for a row nothing has ever reinforced, which
    is the same anchor ``_is_stale_dormant`` uses -- the measurement has to see
    the pool the way the gate does, or the solved value would be tuned against
    a distribution the gate never applies to.
    """
    out: list[float] = []
    for row in rows:
        raw = getattr(row, "last_reinforced_at", "") or getattr(
            row, "created_at", ""
        )
        stamp = timephrase.parse_iso(str(raw)) if raw else None
        if stamp is None:
            continue
        out.append(max(0.0, (now - stamp).total_seconds() / 86400.0))
    return out


def _age_days(row: Any, now: datetime) -> float | None:
    raw = getattr(row, "created_at", "") or ""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        created = datetime.fromisoformat(text)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds() / 86_400.0)


def snapshot(
    rows: Sequence[Any],
    pops: Mapping[str, Sequence[float]],
    *,
    now: datetime,
    previous_at: datetime | None = None,
    event_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """One ``concept_population.jsonl`` line.

    ``hours_since_previous`` is recorded rather than assumed, because the app
    is not always running: a machine that sleeps overnight produces uneven
    spacing, and a trend read that treats rows as daily would quietly
    misreport every rate in here.
    """
    by_status: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    by_subject: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    kind_conf: dict[str, list[float]] = defaultdict(list)

    actives = 0
    for row in rows:
        status = str(getattr(row, "status", ""))
        kind = str(getattr(row, "kind", ""))
        by_status[status] += 1
        by_kind[kind] += 1
        by_subject[str(getattr(row, "subject", ""))] += 1
        if status == "active":
            actives += 1
            roles[_role_of(kind)] += 1
            try:
                kind_conf[kind].append(float(row.confidence))
            except (TypeError, ValueError):
                pass

    directed = roles[ROLE_ANCHOR] + roles[ROLE_GUIDE] + roles[ROLE_GENERATIVE]
    candidates = [
        row for row in rows if str(getattr(row, "status", "")) == "candidate"
    ]
    ages = [
        age for age in (_age_days(row, now) for row in candidates)
        if age is not None
    ]
    sources = [
        float(getattr(row, "distinct_source_count", 0) or 0)
        for row in candidates
    ]

    hours: float | None = None
    if previous_at is not None:
        hours = round(
            max(0.0, (now - previous_at).total_seconds() / 3600.0), 2
        )

    return {
        "at": now.isoformat(),
        "hours_since_previous": hours,
        "total": len(rows),
        "active": actives,
        "by_status": dict(by_status),
        "by_kind": dict(by_kind),
        "by_subject": dict(by_subject),
        "by_role": {
            ROLE_ANCHOR: roles[ROLE_ANCHOR],
            ROLE_GUIDE: roles[ROLE_GUIDE],
            ROLE_GENERATIVE: roles[ROLE_GENERATIVE],
        },
        "constraint_ratio": (
            round((roles[ROLE_ANCHOR] + roles[ROLE_GUIDE]) / directed, 4)
            if directed else None
        ),
        "confidence": {
            name: describe(samples)
            for name, samples in sorted(pops.items())
            if not name.startswith("kind_confidence:")
        },
        "confidence_by_kind": {
            kind: describe(values) for kind, values in sorted(kind_conf.items())
        },
        "candidate_age_days": describe(ages),
        "candidate_sources": describe(sources),
        "events_since_previous": dict(event_counts or {}),
    }


__all__ = ["populations", "sample_pair_cosine", "snapshot"]

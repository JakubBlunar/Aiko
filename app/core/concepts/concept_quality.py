"""L22 concept-quality scoring -- the pure aggregate layer.

The deterministic, I/O-free core of the concept-quality scoreboard. It
knows nothing about SQLite, the ``ConceptStore``, the memory mirror or
the lifecycle worker; it only takes already-loaded
:class:`~app.core.concepts.concept_store.Concept` rows (plus a few facts
the caller resolved for it) and turns them into a snapshot dict.

Orchestration -- loading concepts, walking evidence edges, joining the
memory mirror, serving the result over REST / MCP -- lives in
:mod:`app.core.concepts.concept_snapshot`. Keeping the arithmetic pure
mirrors the split :mod:`app.core.persona.persona_regression` already
uses for K10, and makes the brittle parts (rate maths, register regexes,
duplicate detection) unit-testable without standing up a database.

**Why this exists.** L3's confidence gates keep *individual* concepts
honest, but nothing measured whether the layer as a whole was producing
good concepts. It wasn't: the first month of real use produced 544
concepts at a 91% promotion rate, of which 83% were never reinforced
again, with zero demotions ever -- none of which was visible from any
existing surface. Every metric here exists to make one of those failures
countable, so thresholds can be tuned against evidence.

Four families of signal:

- **Flow** (:func:`_flow`) -- production / promotion / reinforcement /
  retirement rates from the ``concept_events`` timeline. Answers "is the
  layer minting faster than it prunes?"
- **Shape** (:func:`_confidence`, :func:`_evidence`) -- confidence and
  evidence-support distributions, plus concepts sitting *below* the
  promotion bar they supposedly passed.
- **Register** (:func:`_register`) -- per-(kind, subject) label-template
  concentration. Deliberately never aggregated globally: a 67% shared
  opening is *correct* for ``value`` ("Jacob values ...") and pathological
  for ``identity`` ("Jacob treats the ..."), so one global number would
  average the signal away to nothing.
- **Pruning** (:func:`_pruning`) -- the L22 spurious-concept signals, the
  *rate* at which more are arriving, and how long the current decay
  settings would actually take to act on the ones already there.

Everything here is read-only and advisory. Nothing in this module
demotes, retires or deletes a concept; the L3 lifecycle worker remains
the single writer of ``confidence`` / ``plasticity`` / ``status``.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import numpy as np
from app.core.concepts.concept_lifecycle import effective_halflife
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.concepts.concept_store import Concept


# ── register heuristics ───────────────────────────────────────────────
#
# Two crude lexical probes for the failure mode that actually happened:
# the proposer stopped describing behaviour and started asserting a
# *functional theory* about it ("Jacob treats typos as high-fidelity data
# artifacts that validate ..."). Neither regex is a quality judgement on
# its own -- several kinds are interpretive by design (``value`` is
# literally "the normative why under choices", and ``tension`` /
# ``generalization`` / ``affective`` / ``aspiration`` are all inference).
# They are only meaningful *per kind, tracked over time*: a jump in
# ``identity`` is a collapse, while a steady high reading on ``value`` is
# that kind working as intended.

# "treats X as Y" and friends -- an interpretive frame imposed on an
# observation. Bounded lookahead so it only fires when the "as" clause is
# close enough to be the same claim.
_FRAME_RE = re.compile(
    r"\b(?:treats?|uses?|utili[sz]es?|views?|interprets?|frames?|equates?"
    r"|regards?|conceptuali[sz]es?)\b.{0,80}?\bas\b",
    re.IGNORECASE,
)

# Vocabulary imported from an unrelated (engineering) domain to describe
# ordinary human behaviour. The tell that accompanied the frame collapse.
_JARGON_RE = re.compile(
    r"\b(?:protocol|proxy|system\s+(?:test|idle|check)|handshake|keep-?alive"
    r"|low-stakes|high-fidelity|stress-test|data\s+artifacts?"
    r"|mechanism\s+to|low-bandwidth|bandwidth|architectural"
    r"|validat\w+|verif\w+)\b",
    re.IGNORECASE,
)

# Leading-n-gram width for the template-concentration probe.
_LEAD_NGRAM = 3

# Confidence above which a concept counts as "held firmly", used for the
# top-heaviness readout. Not a threshold anything acts on.
_HIGH_CONFIDENCE = 0.8

# Default cosine floor for the near-duplicate sweep. Pairs at or above
# the caller's dedupe bar were meant to be caught at creation; this looks
# at the band *below* it, which is where paraphrase twins actually land.
_DUPLICATE_BAND_FLOOR = 0.78

# Cap on the duplicate pairs carried in the snapshot. The count is
# always exact; only the sampled list is truncated.
_MAX_DUPLICATE_PAIRS = 40

# Label preview length in sampled output (keeps the payload small).
_PREVIEW_CHARS = 120

# Window for the pruning section's *flow* figures. Short enough that a
# threshold change shows up in it within days, long enough to survive a
# couple of quiet days without reading as zero.
_RECENT_WINDOW_DAYS = 7.0


# ── inputs ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceFacts:
    """Per-concept evidence facts the caller resolved from the graph.

    These need the edge table and the memory mirror, so they cannot be
    derived here. Supplied by
    :func:`app.core.concepts.concept_snapshot.build_concept_quality`;
    omit for a concept and its A/B signals simply read as unknown.

    - ``cluster_span`` (L22 signal A) -- distinct topic clusters behind
      the evidence, resolving ``memory`` edges through to their cluster.
      This is what ``distinct_source_count`` was standing in for and
      isn't: that counts distinct *edge endpoints*, so three memories
      from one cluster read as three sources when they are really one.
    - ``memory_confidences`` (L22 signal B) -- confidence of each
      supporting memory. A concept resting entirely on shaky memories is
      itself shaky, which nothing in the layer previously noticed.
    """

    cluster_span: int = 0
    memory_confidences: tuple[float, ...] = ()

    @property
    def memory_confidence_mean(self) -> float | None:
        if not self.memory_confidences:
            return None
        return sum(self.memory_confidences) / len(self.memory_confidences)

    @property
    def memory_confidence_min(self) -> float | None:
        if not self.memory_confidences:
            return None
        return min(self.memory_confidences)


@dataclass(frozen=True)
class QualityThresholds:
    """The live settings the report measures *against*.

    Passed in rather than imported so the report always reflects the
    running configuration, and so tests can vary them freely. The
    defaults here are only a fallback; every real caller supplies the
    live values (``dedupe_cos`` from the synthesis worker's
    ``_DEDUPE_COS``, the rest from ``MemorySettings``).
    """

    promote_min_sources: int = 2
    dedupe_cos: float = 0.86
    dormant_confidence_floor: float = 0.35
    confidence_halflife_days: float = 45.0
    duplicate_band_floor: float = _DUPLICATE_BAND_FLOOR


@dataclass
class _Bucket:
    """Mutable per-(kind, subject) accumulator used while scanning."""

    labels: list[str] = field(default_factory=list)
    frame_hits: int = 0
    jargon_hits: int = 0


# ── helpers ───────────────────────────────────────────────────────────


def _parse_iso(value: str | None) -> datetime | None:
    """Lenient ISO-8601 parse returning an aware UTC datetime or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _preview(text: str) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) > _PREVIEW_CHARS:
        return flat[: _PREVIEW_CHARS - 1].rstrip() + "\u2026"
    return flat


def _pct(part: int, whole: int) -> float:
    """Percentage rounded to one decimal; ``0.0`` when the base is empty."""
    if whole <= 0:
        return 0.0
    return round(100.0 * float(part) / float(whole), 1)


def _stats(values: Sequence[float]) -> dict[str, Any]:
    """min / mean / max / n for a run of floats (zeros when empty)."""
    if not values:
        return {"n": 0, "min": 0.0, "mean": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "max": round(max(values), 3),
    }


def unreinforced_since_promotion(concept: "Concept") -> bool:
    """L22 signal C -- promoted once and never reinforced since.

    True when an active concept has either never recorded a
    reinforcement at all, or last recorded one at or before the moment it
    was promoted. Decay alone eventually handles these, but on the
    default 45-engaged-day half-life "eventually" is measured in tens of
    hours of conversation, which is far slower than they are minted --
    so the state needs to be countable rather than merely implied.

    Candidates are excluded: they have not been promoted yet, so the
    predicate is meaningless for them.
    """
    if concept.status != "active":
        return False
    promoted = _parse_iso(concept.promoted_at)
    if promoted is None:
        # Active without a promotion stamp: pre-timeline row. Fall back
        # to "has it ever been reinforced at all".
        return not concept.last_reinforced_at
    reinforced = _parse_iso(concept.last_reinforced_at)
    if reinforced is None:
        return True
    return reinforced <= promoted


def engaged_days_to_floor(
    confidence: float, *, floor: float, halflife_days: float
) -> float | None:
    """Engaged days of pure decay before ``confidence`` reaches ``floor``.

    The honest cost of the current pruning settings. An engaged day is
    roughly an hour of active conversation (see the ``EngagementClock``
    note in ``concept_lifecycle_worker``), so the returned figure is
    conversation hours, not calendar days. Returns ``0.0`` when already
    at or below the floor, and ``None`` when the maths does not apply.

    Pass the *effective* (plasticity-damped) half-life, not the raw
    setting -- that is what the lifecycle worker decays against, and the
    two differ by up to 2x for a sticky kind.
    """
    if halflife_days <= 0 or floor <= 0:
        return None
    if confidence <= floor:
        return 0.0
    return round(halflife_days * math.log2(confidence / floor), 1)


# ── sections ──────────────────────────────────────────────────────────


def _totals(concepts: Sequence["Concept"]) -> dict[str, Any]:
    by_status: Counter[str] = Counter()
    by_subject: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    by_kind_subject: Counter[str] = Counter()
    for c in concepts:
        by_status[c.status] += 1
        by_subject[c.subject] += 1
        by_kind[c.kind] += 1
        by_kind_subject[f"{c.kind}/{c.subject}"] += 1
    return {
        "total": len(concepts),
        "by_status": dict(by_status),
        "by_subject": dict(by_subject),
        # by_kind is new here -- the existing snapshot's counts block has
        # only status + subject, which cannot show a single kind running
        # away from the rest.
        "by_kind": dict(by_kind),
        "by_kind_subject": dict(by_kind_subject),
    }


def _flow(
    concepts: Sequence["Concept"],
    event_counts: Mapping[str, int],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Production vs. pruning, from the append-only event timeline."""
    counts = {str(k): int(v) for k, v in (event_counts or {}).items()}
    discovered = counts.get("discovered", 0)
    promoted = counts.get("promoted", 0)
    dormant = counts.get("dormant", 0)
    retired = counts.get("retired", 0)

    # Observation window: first concept creation to now. Uses the concept
    # rows rather than the events so it stays correct for graphs that
    # predate the timeline table.
    created = [d for d in (_parse_iso(c.created_at) for c in concepts) if d]
    span_days = 0.0
    if created:
        span_days = max(
            0.0, (now - min(created)).total_seconds() / 86400.0
        )

    per_day = round(len(concepts) / span_days, 2) if span_days >= 1.0 else None

    return {
        "window_days": round(span_days, 1),
        "concepts_per_day": per_day,
        "events": counts,
        # The headline ratio: what share of everything proposed ends up
        # an accepted belief. A number close to 100 means the promotion
        # gate is not discriminating.
        "promotion_rate_pct": _pct(promoted, discovered),
        "reinforced_events": counts.get("reinforced", 0),
        "merged_events": counts.get("merged", 0),
        "contradicted_events": counts.get("contradicted", 0),
        # Demotions of any kind. Zero here alongside a healthy promotion
        # count means the graph only ever grows.
        "demotion_events": dormant + retired,
    }


def _confidence(concepts: Sequence["Concept"]) -> dict[str, Any]:
    per_status: dict[str, list[float]] = defaultdict(list)
    for c in concepts:
        per_status[c.status].append(float(c.confidence))
    high = sum(
        1 for c in concepts if float(c.confidence) >= _HIGH_CONFIDENCE
    )
    return {
        "by_status": {k: _stats(v) for k, v in sorted(per_status.items())},
        "high_confidence_pct": _pct(high, len(concepts)),
        "high_confidence_threshold": _HIGH_CONFIDENCE,
    }


def _evidence(
    concepts: Sequence["Concept"],
    thresholds: QualityThresholds,
    evidence_facts: Mapping[int, EvidenceFacts],
) -> dict[str, Any]:
    """Support distribution + concepts standing on less than they should."""
    histogram: Counter[int] = Counter()
    below_bar: list[dict[str, Any]] = []
    zero_source = 0
    single_cluster: list[dict[str, Any]] = []
    weak_memory: list[dict[str, Any]] = []

    bar = int(thresholds.promote_min_sources)
    for c in concepts:
        histogram[int(c.distinct_source_count)] += 1
        if c.status != "active":
            continue

        if int(c.distinct_source_count) < bar:
            if int(c.distinct_source_count) == 0:
                zero_source += 1
            below_bar.append({
                "id": int(c.concept_id),
                "kind": c.kind,
                "subject": c.subject,
                "distinct_source_count": int(c.distinct_source_count),
                "confidence": round(float(c.confidence), 3),
                "label": _preview(c.label),
            })

        facts = evidence_facts.get(int(c.concept_id))
        if facts is None:
            continue
        # Signal A: everything traces back to one topic cluster, which
        # means it was a topic, not a cross-cutting concept.
        if facts.cluster_span == 1 and int(c.distinct_source_count) >= bar:
            single_cluster.append({
                "id": int(c.concept_id),
                "kind": c.kind,
                "subject": c.subject,
                "distinct_source_count": int(c.distinct_source_count),
                "label": _preview(c.label),
            })
        # Signal B: the memories underneath are themselves low-confidence.
        mean_conf = facts.memory_confidence_mean
        if mean_conf is not None and mean_conf < 0.5:
            weak_memory.append({
                "id": int(c.concept_id),
                "kind": c.kind,
                "subject": c.subject,
                "evidence_memory_confidence_mean": round(mean_conf, 3),
                "label": _preview(c.label),
            })

    return {
        "distinct_source_histogram": {
            str(k): v for k, v in sorted(histogram.items())
        },
        "promote_min_sources": bar,
        # Active concepts holding less evidence than the bar they were
        # promoted through -- reachable when edges are reconciled away
        # after the fact without the status being re-gated.
        "active_below_bar": len(below_bar),
        "active_zero_source": zero_source,
        "active_below_bar_sample": below_bar[:_MAX_DUPLICATE_PAIRS],
        "single_cluster_active": len(single_cluster),
        "single_cluster_sample": single_cluster[:_MAX_DUPLICATE_PAIRS],
        "weak_memory_active": len(weak_memory),
        "weak_memory_sample": weak_memory[:_MAX_DUPLICATE_PAIRS],
        "evidence_facts_resolved": len(evidence_facts),
    }


def _duplicates(
    concepts: Sequence["Concept"], thresholds: QualityThresholds
) -> dict[str, Any]:
    """Paraphrase twins sitting just under the creation-time dedupe bar.

    Creation-time dedupe only fuses proposals at or above ``dedupe_cos``.
    Anything below lands as a separate row, so the interesting band is
    ``[duplicate_band_floor, dedupe_cos)`` -- close enough to be the same
    claim, far enough apart to have slipped through. Compared within a
    (kind, subject) group only: an ``identity`` and a ``value`` concept
    phrased alike are not duplicates of each other.
    """
    ceiling = float(thresholds.dedupe_cos)
    floor = float(thresholds.duplicate_band_floor)
    groups: dict[tuple[str, str], list["Concept"]] = defaultdict(list)
    for c in concepts:
        if c.status in ("retired", "contradicted"):
            continue
        embedding = getattr(c, "embedding", None)
        if embedding is None or int(np.asarray(embedding).size) == 0:
            continue
        groups[(c.kind, c.subject)].append(c)

    pairs: list[dict[str, Any]] = []
    total = 0
    for (kind, subject), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        matrix = np.vstack([
            np.asarray(m.embedding, dtype=np.float32).ravel()
            for m in members
        ])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        matrix = matrix / norms
        sims = matrix @ matrix.T
        # Upper triangle only, so each pair is considered once.
        rows, cols = np.triu_indices(len(members), k=1)
        hits = np.where((sims[rows, cols] >= floor) & (sims[rows, cols] < ceiling))[0]
        total += int(hits.size)
        for idx in hits:
            i, j = int(rows[idx]), int(cols[idx])
            pairs.append({
                "kind": kind,
                "subject": subject,
                "cosine": round(float(sims[i, j]), 4),
                "a": {
                    "id": int(members[i].concept_id),
                    "label": _preview(members[i].label),
                },
                "b": {
                    "id": int(members[j].concept_id),
                    "label": _preview(members[j].label),
                },
            })

    pairs.sort(key=lambda p: p["cosine"], reverse=True)
    return {
        "band": {"floor": round(floor, 3), "ceiling": round(ceiling, 3)},
        "pair_count": total,
        "pairs": pairs[:_MAX_DUPLICATE_PAIRS],
    }


def _register(concepts: Sequence["Concept"]) -> dict[str, Any]:
    """Per-(kind, subject) template concentration.

    Never aggregated across kinds -- see the module docstring. Each entry
    reports how many labels carry an interpretive frame, how many carry
    imported jargon, and how concentrated the opening words are, which
    together make a proposer collapsing onto one sentence shape visible
    as three numbers instead of a hunch.
    """
    buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)
    for c in concepts:
        if c.status in ("retired", "contradicted"):
            continue
        bucket = buckets[(c.kind, c.subject)]
        label = str(c.label or "")
        bucket.labels.append(label)
        if _FRAME_RE.search(label):
            bucket.frame_hits += 1
        if _JARGON_RE.search(label):
            bucket.jargon_hits += 1

    out: dict[str, Any] = {}
    for (kind, subject), bucket in buckets.items():
        n = len(bucket.labels)
        if not n:
            continue
        leads = Counter(
            " ".join(label.split()[:_LEAD_NGRAM]).lower()
            for label in bucket.labels
        )
        top_lead, top_count = leads.most_common(1)[0]
        out[f"{kind}/{subject}"] = {
            "n": n,
            "frame_pct": _pct(bucket.frame_hits, n),
            "jargon_pct": _pct(bucket.jargon_hits, n),
            "top_lead_ngram": top_lead,
            "top_lead_pct": _pct(top_count, n),
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


def _pruning(
    concepts: Sequence["Concept"],
    thresholds: QualityThresholds,
    *,
    now: datetime,
) -> dict[str, Any]:
    """How much is standing still, how fast more arrives, and how long
    decay would take to act.

    The standing count (``unreinforced_since_promotion``) is a *stock*, and
    a slow-moving one: on the current settings a stalled concept needs tens
    of hours of conversation to reach the dormant floor, so the stock barely
    responds to an intake change inside a week. The window figures below are
    the *flow*, which does respond immediately -- they are what to compare
    across a threshold change.

    Caveat on ``unreinforced_recent_pct``: a concept promoted yesterday has
    had almost no opportunity to be reinforced, so this reads high in
    absolute terms by construction. It is only meaningful compared against
    the same window measured at another time.
    """
    actives = [c for c in concepts if c.status == "active"]
    stalled = [c for c in actives if unreinforced_since_promotion(c)]

    # Per-concept plasticity matters: real decay runs on the *effective*
    # half-life (``halflife * (2 - plasticity)``), so a sticky identity trait
    # takes far longer to fade than the base setting suggests. Using the raw
    # half-life here understated the horizon by up to 2x and made the decay
    # settings look far more active than they are.
    horizons = [
        h
        for h in (
            engaged_days_to_floor(
                float(c.confidence),
                floor=thresholds.dormant_confidence_floor,
                halflife_days=effective_halflife(
                    thresholds.confidence_halflife_days, float(c.plasticity)
                ),
            )
            for c in stalled
        )
        if h is not None
    ]

    cutoff = now - timedelta(days=_RECENT_WINDOW_DAYS)
    promoted_dates = [
        d for d in (_parse_iso(c.promoted_at) for c in concepts) if d
    ]
    recent = [
        c
        for c in concepts
        if (d := _parse_iso(c.promoted_at)) is not None and d >= cutoff
    ]
    recent_stalled = [c for c in recent if unreinforced_since_promotion(c)]

    # Lifetime promotion rate, the companion to ``flow.concepts_per_day``:
    # measured over first-promotion to now rather than the concept-creation
    # span, so it is not diluted by a long pre-promotion history.
    span_days = 0.0
    if promoted_dates:
        span_days = max(
            0.0, (now - min(promoted_dates)).total_seconds() / 86400.0
        )
    per_day = (
        round(len(promoted_dates) / span_days, 2) if span_days >= 1.0 else None
    )

    return {
        "active": len(actives),
        "unreinforced_since_promotion": len(stalled),
        "unreinforced_pct": _pct(len(stalled), len(actives)),
        # Signal C was the only spurious signal without an id list (A and B
        # both sample), which left it countable but not inspectable -- and
        # made any targeted sweep of the backlog a fresh query.
        "unreinforced_sample": [
            {
                "id": int(c.concept_id),
                "kind": c.kind,
                "subject": c.subject,
                "confidence": round(float(c.confidence), 3),
                "promoted_at": c.promoted_at or "",
                "label": _preview(c.label),
            }
            for c in sorted(
                stalled, key=lambda c: str(c.promoted_at or ""), reverse=True
            )[:_MAX_DUPLICATE_PAIRS]
        ],
        "promotions_per_day": per_day,
        "recent_window_days": _RECENT_WINDOW_DAYS,
        "promoted_recent": len(recent),
        "promotions_per_day_recent": round(
            len(recent) / _RECENT_WINDOW_DAYS, 2
        ),
        # The intake-quality number: of what was promoted inside the window,
        # how much has already gone quiet. This is what a tighter promotion
        # gate is supposed to move.
        "unreinforced_recent": len(recent_stalled),
        "unreinforced_recent_pct": _pct(len(recent_stalled), len(recent)),
        "dormant_confidence_floor": thresholds.dormant_confidence_floor,
        "confidence_halflife_days": thresholds.confidence_halflife_days,
        # Engaged days ~= hours of active conversation. Presented as the
        # median so one sticky outlier does not distort the picture, and
        # computed per-concept against the plasticity-damped half-life.
        "median_engaged_days_to_dormant": (
            round(float(np.median(horizons)), 1) if horizons else None
        ),
        "max_engaged_days_to_dormant": (
            round(max(horizons), 1) if horizons else None
        ),
    }


# ── entry point ───────────────────────────────────────────────────────


def build_quality_report(
    concepts: "Iterable[Concept]",
    *,
    event_counts: Mapping[str, int] | None = None,
    evidence_facts: Mapping[int, EvidenceFacts] | None = None,
    thresholds: QualityThresholds | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate the concept layer into the L22 quality snapshot.

    ``concepts`` is every stored concept; ``event_counts`` maps
    ``event_type -> count`` over the whole timeline; ``evidence_facts``
    carries the per-concept graph joins the caller resolved (optional --
    signals A and B are simply omitted without it). Returns a plain dict
    suitable for JSON, kv_meta, or the debug panel.
    """
    rows = list(concepts)
    limits = thresholds or QualityThresholds()
    when = (now or timephrase.utcnow()).astimezone(timezone.utc)
    facts = dict(evidence_facts or {})

    return {
        "enabled": True,
        "generated_at": when.isoformat(),
        "thresholds": {
            "promote_min_sources": limits.promote_min_sources,
            "dedupe_cos": limits.dedupe_cos,
            "dormant_confidence_floor": limits.dormant_confidence_floor,
            "confidence_halflife_days": limits.confidence_halflife_days,
            "duplicate_band_floor": limits.duplicate_band_floor,
        },
        "totals": _totals(rows),
        "flow": _flow(rows, event_counts or {}, now=when),
        "confidence": _confidence(rows),
        "evidence": _evidence(rows, limits, facts),
        "duplicates": _duplicates(rows, limits),
        "register": _register(rows),
        "pruning": _pruning(rows, limits, now=when),
    }


def disabled_quality_report() -> dict[str, Any]:
    """Empty-but-valid shape for when the concept layer is off."""
    return {
        "enabled": False,
        "generated_at": "",
        "thresholds": {},
        "totals": {
            "total": 0,
            "by_status": {},
            "by_subject": {},
            "by_kind": {},
            "by_kind_subject": {},
        },
        "flow": {},
        "confidence": {},
        "evidence": {},
        "duplicates": {"band": {}, "pair_count": 0, "pairs": []},
        "register": {},
        "pruning": {},
    }


__all__ = [
    "EvidenceFacts",
    "QualityThresholds",
    "build_quality_report",
    "disabled_quality_report",
    "engaged_days_to_floor",
    "unreinforced_since_promotion",
]

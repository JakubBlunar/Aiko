"""Pure L42 detectors for Aiko's relationship-scoped surfacing conduct.

The L37 ledger records what reached the prompt; this module asks three slower,
different questions about that history:

* concentration -- does Aiko allocate much more topic space than the user's
  own conversation mix would predict?
* neglect -- which mature, high-confidence concepts does she almost never use?
* fixation -- which non-core concept repeatedly wins flex/activation slots
  despite landing below the relationship's normal engaged rate?

The functions are I/O-free and fail silent on thin data. Counts stay attached
for debug/fingerprints but must never be rendered to Aiko.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


CONDUCT_SHAPES = frozenset({"concentration", "neglect", "fixation"})
CONDUCT_SNAPSHOT_KEY = "concept.surfacing_conduct"
CONDUCT_LAST_RUN_KEY = "concept.surfacing_conduct.last_run"


@dataclass(frozen=True, slots=True)
class ConductFinding:
    """One observation about how Aiko has been showing up.

    ``summary`` is first-person, for the prompt snapshot she reads.
    ``second_person`` is the same observation addressed *to* her ("you
    keep..."), which is the register conduct concepts are stored in — it
    exists so the proposer can still mint a concept when the LLM naming
    pass comes back empty, instead of losing the finding entirely.
    """

    shape: str
    key: str
    summary: str
    evidence: tuple[tuple[str, int], ...]
    score: float
    surfaced: int = 0
    settled: int = 0
    engaged: int = 0
    observed_share: float | None = None
    expected_share: float | None = None
    second_person: str = ""

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.shape,
            self.key,
            tuple(self.evidence),
            round(float(self.score), 3),
            self.surfaced,
            self.settled,
            self.engaged,
        )

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "key": self.key,
            "summary": self.summary,
            "evidence": [[kind, int(item_id)] for kind, item_id in self.evidence],
            "score": round(float(self.score), 4),
        }

    def with_evidence(
        self,
        extra: Iterable[tuple[str, int]],
        *,
        cap: int = 8,
    ) -> "ConductFinding":
        merged = list(dict.fromkeys((*self.evidence, *tuple(extra))))
        return ConductFinding(
            shape=self.shape,
            key=self.key,
            summary=self.summary,
            evidence=tuple(merged[: max(2, int(cap))]),
            score=self.score,
            surfaced=self.surfaced,
            settled=self.settled,
            engaged=self.engaged,
            observed_share=self.observed_share,
            expected_share=self.expected_share,
            second_person=self.second_person,
        )


def load_conduct_snapshot(kv_get) -> list[dict[str, Any]]:
    try:
        raw = kv_get(CONDUCT_SNAPSHOT_KEY)
        parsed = json.loads(raw) if raw else []
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        row
        for row in parsed
        if isinstance(row, dict)
        and str(row.get("shape", "")) in CONDUCT_SHAPES
    ]


def save_conduct_snapshot(
    kv_set,
    findings: Sequence[ConductFinding],
    *,
    cap: int = 6,
) -> None:
    payload = [
        finding.as_snapshot()
        for finding in findings[: max(1, int(cap))]
    ]
    try:
        kv_set(
            CONDUCT_SNAPSHOT_KEY,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        )
    except Exception:
        return


def map_user_topic_counts(
    vector_rows: Iterable[tuple[str, Any]],
    topic_graph: Any,
    *,
    min_similarity: float = 0.45,
) -> tuple[dict[int, int], int]:
    """Map indexed user-message vectors onto their best live topic cluster."""
    counts: dict[int, int] = {}
    mapped = 0
    for _created_at, vector in vector_rows:
        try:
            matches = topic_graph.best_clusters_for(
                vector, top_n=1, min_sim=float(min_similarity),
            )
        except Exception:
            continue
        if not matches:
            continue
        cid = int(matches[0][0])
        if cid <= 0:
            continue
        counts[cid] = counts.get(cid, 0) + 1
        mapped += 1
    return counts, mapped


def _note(
    reading: "dict[str, Any] | None", outcome: str, why: str,
) -> None:
    """Record how a detector ended, for the per-run gate reading.

    A detector that declines is indistinguishable from one that never ran,
    which is how two of the three spent months unreachable without anyone
    noticing: establishing that ``min_top_gap`` needed 0.10 against a
    measured 0.049 took a hand audit of the ledger. The reading carries
    each gate's bar next to the best value the data actually offered, so
    the next calibration is a read rather than an investigation.
    """
    if reading is None:
        return
    reading["outcome"] = outcome
    if why:
        reading["declined_on"] = why


def detect_concentration(
    cluster_stats: Mapping[int, Any],
    user_topic_counts: Mapping[int, int],
    cluster_reps: Mapping[int, tuple[int, str]],
    *,
    min_total_settled: int = 50,
    min_user_turns: int = 20,
    min_cluster_settled: int = 8,
    min_share: float = 0.30,
    min_excess: float = 0.12,
    min_ratio: float = 2.0,
    min_top_gap: float = 0.10,
    reading: "dict[str, Any] | None" = None,
) -> ConductFinding | None:
    total_settled = sum(
        max(0, int(getattr(row, "settled", 0) or 0))
        for row in cluster_stats.values()
    )
    total_user = sum(max(0, int(count or 0)) for count in user_topic_counts.values())
    if reading is not None:
        reading.update({
            "total_settled": total_settled,
            "min_total_settled": min_total_settled,
            "total_user_turns": total_user,
            "min_user_turns": min_user_turns,
        })
    if total_settled < min_total_settled or total_user < min_user_turns:
        _note(reading, "declined", "not_enough_history")
        return None
    ranked: list[tuple[float, int, float, float, Any]] = []
    best_share = best_excess = best_ratio = 0.0
    for cid, row in cluster_stats.items():
        settled = max(0, int(getattr(row, "settled", 0) or 0))
        if settled < min_cluster_settled or int(cid) not in cluster_reps:
            continue
        observed = settled / total_settled
        expected = max(0, int(user_topic_counts.get(int(cid), 0))) / total_user
        excess = observed - expected
        ratio = observed / max(expected, 0.02)
        best_share = max(best_share, observed)
        best_excess = max(best_excess, excess)
        best_ratio = max(best_ratio, ratio)
        if observed >= min_share and excess >= min_excess and ratio >= min_ratio:
            ranked.append((observed, int(cid), expected, excess, row))
    if reading is not None:
        reading.update({
            "best_share": round(best_share, 4), "min_share": min_share,
            "best_excess": round(best_excess, 4), "min_excess": min_excess,
            "best_ratio": round(best_ratio, 4), "min_ratio": min_ratio,
        })
    if not ranked:
        _note(reading, "declined", "no_cluster_clears_the_bars")
        return None
    ranked.sort(reverse=True)
    top = ranked[0]
    second_share = max(
        (
            max(0, int(getattr(row, "settled", 0) or 0)) / total_settled
            for cid, row in cluster_stats.items()
            if int(cid) != top[1]
        ),
        default=0.0,
    )
    if reading is not None:
        reading.update({
            "top_gap": round(top[0] - second_share, 4),
            "min_top_gap": min_top_gap,
        })
    if top[0] - second_share < min_top_gap:
        _note(reading, "declined", "top_gap")
        return None
    rep_id, label = cluster_reps[top[1]]
    comparison_reps = [
        int(cluster_reps[cid][0])
        for cid, _row in sorted(
            cluster_stats.items(),
            key=lambda pair: int(getattr(pair[1], "settled", 0) or 0),
            reverse=True,
        )
        if int(cid) in cluster_reps and int(cid) != top[1]
    ][:2]
    evidence = tuple(
        [("cluster", int(rep_id))]
        + [("cluster", rep) for rep in comparison_reps if rep > 0]
    )
    if len(evidence) < 2:
        _note(reading, "declined", "too_few_comparison_clusters")
        return None
    row = top[4]
    _note(reading, "fired", "")
    return ConductFinding(
        shape="concentration",
        key=f"cluster:{top[1]}",
        summary=f"I keep steering our attention toward {label or 'one familiar topic'}",
        second_person=(
            "You keep steering your attention toward "
            f"{label or 'one familiar topic'}"
        ),
        evidence=evidence,
        score=min(1.0, max(0.0, top[3] / max(min_excess, 0.01) * 0.5)),
        surfaced=int(getattr(row, "surfaced", 0) or 0),
        settled=int(getattr(row, "settled", 0) or 0),
        engaged=int(getattr(row, "engaged", 0) or 0),
        observed_share=top[0],
        expected_share=top[2],
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def detect_neglect(
    concepts: Sequence[Any],
    concept_stats: Mapping[int, Any],
    *,
    now: datetime,
    min_confidence: float = 0.75,
    min_age_days: float = 14.0,
    max_surfaced: int = 1,
    min_candidates: int = 3,
    profile_kinds: frozenset[str] = frozenset({"identity", "value"}),
    profile_min_confidence: float = 0.5,
) -> ConductFinding | None:
    eligible: list[Any] = []
    for concept in concepts:
        cid = int(getattr(concept, "concept_id", 0) or 0)
        kind = str(getattr(concept, "kind", "") or "")
        subject = str(getattr(concept, "subject", "") or "")
        confidence = float(getattr(concept, "confidence", 0.0) or 0.0)
        if cid <= 0 or kind == "conduct" or confidence < min_confidence:
            continue
        # T0 profile concepts are present every turn but intentionally absent
        # from L37's T3 ledger; calling them neglected would invert the truth.
        if (
            subject == "user"
            and kind in profile_kinds
            and confidence >= profile_min_confidence
        ):
            continue
        born = _parse_time(
            getattr(concept, "promoted_at", None)
            or getattr(concept, "first_evidence_at", None)
            or getattr(concept, "created_at", None)
        )
        if born is None or (now - born).total_seconds() < min_age_days * 86400:
            continue
        surfaced = int(getattr(concept_stats.get(cid), "surfaced", 0) or 0)
        if surfaced <= max_surfaced:
            eligible.append(concept)
    if len(eligible) < min_candidates:
        return None
    eligible.sort(
        key=lambda c: (
            float(getattr(c, "confidence", 0.0) or 0.0),
            int(getattr(c, "evidence_count", 0) or 0),
        ),
        reverse=True,
    )
    chosen = eligible[:5]
    labels = [str(getattr(c, "label", "") or "").strip() for c in chosen[:3]]
    evidence = tuple(
        ("concept", int(getattr(c, "concept_id", 0) or 0)) for c in chosen
    )
    return ConductFinding(
        shape="neglect",
        key="concepts:" + ",".join(str(item_id) for _kind, item_id in evidence),
        summary="I hold parts of this understanding quietly without bringing them forward: "
        + "; ".join(label for label in labels if label),
        second_person=(
            "You hold parts of your understanding quietly without bringing "
            "them forward: "
            + "; ".join(label for label in labels if label)
        ),
        evidence=evidence,
        score=min(1.0, 0.5 + 0.1 * len(eligible)),
    )


def detect_fixation(
    concepts: Sequence[Any],
    flex_stats: Mapping[int, Any],
    *,
    engaged_baseline: float,
    core_kinds: frozenset[str],
    min_surfaced: int = 12,
    min_settled: int = 6,
    min_frequency_ratio: float = 3.0,
    min_rate_gap: float = 0.05,
    reading: "dict[str, Any] | None" = None,
) -> ConductFinding | None:
    by_id = {
        int(getattr(concept, "concept_id", 0) or 0): concept
        for concept in concepts
        if str(getattr(concept, "kind", "") or "") not in core_kinds
        and str(getattr(concept, "kind", "") or "") != "conduct"
    }
    ranked = sorted(
        (
            (int(getattr(row, "surfaced", 0) or 0), cid, row)
            for cid, row in flex_stats.items()
            if cid in by_id
        ),
        reverse=True,
    )
    if not ranked:
        _note(reading, "declined", "no_flex_candidates")
        return None
    surfaced, cid, row = ranked[0]
    settled = int(getattr(row, "settled", 0) or 0)
    engaged = int(getattr(row, "engaged", 0) or 0)
    second = ranked[1][0] if len(ranked) > 1 else 0
    rate = engaged / settled if settled > 0 else None
    if reading is not None:
        reading.update({
            "top_surfaced": surfaced, "min_surfaced": min_surfaced,
            "top_settled": settled, "min_settled": min_settled,
            "frequency_ratio": round(surfaced / max(1, second), 4),
            "min_frequency_ratio": min_frequency_ratio,
            "top_engaged_rate": round(rate, 4) if rate is not None else None,
            "engaged_baseline": round(float(engaged_baseline), 4),
            # The premise: fixation is "she keeps raising something he does
            # not care about", so the top concept has to be engaged with
            # *less* than usual. A rate at or above baseline means the shape
            # is simply not present, which is a finding in itself.
            "engaged_rate_ceiling": round(
                float(engaged_baseline) - min_rate_gap, 4
            ),
        })
    if (
        surfaced < min_surfaced
        or settled < min_settled
        or rate is None
        or rate > float(engaged_baseline) - min_rate_gap
        or surfaced / max(1, second) < min_frequency_ratio
    ):
        _note(reading, "declined", "gates")
        return None
    support = [other_cid for _count, other_cid, _row in ranked[1:3]]
    evidence = tuple(
        [("concept", cid)] + [("concept", other) for other in support]
    )
    if len(evidence) < 2:
        _note(reading, "declined", "too_few_comparison_concepts")
        return None
    _note(reading, "fired", "")
    label = str(getattr(by_id[cid], "label", "") or "").strip()
    return ConductFinding(
        shape="fixation",
        key=f"concept:{cid}",
        summary=(
            f"I keep returning to {label or 'the same interpretation'} more "
            "than it seems to open things up"
        ),
        second_person=(
            f"You keep returning to {label or 'the same interpretation'} more "
            "than it seems to open things up"
        ),
        evidence=evidence,
        score=min(1.0, surfaced / max(min_surfaced, 1) * 0.5),
        surfaced=surfaced,
        settled=settled,
        engaged=engaged,
    )


def detect_conduct(
    *,
    cluster_stats: Mapping[int, Any],
    user_topic_counts: Mapping[int, int],
    cluster_reps: Mapping[int, tuple[int, str]],
    concepts: Sequence[Any],
    concept_stats: Mapping[int, Any],
    flex_stats: Mapping[int, Any],
    engaged_baseline: float,
    core_kinds: frozenset[str],
    now: datetime,
    settings: Any,
    readings: "dict[str, dict[str, Any]] | None" = None,
) -> list[ConductFinding]:
    """Run all three self-observation detectors over one window.

    ``readings`` opts into the per-detector gate reading: each shape's bar
    next to the best value its data actually offered this run. It is how a
    detector that has never fired is told apart from one that is simply
    unreachable -- see :func:`_note`.
    """
    take = (lambda shape: readings.setdefault(shape, {})) if (
        readings is not None
    ) else (lambda shape: None)
    findings = [
        detect_concentration(
            cluster_stats,
            user_topic_counts,
            cluster_reps,
            min_total_settled=int(
                getattr(settings, "conduct_min_settled_rows", 50)
            ),
            min_user_turns=int(
                getattr(settings, "conduct_min_user_turns", 20)
            ),
            min_cluster_settled=int(
                getattr(settings, "conduct_concentration_min_settled", 8)
            ),
            min_share=float(
                getattr(settings, "conduct_concentration_min_share", 0.30)
            ),
            min_excess=float(
                getattr(settings, "conduct_concentration_min_excess", 0.12)
            ),
            min_ratio=float(
                getattr(settings, "conduct_concentration_min_ratio", 2.0)
            ),
            min_top_gap=float(
                getattr(settings, "conduct_concentration_min_top_gap", 0.10)
            ),
            reading=take("concentration"),
        ),
        detect_neglect(
            concepts,
            concept_stats,
            now=now,
            min_confidence=float(
                getattr(settings, "conduct_neglect_min_confidence", 0.75)
            ),
            min_age_days=float(
                getattr(settings, "conduct_neglect_min_age_days", 14.0)
            ),
            max_surfaced=int(
                getattr(settings, "conduct_neglect_max_surfaced", 1)
            ),
            min_candidates=int(
                getattr(settings, "conduct_neglect_min_candidates", 3)
            ),
            profile_min_confidence=float(
                getattr(settings, "profile_concept_min_confidence", 0.5)
            ),
        ),
        detect_fixation(
            concepts,
            flex_stats,
            engaged_baseline=engaged_baseline,
            core_kinds=core_kinds,
            min_surfaced=int(
                getattr(settings, "conduct_fixation_min_surfaced", 12)
            ),
            min_settled=int(
                getattr(settings, "conduct_fixation_min_settled", 6)
            ),
            min_frequency_ratio=float(
                getattr(settings, "conduct_fixation_min_ratio", 3.0)
            ),
            min_rate_gap=float(
                getattr(settings, "conduct_fixation_min_rate_gap", 0.05)
            ),
            reading=take("fixation"),
        ),
    ]
    return [finding for finding in findings if finding is not None]


__all__ = [
    "CONDUCT_SHAPES",
    "CONDUCT_LAST_RUN_KEY",
    "CONDUCT_SNAPSHOT_KEY",
    "ConductFinding",
    "detect_concentration",
    "detect_conduct",
    "detect_fixation",
    "detect_neglect",
    "map_user_topic_counts",
    "load_conduct_snapshot",
    "save_conduct_snapshot",
]

"""Tests for the L22 concept-quality scorer (``concept_quality``).

The module is pure, so these build ``Concept`` rows directly rather than
standing up a store. Cases are written against the failure modes the
first month of real use actually produced -- a runaway promotion rate, a
graph that never prunes, paraphrase twins under the dedupe bar, and one
proposer collapsing onto a single sentence shape -- so a regression in
the scorer shows up as the wrong diagnosis, not just a wrong number.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.concepts.concept_quality import (
    EvidenceFacts,
    QualityThresholds,
    build_quality_report,
    disabled_quality_report,
    engaged_days_to_floor,
    unreinforced_since_promotion,
)
from app.core.concepts.concept_store import Concept

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _concept(
    label: str = "Jacob likes cold brew",
    *,
    cid: int = 1,
    kind: str = "identity",
    subject: str = "user",
    status: str = "active",
    confidence: float = 0.8,
    plasticity: float = 0.5,
    sources: int = 3,
    created_days_ago: float = 10.0,
    promoted_days_ago: float | None = 9.0,
    reinforced_days_ago: float | None = None,
    embedding: np.ndarray | None = None,
) -> Concept:
    return Concept(
        label=label,
        kind=kind,
        subject=subject,
        status=status,
        confidence=confidence,
        plasticity=plasticity,
        evidence_count=sources,
        distinct_source_count=sources,
        created_at=_iso(created_days_ago),
        promoted_at=(
            _iso(promoted_days_ago) if promoted_days_ago is not None else None
        ),
        last_reinforced_at=(
            _iso(reinforced_days_ago)
            if reinforced_days_ago is not None
            else None
        ),
        embedding=(
            embedding
            if embedding is not None
            else np.zeros(0, dtype=np.float32)
        ),
        concept_id=cid,
    )


def _report(concepts, **kwargs):
    kwargs.setdefault("now", NOW)
    return build_quality_report(concepts, **kwargs)


class UnreinforcedSincePromotionTests(unittest.TestCase):
    """L22 signal C. This is the one that fired on 83% of the live graph,
    so its edges matter more than most."""

    def test_never_reinforced_is_flagged(self) -> None:
        c = _concept(promoted_days_ago=9.0, reinforced_days_ago=None)
        self.assertTrue(unreinforced_since_promotion(c))

    def test_reinforced_after_promotion_is_clean(self) -> None:
        c = _concept(promoted_days_ago=9.0, reinforced_days_ago=2.0)
        self.assertFalse(unreinforced_since_promotion(c))

    def test_reinforcement_predating_promotion_still_counts_as_stalled(
        self,
    ) -> None:
        # The evidence that *caused* promotion is not evidence the belief
        # kept earning its place afterwards.
        c = _concept(promoted_days_ago=5.0, reinforced_days_ago=8.0)
        self.assertTrue(unreinforced_since_promotion(c))

    def test_candidates_are_excluded(self) -> None:
        # Not promoted yet, so the predicate is meaningless for them.
        c = _concept(status="candidate", promoted_days_ago=None)
        self.assertFalse(unreinforced_since_promotion(c))

    def test_active_without_promotion_stamp_falls_back(self) -> None:
        # Pre-timeline rows carry no promoted_at; fall back to "ever
        # reinforced at all" rather than silently reading as healthy.
        stalled = _concept(promoted_days_ago=None, reinforced_days_ago=None)
        touched = _concept(promoted_days_ago=None, reinforced_days_ago=1.0)
        self.assertTrue(unreinforced_since_promotion(stalled))
        self.assertFalse(unreinforced_since_promotion(touched))


class EngagedDaysToFloorTests(unittest.TestCase):
    def test_one_halflife_halves_confidence(self) -> None:
        self.assertAlmostEqual(
            engaged_days_to_floor(0.8, floor=0.4, halflife_days=45.0),
            45.0,
            places=1,
        )

    def test_live_defaults_expose_the_pruning_gap(self) -> None:
        # 0.85 down to the 0.35 dormant floor on a 45-engaged-day
        # half-life. An engaged day is ~1h of conversation, so this is
        # the number that explains zero demotions in a month of use.
        days = engaged_days_to_floor(0.85, floor=0.35, halflife_days=45.0)
        self.assertIsNotNone(days)
        self.assertGreater(days, 50.0)
        self.assertLess(days, 60.0)

    def test_already_below_floor_is_zero(self) -> None:
        self.assertEqual(
            engaged_days_to_floor(0.2, floor=0.35, halflife_days=45.0), 0.0
        )

    def test_degenerate_settings_return_none(self) -> None:
        self.assertIsNone(
            engaged_days_to_floor(0.8, floor=0.35, halflife_days=0.0)
        )
        self.assertIsNone(
            engaged_days_to_floor(0.8, floor=0.0, halflife_days=45.0)
        )


class TotalsTests(unittest.TestCase):
    def test_counts_split_by_kind_not_only_status_and_subject(self) -> None:
        # by_kind is the axis the pre-existing snapshot lacked, and the
        # one that shows a single proposer running away from the rest.
        report = _report([
            _concept(cid=1, kind="identity", subject="user"),
            _concept(cid=2, kind="identity", subject="user"),
            _concept(cid=3, kind="value", subject="aiko"),
            _concept(cid=4, kind="value", subject="aiko", status="candidate"),
        ])
        totals = report["totals"]
        self.assertEqual(totals["total"], 4)
        self.assertEqual(totals["by_kind"], {"identity": 2, "value": 2})
        self.assertEqual(totals["by_subject"], {"user": 2, "aiko": 2})
        self.assertEqual(totals["by_status"], {"active": 3, "candidate": 1})
        self.assertEqual(
            totals["by_kind_subject"], {"identity/user": 2, "value/aiko": 2}
        )


class FlowTests(unittest.TestCase):
    def test_promotion_rate_and_demotions(self) -> None:
        report = _report(
            [_concept(cid=i) for i in range(1, 11)],
            event_counts={
                "discovered": 100,
                "promoted": 91,
                "reinforced": 12,
                "merged": 2,
                "dormant": 3,
                "retired": 1,
            },
        )
        flow = report["flow"]
        self.assertEqual(flow["promotion_rate_pct"], 91.0)
        self.assertEqual(flow["demotion_events"], 4)
        self.assertEqual(flow["reinforced_events"], 12)
        self.assertEqual(flow["merged_events"], 2)

    def test_zero_demotions_is_reported_not_hidden(self) -> None:
        report = _report(
            [_concept()], event_counts={"discovered": 50, "promoted": 50}
        )
        self.assertEqual(report["flow"]["demotion_events"], 0)
        self.assertEqual(report["flow"]["promotion_rate_pct"], 100.0)

    def test_production_rate_uses_oldest_concept_as_window_start(self) -> None:
        concepts = [
            _concept(cid=1, created_days_ago=20.0),
            _concept(cid=2, created_days_ago=1.0),
        ]
        flow = _report(concepts)["flow"]
        self.assertEqual(flow["window_days"], 20.0)
        self.assertAlmostEqual(flow["concepts_per_day"], 0.1, places=2)

    def test_sub_day_window_reports_no_rate(self) -> None:
        # Dividing two concepts by a few minutes would print nonsense.
        flow = _report([_concept(created_days_ago=0.01)])["flow"]
        self.assertIsNone(flow["concepts_per_day"])

    def test_empty_event_log_does_not_divide_by_zero(self) -> None:
        flow = _report([_concept()], event_counts={})["flow"]
        self.assertEqual(flow["promotion_rate_pct"], 0.0)


class ConfidenceTests(unittest.TestCase):
    def test_stats_grouped_by_status(self) -> None:
        report = _report([
            _concept(cid=1, status="active", confidence=0.9),
            _concept(cid=2, status="active", confidence=0.7),
            _concept(cid=3, status="contradicted", confidence=0.3),
        ])
        active = report["confidence"]["by_status"]["active"]
        self.assertEqual(active["n"], 2)
        self.assertAlmostEqual(active["mean"], 0.8, places=3)
        self.assertAlmostEqual(active["min"], 0.7, places=3)
        self.assertEqual(
            report["confidence"]["by_status"]["contradicted"]["n"], 1
        )

    def test_top_heaviness_is_visible(self) -> None:
        report = _report([
            _concept(cid=1, confidence=0.95),
            _concept(cid=2, confidence=0.85),
            _concept(cid=3, confidence=0.4),
        ])
        self.assertAlmostEqual(
            report["confidence"]["high_confidence_pct"], 66.7, places=1
        )


class EvidenceTests(unittest.TestCase):
    def test_active_below_promotion_bar_is_counted(self) -> None:
        report = _report([
            _concept(cid=1, sources=0),
            _concept(cid=2, sources=1),
            _concept(cid=3, sources=4),
        ])
        evidence = report["evidence"]
        self.assertEqual(evidence["active_below_bar"], 2)
        self.assertEqual(evidence["active_zero_source"], 1)
        self.assertEqual(len(evidence["active_below_bar_sample"]), 2)

    def test_candidates_do_not_count_against_the_bar(self) -> None:
        # A candidate is *supposed* to sit below the promotion bar.
        report = _report([_concept(status="candidate", sources=1)])
        self.assertEqual(report["evidence"]["active_below_bar"], 0)

    def test_signal_a_flags_single_cluster_evidence(self) -> None:
        # Three memories from one cluster: distinct_source_count says 3,
        # but it is one topic wearing a concept's clothes.
        report = _report(
            [
                _concept(cid=1, sources=3),
                _concept(cid=2, sources=3),
            ],
            evidence_facts={
                1: EvidenceFacts(cluster_span=1),
                2: EvidenceFacts(cluster_span=3),
            },
        )
        evidence = report["evidence"]
        self.assertEqual(evidence["single_cluster_active"], 1)
        self.assertEqual(evidence["single_cluster_sample"][0]["id"], 1)

    def test_signal_b_flags_weak_supporting_memories(self) -> None:
        report = _report(
            [_concept(cid=1), _concept(cid=2)],
            evidence_facts={
                1: EvidenceFacts(memory_confidences=(0.2, 0.3)),
                2: EvidenceFacts(memory_confidences=(0.9, 0.8)),
            },
        )
        evidence = report["evidence"]
        self.assertEqual(evidence["weak_memory_active"], 1)
        self.assertEqual(evidence["weak_memory_sample"][0]["id"], 1)

    def test_signals_absent_without_evidence_facts(self) -> None:
        # A/B need graph joins the pure layer cannot do; their absence
        # must read as "unknown", never as "clean".
        report = _report([_concept(cid=1)])
        self.assertEqual(report["evidence"]["single_cluster_active"], 0)
        self.assertEqual(report["evidence"]["evidence_facts_resolved"], 0)

    def test_histogram_covers_every_status(self) -> None:
        report = _report([
            _concept(cid=1, sources=2),
            _concept(cid=2, sources=2),
            _concept(cid=3, sources=5, status="candidate"),
        ])
        histogram = report["evidence"]["distinct_source_histogram"]
        self.assertEqual(histogram["2"], 2)
        self.assertEqual(histogram["5"], 1)


class EvidenceFactsTests(unittest.TestCase):
    def test_empty_confidences_read_as_unknown(self) -> None:
        facts = EvidenceFacts()
        self.assertIsNone(facts.memory_confidence_mean)
        self.assertIsNone(facts.memory_confidence_min)

    def test_mean_and_min(self) -> None:
        facts = EvidenceFacts(memory_confidences=(0.4, 0.6, 0.8))
        self.assertAlmostEqual(facts.memory_confidence_mean, 0.6, places=3)
        self.assertAlmostEqual(facts.memory_confidence_min, 0.4, places=3)


class DuplicateTests(unittest.TestCase):
    @staticmethod
    def _vec(*values: float) -> np.ndarray:
        return np.asarray(values, dtype=np.float32)

    def test_pairs_below_the_dedupe_bar_are_found(self) -> None:
        # Two near-parallel vectors: close enough to be the same claim,
        # below the 0.9 creation-time bar so they landed as two rows.
        report = _report(
            [
                _concept(cid=1, embedding=self._vec(1.0, 0.0)),
                _concept(cid=2, embedding=self._vec(0.88, 0.475)),
            ],
            thresholds=QualityThresholds(
                dedupe_cos=0.9, duplicate_band_floor=0.78
            ),
        )
        duplicates = report["duplicates"]
        self.assertEqual(duplicates["pair_count"], 1)
        self.assertEqual(
            {duplicates["pairs"][0]["a"]["id"], duplicates["pairs"][0]["b"]["id"]},
            {1, 2},
        )

    def test_pairs_above_the_bar_are_not_reported(self) -> None:
        # At/above the dedupe bar is creation-time's job, not this sweep's.
        report = _report(
            [
                _concept(cid=1, embedding=self._vec(1.0, 0.0)),
                _concept(cid=2, embedding=self._vec(1.0, 0.02)),
            ],
            thresholds=QualityThresholds(dedupe_cos=0.9),
        )
        self.assertEqual(report["duplicates"]["pair_count"], 0)

    def test_distant_pairs_are_not_reported(self) -> None:
        report = _report([
            _concept(cid=1, embedding=self._vec(1.0, 0.0)),
            _concept(cid=2, embedding=self._vec(0.0, 1.0)),
        ])
        self.assertEqual(report["duplicates"]["pair_count"], 0)

    def test_different_kinds_are_never_paired(self) -> None:
        # An identity and a value concept phrased alike are not twins.
        report = _report([
            _concept(cid=1, kind="identity", embedding=self._vec(1.0, 0.0)),
            _concept(
                cid=2, kind="value", embedding=self._vec(0.88, 0.475)
            ),
        ])
        self.assertEqual(report["duplicates"]["pair_count"], 0)

    def test_retired_and_contradicted_are_excluded(self) -> None:
        report = _report([
            _concept(cid=1, embedding=self._vec(1.0, 0.0)),
            _concept(
                cid=2,
                status="contradicted",
                embedding=self._vec(0.88, 0.475),
            ),
        ])
        self.assertEqual(report["duplicates"]["pair_count"], 0)

    def test_unembedded_concepts_are_skipped_not_crashed(self) -> None:
        report = _report([
            _concept(cid=1, embedding=self._vec(1.0, 0.0)),
            _concept(cid=2),  # no embedding yet
        ])
        self.assertEqual(report["duplicates"]["pair_count"], 0)


class RegisterTests(unittest.TestCase):
    def test_interpretive_frame_is_detected(self) -> None:
        report = _report([
            _concept(
                cid=1,
                label=(
                    "Jacob treats linguistic typos as high-fidelity data "
                    "artifacts that validate intimacy"
                ),
            ),
            _concept(cid=2, label="Jacob drinks cold brew every morning"),
        ])
        entry = report["register"]["identity/user"]
        self.assertEqual(entry["n"], 2)
        self.assertEqual(entry["frame_pct"], 50.0)
        self.assertEqual(entry["jargon_pct"], 50.0)

    def test_plain_behavioural_labels_score_clean(self) -> None:
        report = _report([
            _concept(cid=1, label="Jacob drinks cold brew every morning"),
            _concept(cid=2, label="Jacob sings Powerwolf in the shower"),
        ])
        entry = report["register"]["identity/user"]
        self.assertEqual(entry["frame_pct"], 0.0)
        self.assertEqual(entry["jargon_pct"], 0.0)

    def test_value_register_is_not_penalised(self) -> None:
        # The regression guard for Phase 3: a normative "I value X over Y"
        # statement is the value kind working correctly and must not read
        # as contamination just because it states a belief.
        report = _report([
            _concept(
                cid=1,
                kind="value",
                subject="aiko",
                label=(
                    "I value Jacob's raw emotional transparency over "
                    "polished performance"
                ),
            ),
            _concept(
                cid=2,
                kind="value",
                subject="aiko",
                label=(
                    "I value preserving my own agency by correcting my own "
                    "mistakes"
                ),
            ),
        ])
        entry = report["register"]["value/aiko"]
        self.assertEqual(entry["frame_pct"], 0.0)
        self.assertEqual(entry["jargon_pct"], 0.0)

    def test_scores_are_per_kind_never_pooled(self) -> None:
        # A global figure would average a collapsed proposer against a
        # healthy one and hide both.
        report = _report([
            _concept(
                cid=1,
                kind="identity",
                subject="user",
                label="Jacob treats cookies as a low-stakes protocol",
            ),
            _concept(
                cid=2,
                kind="value",
                subject="aiko",
                label="I value honesty over comfort",
            ),
        ])
        register = report["register"]
        self.assertEqual(register["identity/user"]["frame_pct"], 100.0)
        self.assertEqual(register["value/aiko"]["frame_pct"], 0.0)

    def test_lead_ngram_concentration_exposes_template_collapse(self) -> None:
        report = _report([
            _concept(cid=1, label="Jacob treats the bath as a system idle"),
            _concept(cid=2, label="Jacob treats the commute as a buffer"),
            _concept(cid=3, label="Jacob enjoys long walks"),
        ])
        entry = report["register"]["identity/user"]
        self.assertEqual(entry["top_lead_ngram"], "jacob treats the")
        self.assertAlmostEqual(entry["top_lead_pct"], 66.7, places=1)


class PruningTests(unittest.TestCase):
    def test_stalled_share_and_horizon(self) -> None:
        report = _report(
            [
                _concept(cid=1, confidence=0.85, reinforced_days_ago=None),
                _concept(cid=2, confidence=0.85, reinforced_days_ago=None),
                _concept(cid=3, confidence=0.85, reinforced_days_ago=1.0),
            ],
            thresholds=QualityThresholds(
                dormant_confidence_floor=0.35, confidence_halflife_days=45.0
            ),
        )
        pruning = report["pruning"]
        self.assertEqual(pruning["active"], 3)
        self.assertEqual(pruning["unreinforced_since_promotion"], 2)
        self.assertAlmostEqual(pruning["unreinforced_pct"], 66.7, places=1)
        # 45-day base at plasticity 0.5 => a 67.5-day effective half-life,
        # and 0.85 -> 0.35 is ~1.28 half-lives.
        self.assertAlmostEqual(
            pruning["median_engaged_days_to_dormant"], 86.4, places=1
        )

    def test_horizon_uses_the_plasticity_damped_halflife(self) -> None:
        # The number the decay pass will be argued from, so it has to be the
        # one the lifecycle worker actually decays against. A sticky concept
        # (low plasticity) takes up to 2x the base half-life to fade; reading
        # the raw setting understated every horizon in the report.
        sticky = _report(
            [_concept(cid=1, confidence=0.8, plasticity=0.0)],
            thresholds=QualityThresholds(
                dormant_confidence_floor=0.4, confidence_halflife_days=45.0
            ),
        )["pruning"]
        fluid = _report(
            [_concept(cid=1, confidence=0.8, plasticity=1.0)],
            thresholds=QualityThresholds(
                dormant_confidence_floor=0.4, confidence_halflife_days=45.0
            ),
        )["pruning"]
        # 0.8 -> 0.4 is exactly one half-life: 2x the base when p=0, 1x at p=1.
        self.assertAlmostEqual(
            sticky["median_engaged_days_to_dormant"], 90.0, places=1
        )
        self.assertAlmostEqual(
            fluid["median_engaged_days_to_dormant"], 45.0, places=1
        )

    def test_healthy_graph_reports_no_horizon(self) -> None:
        report = _report([_concept(reinforced_days_ago=1.0)])
        pruning = report["pruning"]
        self.assertEqual(pruning["unreinforced_since_promotion"], 0)
        self.assertIsNone(pruning["median_engaged_days_to_dormant"])

    def test_recent_window_separates_new_intake_from_the_backlog(self) -> None:
        # The standing count cannot move inside a week (the horizons above
        # are tens of hours of conversation), so the window figures are what
        # a tightened promotion gate is measured by.
        report = _report([
            _concept(cid=1, promoted_days_ago=60.0, reinforced_days_ago=None),
            _concept(cid=2, promoted_days_ago=40.0, reinforced_days_ago=None),
            _concept(cid=3, promoted_days_ago=2.0, reinforced_days_ago=None),
            _concept(cid=4, promoted_days_ago=1.0, reinforced_days_ago=0.5),
        ])
        pruning = report["pruning"]
        self.assertEqual(pruning["unreinforced_since_promotion"], 3)
        self.assertEqual(pruning["recent_window_days"], 7.0)
        self.assertEqual(pruning["promoted_recent"], 2)
        self.assertEqual(pruning["unreinforced_recent"], 1)
        self.assertAlmostEqual(pruning["unreinforced_recent_pct"], 50.0)

    def test_promotions_per_day_spans_first_promotion_to_now(self) -> None:
        # Four promotions over a 20-day promotion history => 0.2/day.
        report = _report([
            _concept(cid=1, promoted_days_ago=20.0),
            _concept(cid=2, promoted_days_ago=15.0),
            _concept(cid=3, promoted_days_ago=10.0),
            _concept(cid=4, promoted_days_ago=5.0),
        ])
        self.assertAlmostEqual(report["pruning"]["promotions_per_day"], 0.2)

    def test_promotions_per_day_is_none_below_a_day(self) -> None:
        # Same guard as flow.concepts_per_day: no rate from a sub-day span.
        report = _report([_concept(cid=1, promoted_days_ago=0.2)])
        self.assertIsNone(report["pruning"]["promotions_per_day"])

    def test_candidates_are_excluded_from_the_window(self) -> None:
        # A candidate has no promotion to count, even if it carries a stale
        # promoted_at from an earlier active spell that was demoted.
        report = _report([
            _concept(cid=1, status="candidate", promoted_days_ago=None),
            _concept(cid=2, promoted_days_ago=1.0, reinforced_days_ago=None),
        ])
        pruning = report["pruning"]
        self.assertEqual(pruning["promoted_recent"], 1)
        self.assertEqual(pruning["unreinforced_recent"], 1)

    def test_stalled_sample_is_inspectable_and_newest_first(self) -> None:
        # Signal C had no id list, which left it countable but not
        # actionable -- a targeted sweep needed a fresh query every time.
        report = _report([
            _concept(cid=1, promoted_days_ago=30.0, reinforced_days_ago=None),
            _concept(cid=2, promoted_days_ago=3.0, reinforced_days_ago=None),
            _concept(cid=3, promoted_days_ago=5.0, reinforced_days_ago=1.0),
        ])
        sample = report["pruning"]["unreinforced_sample"]
        self.assertEqual([row["id"] for row in sample], [2, 1])
        self.assertEqual(sample[0]["kind"], "identity")
        self.assertIn("cold brew", sample[0]["label"])


class ReportShapeTests(unittest.TestCase):
    def test_empty_graph_does_not_crash(self) -> None:
        report = _report([])
        self.assertTrue(report["enabled"])
        self.assertEqual(report["totals"]["total"], 0)
        self.assertEqual(report["duplicates"]["pair_count"], 0)
        self.assertEqual(report["register"], {})

    def test_thresholds_are_echoed_for_the_reader(self) -> None:
        report = _report(
            [_concept()],
            thresholds=QualityThresholds(
                promote_min_sources=3, dedupe_cos=0.85
            ),
        )
        self.assertEqual(report["thresholds"]["promote_min_sources"], 3)
        self.assertEqual(report["thresholds"]["dedupe_cos"], 0.85)

    def test_disabled_shape_matches_the_enabled_keys(self) -> None:
        # Callers should never need to special-case the off path.
        enabled = set(_report([]).keys())
        self.assertEqual(set(disabled_quality_report().keys()), enabled)
        self.assertFalse(disabled_quality_report()["enabled"])


if __name__ == "__main__":
    unittest.main()

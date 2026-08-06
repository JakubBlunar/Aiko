"""L17b: the pure drift classifier."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.concepts.concept_drift import (
    ConceptTrace,
    DriftThresholds,
    SuccessionCandidate,
    TrajectoryPoint,
    build_traces,
    classify_succession,
    classify_trajectory,
    detect_drift,
    evidence_overlap,
    is_material_relabel,
    label_tokens,
    normalize_label,
    plasticity_weight,
)


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _point(
    event_id: int,
    event_type: str,
    *,
    days_ago: float = 10.0,
    label: str = "",
    confidence: float = 0.5,
) -> TrajectoryPoint:
    return TrajectoryPoint(
        event_id=event_id,
        event_type=event_type,
        label=label,
        confidence=confidence,
        created_at=_iso(days_ago),
    )


def _trace(
    concept_id: int,
    *,
    label: str = "a belief",
    kind: str = "identity",
    subject: str = "user",
    status: str = "active",
    confidence: float = 0.8,
    plasticity: float = 0.3,
    born_days_ago: float = 60.0,
    held: bool = True,
    points: tuple[TrajectoryPoint, ...] = (),
    evidence: tuple[tuple[str, str], ...] = (),
) -> ConceptTrace:
    """A trace for a belief that was genuinely held, unless ``held=False``.

    ``held`` is the reinforcement pair the ``loss`` gate reads: evidence
    landed on it after promotion, so a later fade is a real change of
    mind. ``held=False`` models the one-shot inference nothing ever
    confirmed, whose fade must not count as learning.
    """
    return ConceptTrace(
        concept_id=concept_id,
        kind=kind,
        subject=subject,
        label=label,
        status=status,
        confidence=confidence,
        plasticity=plasticity,
        first_evidence_at=_iso(born_days_ago),
        promoted_at=_iso(born_days_ago - 1.0),
        last_reinforced_at=_iso(born_days_ago - 2.0) if held else "",
        points=points,
        evidence_refs=frozenset(evidence),
    )


class LabelHelperTests(unittest.TestCase):
    def test_normalize_strips_case_and_punctuation(self) -> None:
        self.assertEqual(
            normalize_label("  Likes Detailed Answers!  "),
            normalize_label("likes detailed answers"),
        )

    def test_tokens_drop_filler(self) -> None:
        self.assertNotIn("the", label_tokens("the depth of the answer"))
        self.assertIn("depth", label_tokens("the depth of the answer"))

    def test_cosmetic_change_is_not_material(self) -> None:
        self.assertFalse(
            is_material_relabel("Likes detailed answers.", "likes detailed answers")
        )

    def test_filler_only_change_is_not_material(self) -> None:
        self.assertFalse(
            is_material_relabel(
                "he really tends to like depth", "he likes depth"
            )
        )

    def test_real_rewording_is_material(self) -> None:
        self.assertTrue(
            is_material_relabel(
                "likes detailed answers", "prefers depth calibrated to the topic"
            )
        )

    def test_empty_new_label_is_never_material(self) -> None:
        self.assertFalse(is_material_relabel("something", "   "))


class PlasticityWeightTests(unittest.TestCase):
    def test_sticky_beliefs_weigh_more_than_fluid_ones(self) -> None:
        self.assertGreater(plasticity_weight(0.2), plasticity_weight(0.5))

    def test_malformed_plasticity_is_neutral(self) -> None:
        self.assertEqual(plasticity_weight(float("nan")), 1.0)
        self.assertEqual(plasticity_weight(None), 1.0)  # type: ignore[arg-type]
        self.assertEqual(plasticity_weight(0.0), 1.0)
        self.assertEqual(plasticity_weight(4.2), 1.0)


class TrajectoryShapeTests(unittest.TestCase):
    def test_promotion_is_emergence(self) -> None:
        trace = _trace(
            1,
            points=(
                _point(10, "discovered", days_ago=40, confidence=0.3),
                _point(11, "promoted", days_ago=5, confidence=0.8),
            ),
        )
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "emergence")
        self.assertEqual(finding.decisive_event_id, 11)

    def test_retirement_is_loss(self) -> None:
        trace = _trace(
            2,
            status="retired",
            points=(
                _point(20, "promoted", days_ago=40, confidence=0.8),
                _point(21, "retired", days_ago=2, confidence=0.2),
            ),
        )
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "loss")
        self.assertEqual(finding.resolution, "no longer held")

    def test_a_finding_is_dated_by_its_decisive_event(self) -> None:
        # The backfill classifies years of history in one pass; if findings
        # were dated by detection, all of it would land on the day the
        # sweep ran and the self-history would read as one huge afternoon.
        trace = _trace(
            17,
            status="retired",
            points=(
                _point(170, "promoted", days_ago=40, confidence=0.8),
                _point(171, "retired", days_ago=25, confidence=0.2),
            ),
        )
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.occurred_at, _iso(25.0))
        self.assertEqual(finding.detected_at, NOW.isoformat())

    def test_revival_is_its_own_shape(self) -> None:
        trace = _trace(
            3,
            points=(
                _point(30, "promoted", days_ago=60, confidence=0.8),
                _point(31, "dormant", days_ago=30, confidence=0.3),
                _point(32, "revived", days_ago=1, confidence=0.7),
            ),
        )
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "revival")

    def test_relabel_uses_the_preceding_wording_as_old(self) -> None:
        trace = _trace(
            4,
            label="prefers depth calibrated to the topic",
            points=(
                _point(
                    40, "promoted", days_ago=40,
                    label="likes detailed answers", confidence=0.8,
                ),
                _point(
                    41, "relabeled", days_ago=1,
                    label="prefers depth calibrated to the topic",
                    confidence=0.8,
                ),
            ),
        )
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "relabel")
        self.assertEqual(finding.old_label, "likes detailed answers")
        self.assertEqual(
            finding.new_label, "prefers depth calibrated to the topic"
        )

    def test_cosmetic_relabel_is_rejected(self) -> None:
        trace = _trace(
            5,
            points=(
                _point(50, "promoted", days_ago=40, label="Likes depth."),
                _point(51, "relabeled", days_ago=1, label="likes depth"),
            ),
        )
        self.assertIsNone(
            classify_trajectory(trace, now=NOW, thresholds=DriftThresholds())
        )

    def test_confidence_samples_alone_are_noise(self) -> None:
        trace = _trace(
            6,
            points=(
                _point(60, "confidence_sample", days_ago=20, confidence=0.80),
                _point(61, "confidence_sample", days_ago=10, confidence=0.78),
                _point(62, "confidence_sample", days_ago=1, confidence=0.76),
            ),
        )
        self.assertIsNone(
            classify_trajectory(trace, now=NOW, thresholds=DriftThresholds())
        )

    def test_promotion_without_confidence_movement_is_noise(self) -> None:
        trace = _trace(
            7,
            points=(
                _point(70, "discovered", days_ago=40, confidence=0.79),
                _point(71, "promoted", days_ago=1, confidence=0.80),
            ),
        )
        self.assertIsNone(
            classify_trajectory(trace, now=NOW, thresholds=DriftThresholds())
        )

    def test_merge_cleanup_is_not_evolution(self) -> None:
        trace = _trace(
            8,
            points=(
                _point(80, "promoted", days_ago=40, confidence=0.4),
                _point(81, "merged", days_ago=1, confidence=0.9),
            ),
        )
        self.assertIsNone(
            classify_trajectory(trace, now=NOW, thresholds=DriftThresholds())
        )

    def test_bare_discovery_is_a_hypothesis_not_learning(self) -> None:
        trace = _trace(
            9,
            status="candidate",
            points=(_point(90, "discovered", days_ago=5, confidence=0.3),),
        )
        self.assertIsNone(
            classify_trajectory(trace, now=NOW, thresholds=DriftThresholds())
        )

    def test_young_concepts_are_skipped(self) -> None:
        trace = _trace(
            10,
            born_days_ago=0.5,
            points=(
                _point(100, "discovered", days_ago=0.5, confidence=0.2),
                _point(101, "promoted", days_ago=0.1, confidence=0.9),
            ),
        )
        self.assertIsNone(
            classify_trajectory(trace, now=NOW, thresholds=DriftThresholds())
        )

    def test_watermark_suppresses_already_seen_events(self) -> None:
        trace = _trace(
            11,
            points=(
                _point(110, "discovered", days_ago=40, confidence=0.3),
                _point(111, "promoted", days_ago=5, confidence=0.9),
            ),
        )
        self.assertIsNone(
            classify_trajectory(
                trace, now=NOW, thresholds=DriftThresholds(),
                since_event_id=111,
            )
        )

    def test_empty_and_malformed_traces_are_safe(self) -> None:
        self.assertIsNone(
            classify_trajectory(
                _trace(12, points=()), now=NOW, thresholds=DriftThresholds()
            )
        )
        broken = ConceptTrace(
            concept_id=13,
            first_evidence_at="not-a-date",
            points=(_point(130, "promoted", days_ago=5),),
        )
        # No usable birth date reads as age 0, so the age floor rejects it
        # rather than raising.
        self.assertIsNone(
            classify_trajectory(
                broken, now=NOW, thresholds=DriftThresholds()
            )
        )

    def test_sticky_kind_outscores_fluid_kind_on_equal_movement(self) -> None:
        points = (
            _point(140, "promoted", days_ago=40, confidence=0.3),
            _point(141, "retired", days_ago=2, confidence=0.9),
        )
        sticky = classify_trajectory(
            _trace(14, kind="value", plasticity=0.2, status="retired",
                   points=points),
            now=NOW, thresholds=DriftThresholds(),
        )
        fluid = classify_trajectory(
            _trace(15, kind="taste", plasticity=0.5, status="retired",
                   points=points),
            now=NOW, thresholds=DriftThresholds(),
        )
        assert sticky is not None and fluid is not None
        self.assertGreater(sticky.salience, fluid.salience)

    def test_conduct_trajectories_classify(self) -> None:
        trace = _trace(
            16,
            kind="conduct",
            subject="aiko",
            plasticity=0.4,
            label="I keep returning to the same reading",
            status="retired",
            points=(
                _point(160, "promoted", days_ago=40, confidence=0.3),
                _point(161, "retired", days_ago=2, confidence=0.8),
            ),
        )
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "loss")
        self.assertEqual(finding.subject, "aiko")


class HeldBeliefTests(unittest.TestCase):
    """A fade is only learning if the belief was ever actually held.

    Without this, ordinary graph maintenance -- a decay retune, the L22
    sweep that parks never-reinforced bootstrap rows -- would mint
    hundreds of "the support for X fell away" events dated to whichever
    afternoon it ran, and the diary would compose about a mass forgetting
    that never happened.
    """

    def _fade(self, cid: int, *, held: bool) -> ConceptTrace:
        return _trace(
            cid,
            status="retired",
            held=held,
            points=(
                _point(200, "promoted", days_ago=40, confidence=0.8),
                _point(201, "retired", days_ago=2, confidence=0.2),
            ),
        )

    def test_a_never_reinforced_fade_is_not_learning(self) -> None:
        self.assertIsNone(
            classify_trajectory(
                self._fade(20, held=False),
                now=NOW,
                thresholds=DriftThresholds(),
            )
        )

    def test_a_held_belief_that_fades_still_is(self) -> None:
        finding = classify_trajectory(
            self._fade(21, held=True), now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "loss")

    def test_reinforcement_must_postdate_the_promotion(self) -> None:
        # Bootstrap rows carry a reinforcement stamp copied from their
        # promotion. That is the promotion itself, not a confirmation.
        trace = ConceptTrace(
            concept_id=22,
            status="retired",
            first_evidence_at=_iso(60),
            promoted_at=_iso(50),
            last_reinforced_at=_iso(50),
            points=(
                _point(210, "promoted", days_ago=50, confidence=0.8),
                _point(211, "dormant", days_ago=2, confidence=0.2),
            ),
        )
        self.assertFalse(trace.ever_reinforced)
        self.assertIsNone(
            classify_trajectory(
                trace, now=NOW, thresholds=DriftThresholds()
            )
        )

    def test_the_gate_only_touches_losses(self) -> None:
        # Emergence and revival both require fresh evidence to happen at
        # all, so they are already safe and must not be gated.
        emergence = classify_trajectory(
            _trace(
                23,
                held=False,
                points=(
                    _point(220, "discovered", days_ago=40, confidence=0.3),
                    _point(221, "promoted", days_ago=5, confidence=0.85),
                ),
            ),
            now=NOW,
            thresholds=DriftThresholds(),
        )
        assert emergence is not None
        self.assertEqual(emergence.shape, "emergence")
        revival = classify_trajectory(
            _trace(
                24,
                held=False,
                points=(
                    _point(230, "promoted", days_ago=60, confidence=0.8),
                    _point(231, "dormant", days_ago=30, confidence=0.3),
                    _point(232, "revived", days_ago=1, confidence=0.7),
                ),
            ),
            now=NOW,
            thresholds=DriftThresholds(),
        )
        assert revival is not None
        self.assertEqual(revival.shape, "revival")

    def test_a_missing_promotion_stamp_trusts_the_reinforcement(self) -> None:
        # Older rows predate ``promoted_at``. A reinforcement stamp with
        # nothing to compare it against is still evidence it was held.
        trace = ConceptTrace(
            concept_id=25,
            status="retired",
            first_evidence_at=_iso(60),
            last_reinforced_at=_iso(20),
            points=(
                _point(240, "promoted", days_ago=50, confidence=0.8),
                _point(241, "retired", days_ago=2, confidence=0.2),
            ),
        )
        self.assertTrue(trace.ever_reinforced)
        finding = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "loss")


class SuccessionTests(unittest.TestCase):
    def _pair(
        self,
        *,
        cosine: float = 0.72,
        shared: bool = True,
        old_status: str = "retired",
        fade_days: float = 5.0,
        rise_days: float = 8.0,
        old_held: bool = True,
    ) -> SuccessionCandidate:
        shared_refs = (("memory", "1"), ("memory", "2"))
        old = _trace(
            100,
            label="likes detailed answers",
            status=old_status,
            plasticity=0.3,
            confidence=0.25,
            held=old_held,
            points=(
                _point(1000, "promoted", days_ago=90, confidence=0.8),
                _point(1001, "retired", days_ago=fade_days, confidence=0.25),
            ),
            evidence=shared_refs if shared else (("memory", "90"),),
        )
        new = _trace(
            200,
            label="prefers depth calibrated to the topic",
            status="active",
            plasticity=0.3,
            confidence=0.82,
            points=(
                _point(1002, "discovered", days_ago=30, confidence=0.3),
                _point(1003, "promoted", days_ago=rise_days, confidence=0.82),
            ),
            evidence=shared_refs + (("memory", "3"),),
        )
        return SuccessionCandidate(old=old, new=new, cosine=cosine)

    def test_shared_evidence_and_mid_cosine_pairs(self) -> None:
        finding = classify_succession(
            self._pair(), now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "succession")
        self.assertEqual(finding.concept_id, 200)
        self.assertEqual(finding.prior_concept_id, 100)
        self.assertEqual(finding.old_label, "likes detailed answers")
        self.assertEqual(finding.decisive_event_id, 1003)

    def test_the_pair_is_dated_by_the_rise(self) -> None:
        # Not by the fade: the belief changed when the replacement took
        # over. A bulk status pass supplying the fade must not be able to
        # redate a month-old refinement to the afternoon it ran.
        finding = classify_succession(
            self._pair(fade_days=0.0, rise_days=30.0),
            now=NOW,
            thresholds=DriftThresholds(),
        )
        assert finding is not None
        self.assertEqual(finding.occurred_at, _iso(30.0))
        self.assertEqual(finding.detected_at, NOW.isoformat())

    def test_a_never_reinforced_loser_still_pairs(self) -> None:
        # The loss gate is deliberately not applied here: a fade matched
        # to a rising, semantically-near replacement is much stronger
        # evidence that the belief was real than reinforcement counting
        # is, so "I used to think X, now Y" keeps its entry.
        finding = classify_succession(
            self._pair(old_held=False), now=NOW, thresholds=DriftThresholds()
        )
        assert finding is not None
        self.assertEqual(finding.shape, "succession")
        self.assertEqual(finding.prior_concept_id, 100)

    def test_pair_at_or_above_dedupe_bar_is_rejected(self) -> None:
        self.assertIsNone(
            classify_succession(
                self._pair(cosine=0.90), now=NOW,
                thresholds=DriftThresholds(),
            )
        )

    def test_unrelated_pair_is_rejected(self) -> None:
        self.assertIsNone(
            classify_succession(
                self._pair(cosine=0.30), now=NOW,
                thresholds=DriftThresholds(),
            )
        )

    def test_no_shared_evidence_needs_high_cosine(self) -> None:
        self.assertIsNone(
            classify_succession(
                self._pair(cosine=0.60, shared=False), now=NOW,
                thresholds=DriftThresholds(),
            )
        )
        strong = classify_succession(
            self._pair(cosine=0.80, shared=False), now=NOW,
            thresholds=DriftThresholds(),
        )
        self.assertIsNotNone(strong)

    def test_still_active_old_concept_is_not_superseded(self) -> None:
        self.assertIsNone(
            classify_succession(
                self._pair(old_status="active"), now=NOW,
                thresholds=DriftThresholds(),
            )
        )

    def test_distant_events_are_not_a_succession(self) -> None:
        self.assertIsNone(
            classify_succession(
                self._pair(fade_days=5.0, rise_days=400.0), now=NOW,
                thresholds=DriftThresholds(succession_window_days=120.0),
            )
        )

    def test_cross_subject_pairs_are_rejected(self) -> None:
        candidate = self._pair()
        crossed = SuccessionCandidate(
            old=candidate.old,
            new=ConceptTrace(
                concept_id=candidate.new.concept_id,
                kind=candidate.new.kind,
                subject="aiko",
                label=candidate.new.label,
                status=candidate.new.status,
                confidence=candidate.new.confidence,
                plasticity=candidate.new.plasticity,
                first_evidence_at=candidate.new.first_evidence_at,
                points=candidate.new.points,
                evidence_refs=candidate.new.evidence_refs,
            ),
            cosine=candidate.cosine,
        )
        self.assertIsNone(
            classify_succession(
                crossed, now=NOW, thresholds=DriftThresholds()
            )
        )

    def test_evidence_overlap_is_jaccard(self) -> None:
        left = _trace(1, evidence=(("memory", "1"), ("memory", "2")))
        right = _trace(2, evidence=(("memory", "2"), ("memory", "3")))
        self.assertAlmostEqual(evidence_overlap(left, right), 1 / 3)
        self.assertEqual(evidence_overlap(_trace(3), right), 0.0)


class DetectDriftTests(unittest.TestCase):
    def test_succession_suppresses_the_bare_loss_and_emergence(self) -> None:
        shared = (("memory", "1"), ("memory", "2"))
        old = _trace(
            100,
            label="likes detailed answers",
            status="retired",
            confidence=0.25,
            points=(
                _point(1000, "promoted", days_ago=90, confidence=0.8),
                _point(1001, "retired", days_ago=5, confidence=0.25),
            ),
            evidence=shared,
        )
        new = _trace(
            200,
            label="prefers depth calibrated to the topic",
            confidence=0.82,
            points=(
                _point(1002, "discovered", days_ago=30, confidence=0.3),
                _point(1003, "promoted", days_ago=8, confidence=0.82),
            ),
            evidence=shared,
        )
        findings = detect_drift(
            [old, new],
            [SuccessionCandidate(old=old, new=new, cosine=0.72)],
            now=NOW,
        )
        self.assertEqual([f.shape for f in findings], ["succession"])

    def test_unrelated_arcs_still_report(self) -> None:
        lost = _trace(
            300,
            status="retired",
            points=(
                _point(3000, "promoted", days_ago=90, confidence=0.85),
                _point(3001, "retired", days_ago=3, confidence=0.2),
            ),
        )
        findings = detect_drift([lost], [], now=NOW)
        self.assertEqual([f.shape for f in findings], ["loss"])

    def test_one_fading_belief_yields_a_single_successor(self) -> None:
        shared = (("memory", "1"), ("memory", "2"))
        old = _trace(
            100,
            status="retired",
            confidence=0.2,
            points=(
                _point(1000, "promoted", days_ago=90, confidence=0.8),
                _point(1001, "retired", days_ago=5, confidence=0.2),
            ),
            evidence=shared,
        )
        rival_a = _trace(
            201,
            label="prefers depth calibrated to the topic",
            points=(
                _point(1002, "discovered", days_ago=30, confidence=0.3),
                _point(1003, "promoted", days_ago=8, confidence=0.82),
            ),
            evidence=shared,
        )
        rival_b = _trace(
            202,
            label="wants thorough explanations",
            points=(
                _point(1004, "discovered", days_ago=30, confidence=0.3),
                _point(1005, "promoted", days_ago=9, confidence=0.7),
            ),
            evidence=(("memory", "1"),),
        )
        findings = detect_drift(
            [old, rival_a, rival_b],
            [
                SuccessionCandidate(old=old, new=rival_a, cosine=0.80),
                SuccessionCandidate(old=old, new=rival_b, cosine=0.60),
            ],
            now=NOW,
        )
        successions = [f for f in findings if f.shape == "succession"]
        self.assertEqual(len(successions), 1)
        self.assertEqual(successions[0].concept_id, 201)

    def test_findings_are_capped_and_sorted_by_salience(self) -> None:
        traces = [
            _trace(
                400 + i,
                status="retired",
                plasticity=0.2 + i * 0.05,
                points=(
                    _point(4000 + i * 2, "promoted", days_ago=90,
                           confidence=0.85),
                    _point(4001 + i * 2, "retired", days_ago=3,
                           confidence=0.2),
                ),
            )
            for i in range(6)
        ]
        findings = detect_drift(
            traces, [], now=NOW, thresholds=DriftThresholds(max_findings=3)
        )
        self.assertEqual(len(findings), 3)
        self.assertEqual(
            [f.salience for f in findings],
            sorted((f.salience for f in findings), reverse=True),
        )

    def test_low_salience_is_dropped(self) -> None:
        trace = _trace(
            500,
            status="retired",
            plasticity=1.0,
            points=(
                _point(5000, "promoted", days_ago=90, confidence=0.8),
                _point(5001, "retired", days_ago=3, confidence=0.2),
            ),
        )
        self.assertEqual(
            detect_drift(
                [trace], [], now=NOW,
                thresholds=DriftThresholds(min_salience=0.99),
            ),
            [],
        )

    def test_empty_input_is_empty_output(self) -> None:
        self.assertEqual(detect_drift([], [], now=NOW), [])


class FingerprintTests(unittest.TestCase):
    def _finding(self):
        trace = _trace(
            600,
            status="retired",
            points=(
                _point(6000, "promoted", days_ago=90, confidence=0.85),
                _point(6001, "retired", days_ago=3, confidence=0.2),
            ),
        )
        return classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )

    def test_fingerprint_is_stable_across_detections(self) -> None:
        first = self._finding()
        second = self._finding()
        assert first is not None and second is not None
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_fingerprint_ignores_cosmetic_label_differences(self) -> None:
        trace = _trace(
            700,
            label="Likes Depth.",
            status="retired",
            points=(
                _point(7000, "promoted", days_ago=90, confidence=0.85),
                _point(7001, "retired", days_ago=3, confidence=0.2),
            ),
        )
        other = _trace(
            700,
            label="likes depth",
            status="retired",
            points=(
                _point(7000, "promoted", days_ago=90, confidence=0.85),
                _point(7001, "retired", days_ago=3, confidence=0.2),
            ),
        )
        left = classify_trajectory(
            trace, now=NOW, thresholds=DriftThresholds()
        )
        right = classify_trajectory(
            other, now=NOW, thresholds=DriftThresholds()
        )
        assert left is not None and right is not None
        self.assertEqual(left.fingerprint(), right.fingerprint())

    def test_different_shapes_fingerprint_differently(self) -> None:
        loss = self._finding()
        assert loss is not None
        revived = _trace(
            600,
            points=(
                _point(6000, "promoted", days_ago=90, confidence=0.85),
                _point(6002, "revived", days_ago=1, confidence=0.7),
            ),
        )
        other = classify_trajectory(
            revived, now=NOW, thresholds=DriftThresholds()
        )
        assert other is not None
        self.assertNotEqual(loss.fingerprint(), other.fingerprint())


class ThresholdSettingsTests(unittest.TestCase):
    def test_missing_settings_fall_back_to_defaults(self) -> None:
        self.assertEqual(
            DriftThresholds.from_settings(None), DriftThresholds()
        )

        class Empty:
            pass

        self.assertEqual(
            DriftThresholds.from_settings(Empty()), DriftThresholds()
        )

    def test_settings_override_defaults(self) -> None:
        class Settings:
            concept_drift_min_salience = 0.8
            concept_drift_max_findings = 3

        limits = DriftThresholds.from_settings(Settings())
        self.assertEqual(limits.min_salience, 0.8)
        self.assertEqual(limits.max_findings, 3)


class BuildTracesTests(unittest.TestCase):
    def test_adapts_store_rows_and_skips_unpersisted(self) -> None:
        class Row:
            def __init__(self, cid: int) -> None:
                self.concept_id = cid
                self.kind = "value"
                self.subject = "aiko"
                self.label = "honesty over comfort"
                self.status = "active"
                self.confidence = 0.9
                self.plasticity = 0.2
                self.first_evidence_at = _iso(50)

        class Event:
            def __init__(self) -> None:
                self.event_id = 1
                self.event_type = "promoted"
                self.label = "honesty over comfort"
                self.confidence = 0.9
                self.created_at = _iso(5)

        traces = build_traces(
            [Row(5), Row(0)],
            {5: [Event()]},
            {5: [("memory", "7")]},
        )
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].concept_id, 5)
        self.assertEqual(traces[0].kind, "value")
        self.assertEqual(traces[0].evidence_refs, frozenset({("memory", "7")}))
        self.assertEqual(traces[0].points[0].event_type, "promoted")

    def test_missing_events_produce_an_empty_trajectory(self) -> None:
        class Row:
            concept_id = 9
            kind = "identity"
            subject = "user"
            label = "x"
            status = "active"
            confidence = 0.5
            plasticity = 0.3
            first_evidence_at = _iso(10)

        traces = build_traces([Row()], {})
        self.assertEqual(traces[0].points, ())


if __name__ == "__main__":
    unittest.main()

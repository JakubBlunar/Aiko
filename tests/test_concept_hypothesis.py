"""L30a: unsettledness + the hypothesis ranking blend.

The measurements these tests pin down came from a real 261-candidate
graph, and the two that matter most are counter-intuitive: candidate
confidence is *high* (median 0.82, so it cannot be the filter), and the
overwhelming majority of candidates are held back only by the promotion
age floor (so age cannot count as doubt).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.concepts.concept_hypothesis import (
    SETTLED_CONFIDENCE,
    SETTLED_SOURCES,
    HypothesisDetail,
    hypothesis_score,
    unsettledness,
)
from app.core.concepts.concept_importance import IMPORTANCE_NEUTRAL


def _c(sources: int, confidence: float, **kw):
    return SimpleNamespace(
        concept_id=kw.pop("concept_id", 1),
        distinct_source_count=sources,
        confidence=confidence,
        **kw,
    )


class UnsettlednessTests(unittest.TestCase):
    def test_a_fully_grounded_confident_belief_is_settled(self) -> None:
        self.assertEqual(
            unsettledness(_c(SETTLED_SOURCES, SETTLED_CONFIDENCE)), 0.0
        )

    def test_nothing_known_is_maximally_unsettled(self) -> None:
        self.assertEqual(unsettledness(_c(0, 0.0)), 1.0)

    def test_two_sources_at_full_conviction_lands_on_the_calibration_point(
        self,
    ) -> None:
        # This exact number is why ``hypothesis_min_unsettled`` defaults to
        # 0.22 rather than a round 0.2 or 0.25. On the measured graph the
        # single largest cluster of candidates (84 of 261) is "twice
        # grounded, fully confident, waiting only on the age floor", and
        # they all land here. The floor has to sit *above* this point or
        # the lane fills with beliefs Aiko is not actually unsure about.
        self.assertAlmostEqual(
            unsettledness(_c(2, SETTLED_CONFIDENCE)), 0.20, places=6
        )

    def test_age_is_not_a_factor(self) -> None:
        # The whole design hinges on this. Two identical beliefs, one
        # minted moments ago and one months old, are equally unsettled --
        # being young is not the same as being doubted.
        young = _c(1, 0.6, created_at="2026-08-07T10:00:00+00:00")
        old = _c(1, 0.6, created_at="2020-01-01T00:00:00+00:00")
        self.assertEqual(unsettledness(young), unsettledness(old))

    def test_thin_evidence_outweighs_high_confidence(self) -> None:
        # A single-source belief the proposer felt great about is still an
        # open question; a well-corroborated shakier one is closer to
        # settled. Evidence leads because that is what a question can fix.
        loud_but_thin = unsettledness(_c(1, 0.95))
        grounded_but_unsure = unsettledness(_c(3, 0.60))
        self.assertGreater(loud_but_thin, grounded_but_unsure)

    def test_extra_evidence_beyond_the_bar_does_not_go_negative(self) -> None:
        self.assertEqual(unsettledness(_c(50, 1.0)), 0.0)

    def test_monotonic_in_both_inputs(self) -> None:
        self.assertLess(unsettledness(_c(2, 0.5)), unsettledness(_c(1, 0.5)))
        self.assertLess(unsettledness(_c(2, 0.7)), unsettledness(_c(2, 0.5)))

    def test_always_in_range(self) -> None:
        for sources in (0, 1, 2, 3, 9):
            for conf in (0.0, 0.3, 0.72, 0.99, 1.0):
                value = unsettledness(_c(sources, conf))
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_custom_bars_shift_the_scale(self) -> None:
        concept = _c(2, 0.6)
        strict = unsettledness(
            concept, settled_sources=6, settled_confidence=0.9
        )
        lax = unsettledness(
            concept, settled_sources=1, settled_confidence=0.5
        )
        self.assertGreater(strict, lax)
        self.assertEqual(lax, 0.0)

    def test_a_malformed_row_reads_as_settled_not_fascinating(self) -> None:
        # Degrading the other way would turn every junk row into the
        # highest-ranked open question, which is the failure mode this
        # lane can least afford.
        junk = SimpleNamespace(
            concept_id=1, distinct_source_count="lots", confidence="high",
        )
        self.assertEqual(unsettledness(junk), 0.0)

    def test_missing_attributes_do_not_raise(self) -> None:
        self.assertIsInstance(unsettledness(SimpleNamespace()), float)


class ScoreTests(unittest.TestCase):
    def test_every_term_can_veto(self) -> None:
        # A product, not a sum: no single strong signal can carry a
        # candidate into the prompt on its own.
        self.assertEqual(hypothesis_score(cosine=0.0, unsettled=0.9), 0.0)
        self.assertEqual(hypothesis_score(cosine=0.9, unsettled=0.0), 0.0)
        self.assertEqual(
            hypothesis_score(cosine=0.9, unsettled=0.9, habituation=0.0), 0.0
        )

    def test_neutral_importance_leaves_the_product_alone(self) -> None:
        plain = hypothesis_score(cosine=0.6, unsettled=0.5)
        neutral = hypothesis_score(
            cosine=0.6, unsettled=0.5,
            importance=IMPORTANCE_NEUTRAL, importance_strength=0.4,
        )
        self.assertAlmostEqual(plain, neutral, places=9)

    def test_zero_strength_disables_the_importance_axis(self) -> None:
        plain = hypothesis_score(cosine=0.6, unsettled=0.5)
        weighty = hypothesis_score(
            cosine=0.6, unsettled=0.5,
            importance=0.9, importance_strength=0.0,
        )
        self.assertAlmostEqual(plain, weighty, places=9)

    def test_importance_separates_two_equally_open_questions(self) -> None:
        # The whole reason L32 shipped before this: a boundary Aiko is
        # unsure about should outrank an equally-unsure tooling taste.
        weighty = hypothesis_score(
            cosine=0.6, unsettled=0.4,
            importance=0.9, importance_strength=0.4,
        )
        trivial = hypothesis_score(
            cosine=0.6, unsettled=0.4,
            importance=0.3, importance_strength=0.4,
        )
        self.assertGreater(weighty, trivial)

    def test_being_more_unsettled_outranks_being_more_on_topic_only_so_far(
        self,
    ) -> None:
        # Unsettledness can overturn a small cosine lead...
        self.assertGreater(
            hypothesis_score(cosine=0.5, unsettled=0.6),
            hypothesis_score(cosine=0.55, unsettled=0.4),
        )
        # ...but not a large one. An off-topic question stays quiet.
        self.assertLess(
            hypothesis_score(cosine=0.2, unsettled=0.9),
            hypothesis_score(cosine=0.9, unsettled=0.3),
        )

    def test_habituation_damps_a_repeat(self) -> None:
        fresh = hypothesis_score(cosine=0.7, unsettled=0.5, habituation=1.0)
        shown = hypothesis_score(cosine=0.7, unsettled=0.5, habituation=0.35)
        self.assertLess(shown, fresh)

    def test_stays_in_range(self) -> None:
        hottest = hypothesis_score(
            cosine=1.0, unsettled=1.0,
            importance=1.0, importance_strength=1.0, habituation=1.0,
        )
        self.assertGreaterEqual(hottest, 0.0)
        self.assertLessEqual(hottest, 1.0)


class DetailTests(unittest.TestCase):
    def test_trace_keeps_the_inputs_separable(self) -> None:
        trace = HypothesisDetail(
            concept_id=4, score=0.31, cosine=0.62, unsettled=0.4,
            importance=0.9, habituation=1.0,
        ).as_trace()
        self.assertEqual(trace["lane"], "hypothesis")
        self.assertEqual(trace["cosine"], 0.62)
        self.assertEqual(trace["unsettled"], 0.4)
        self.assertEqual(trace["importance"], 0.9)


class ContractTests(unittest.TestCase):
    def test_settled_bars_match_the_promotion_gate(self) -> None:
        # "Settled" here has to mean what the L3 lifecycle engine means by
        # it, or the lane invents a second standard for the same word and
        # the two drift apart silently.
        from app.core.infra.memory_settings import MemorySettings

        defaults = MemorySettings()
        self.assertEqual(
            SETTLED_SOURCES,
            int(getattr(defaults, "concept_promote_young_min_sources", 3)),
        )
        self.assertEqual(
            SETTLED_CONFIDENCE,
            float(
                getattr(defaults, "concept_promote_young_min_confidence", 0.72)
            ),
        )

    def test_the_default_floor_excludes_the_merely_young(self) -> None:
        # Guards the calibration against a well-meaning future edit: the
        # shipped floor must stay above the "two sources, fully confident"
        # point, or 84 of the 261 measured candidates flood back in.
        from app.core.infra.memory_settings import MemorySettings

        floor = float(MemorySettings().hypothesis_min_unsettled)
        self.assertGreater(floor, unsettledness(_c(2, SETTLED_CONFIDENCE)))
        # ...and below a single-source belief, which genuinely is open.
        self.assertLess(floor, unsettledness(_c(1, SETTLED_CONFIDENCE)))


if __name__ == "__main__":
    unittest.main()

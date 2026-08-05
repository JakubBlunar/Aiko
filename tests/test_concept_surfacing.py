"""Tests for the L18 composite surfacing scorer (``concept_surfacing``).

Covers the pure helpers used to rank the turn-relevant concept fill by a
per-kind blend of context (cosine) + confidence + recency:

* ``recency_boost`` half-life decay + neutral fallback on missing/junk,
* ``composite_score`` normalization, the default-weights == cosine back-compat
  guarantee, and that a recency-heavy weight set reorders a fresher-but-lower
  cosine concept above a stale-but-higher cosine one.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.concepts.concept_kinds import DEFAULT_SURFACE_WEIGHTS, SurfaceWeights
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.concepts.concept_surfacing import (
    SURFACE_REASON_LABELS,
    composite_score,
    earned_standing,
    engagement_baseline,
    event_charge,
    event_charge_detail,
    habituation_factor,
    load_habituation,
    load_standing,
    recency_boost,
    salience,
    save_habituation,
    save_standing,
    stability,
    surface_reason,
    surface_score,
    turns_since_surfaced,
)

_UTC = timezone.utc
_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=_UTC)


class EarnedStandingTests(unittest.TestCase):
    def test_relationship_baseline_uses_pooled_settled_rows(self) -> None:
        stats = {
            1: SimpleNamespace(settled=10, engaged=2),
            2: SimpleNamespace(settled=30, engaged=12),
        }
        self.assertAlmostEqual(engagement_baseline(stats), 0.35)
        self.assertEqual(engagement_baseline({}), 0.5)

    def test_dynamic_baseline_maps_to_neutral(self) -> None:
        self.assertAlmostEqual(
            earned_standing(
                engaged=3, settled=10, baseline=0.3, prior_strength=10
            ),
            0.5,
        )

    def test_shrinkage_and_asymmetric_safe_floor(self) -> None:
        above = earned_standing(
            engaged=10, settled=10, baseline=0.3, prior_strength=10
        )
        below = earned_standing(
            engaged=0, settled=10, baseline=0.3, prior_strength=10
        )
        self.assertAlmostEqual(above, 0.75)
        self.assertAlmostEqual(below, 0.425)
        self.assertGreater(above - 0.5, 0.5 - below)

    def test_cold_and_malformed_data_are_neutral(self) -> None:
        self.assertEqual(
            earned_standing(engaged=3, settled=3, baseline=0.3), 0.5
        )
        self.assertEqual(
            earned_standing(engaged="bad", settled=10, baseline=0.3), 0.5
        )
        self.assertEqual(
            earned_standing(engaged=3, settled=10, baseline=float("nan")), 0.5
        )

    def test_bounds_and_protected_kinds_never_drop_below_neutral(self) -> None:
        low = earned_standing(
            engaged=0, settled=100, baseline=0.3, floor=0.35
        )
        protected = earned_standing(
            engaged=0, settled=100, baseline=0.3, floor=0.35,
            protect_downward=True,
        )
        self.assertGreaterEqual(low, 0.35)
        self.assertEqual(protected, 0.5)

    def test_bounded_kv_round_trip_prunes_and_skips_junk(self) -> None:
        kv: dict[str, str] = {}
        save_standing(
            kv.__setitem__, {3: 0.7, 1: 0.4, 2: 2.0, -1: 0.9}, cap=2
        )
        self.assertEqual(load_standing(kv.get), {1: 0.4, 2: 1.0})
        kv["concept.earned_standing"] = '{"1":"bad","2":0.8,"3":NaN}'
        self.assertEqual(load_standing(kv.get), {2: 0.8})


class RecencyBoostTests(unittest.TestCase):
    def test_fresh_is_one(self) -> None:
        self.assertAlmostEqual(
            recency_boost(_NOW.isoformat(), _NOW, halflife_days=14.0), 1.0
        )

    def test_halflife_decay(self) -> None:
        past = (_NOW - timedelta(days=14.0)).isoformat()
        self.assertAlmostEqual(
            recency_boost(past, _NOW, halflife_days=14.0), 0.5, places=4
        )
        two = (_NOW - timedelta(days=28.0)).isoformat()
        self.assertAlmostEqual(
            recency_boost(two, _NOW, halflife_days=14.0), 0.25, places=4
        )

    def test_missing_or_junk_is_neutral(self) -> None:
        # A missing/unparseable timestamp must never *suppress* a concept.
        self.assertEqual(recency_boost(None, _NOW, 14.0), 1.0)
        self.assertEqual(recency_boost("", _NOW, 14.0), 1.0)
        self.assertEqual(recency_boost("not-a-date", _NOW, 14.0), 1.0)

    def test_nonpositive_halflife_is_neutral(self) -> None:
        past = (_NOW - timedelta(days=100.0)).isoformat()
        self.assertEqual(recency_boost(past, _NOW, 0.0), 1.0)

    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive = (_NOW.replace(tzinfo=None)).isoformat()
        self.assertAlmostEqual(
            recency_boost(naive, _NOW, halflife_days=14.0), 1.0, places=4
        )


class CompositeScoreTests(unittest.TestCase):
    def test_default_weights_equal_cosine(self) -> None:
        # The default (context-only) blend is exactly the pre-L18 ranking.
        for cos in (0.0, 0.3, 0.77, 1.0):
            self.assertAlmostEqual(
                composite_score(
                    cosine=cos, confidence=0.9, recency=0.1,
                    w=DEFAULT_SURFACE_WEIGHTS,
                ),
                cos,
            )

    def test_normalized_to_unit_range(self) -> None:
        w = SurfaceWeights(context=0.5, confidence=0.2, recency=0.3)
        score = composite_score(cosine=1.0, confidence=1.0, recency=1.0, w=w)
        self.assertAlmostEqual(score, 1.0)
        score0 = composite_score(cosine=0.0, confidence=0.0, recency=0.0, w=w)
        self.assertAlmostEqual(score0, 0.0)

    def test_zero_weights_fall_back_to_cosine(self) -> None:
        w = SurfaceWeights(context=0.0, confidence=0.0, recency=0.0)
        self.assertAlmostEqual(
            composite_score(cosine=0.42, confidence=1.0, recency=1.0, w=w),
            0.42,
        )

    def test_recency_heavy_reorders_fresh_above_stale(self) -> None:
        # Boundary-like weights: a fresher, slightly-less-relevant concept
        # should outrank a stale, slightly-more-relevant one.
        w = SurfaceWeights(
            context=0.5, confidence=0.2, recency=0.3, recency_halflife_days=14.0
        )
        fresh = composite_score(
            cosine=0.55, confidence=0.8,
            recency=recency_boost(_NOW.isoformat(), _NOW, 14.0), w=w,
        )
        stale = composite_score(
            cosine=0.62, confidence=0.8,
            recency=recency_boost(
                (_NOW - timedelta(days=60.0)).isoformat(), _NOW, 14.0
            ),
            w=w,
        )
        self.assertGreater(fresh, stale)

    def test_context_only_weights_keep_cosine_order(self) -> None:
        # With the default context-only weights, recency can't flip the order.
        w = DEFAULT_SURFACE_WEIGHTS
        hi = composite_score(cosine=0.62, confidence=0.1, recency=0.1, w=w)
        lo = composite_score(cosine=0.55, confidence=1.0, recency=1.0, w=w)
        self.assertGreater(hi, lo)


class StabilityTests(unittest.TestCase):
    def test_low_plasticity_keeps_full_confidence(self) -> None:
        # A sticky (plasticity 0) belief contributes its confidence outright.
        self.assertAlmostEqual(stability(0.8, 0.0), 0.8)

    def test_high_plasticity_halves_contribution(self) -> None:
        # A fully fluid (plasticity 1) concept contributes half its confidence.
        self.assertAlmostEqual(stability(0.8, 1.0), 0.4)
        self.assertAlmostEqual(stability(1.0, 0.5), 0.75)

    def test_clamps_inputs(self) -> None:
        self.assertAlmostEqual(stability(2.0, 0.0), 1.0)
        self.assertAlmostEqual(stability(-1.0, 0.0), 0.0)


class HabituationFactorTests(unittest.TestCase):
    def test_never_surfaced_is_neutral(self) -> None:
        self.assertEqual(habituation_factor(None, window=4, floor=0.35), 1.0)

    def test_zero_window_disables(self) -> None:
        self.assertEqual(habituation_factor(1, window=0, floor=0.35), 1.0)

    def test_same_turn_not_penalized(self) -> None:
        # turns_since <= 0 means a second look on the same turn -> no penalty.
        self.assertEqual(habituation_factor(0, window=4, floor=0.35), 1.0)

    def test_strongest_at_previous_turn(self) -> None:
        # Surfaced last turn (ts == 1) is the floor, the strongest suppression.
        self.assertAlmostEqual(
            habituation_factor(1, window=4, floor=0.35), 0.35
        )

    def test_recovers_across_window(self) -> None:
        f2 = habituation_factor(2, window=4, floor=0.35)
        f3 = habituation_factor(3, window=4, floor=0.35)
        self.assertLess(0.35, f2)
        self.assertLess(f2, f3)
        self.assertLess(f3, 1.0)
        # Fully recovered once turns_since reaches the window.
        self.assertEqual(habituation_factor(4, window=4, floor=0.35), 1.0)
        self.assertEqual(habituation_factor(9, window=4, floor=0.35), 1.0)


class SurfaceScoreTests(unittest.TestCase):
    def test_default_weights_equal_cosine(self) -> None:
        for cos in (0.0, 0.3, 0.77, 1.0):
            self.assertAlmostEqual(
                surface_score(cosine=cos, confidence=0.9, recency=0.5,
                              stability=0.5, w=DEFAULT_SURFACE_WEIGHTS),
                cos,
            )

    def test_habituation_multiplies_final_score(self) -> None:
        self.assertAlmostEqual(
            surface_score(cosine=0.8, confidence=0.0, habituation=0.5),
            0.4,
        )

    def test_standing_is_sum_normalized_not_additive(self) -> None:
        w = SurfaceWeights(context=0.9, standing=0.1)
        self.assertAlmostEqual(
            surface_score(
                cosine=1.0, confidence=0.0, standing=1.0, w=w
            ),
            1.0,
        )
        self.assertAlmostEqual(
            surface_score(
                cosine=1.0, confidence=0.0, standing=0.0, w=w
            ),
            0.9,
        )
        # Omitting standing removes its weight entirely (master-disable no-op).
        self.assertAlmostEqual(
            surface_score(cosine=0.7, confidence=0.0, w=w), 0.7
        )

    def test_habituation_rotates_even_high_standing_concept(self) -> None:
        w = SurfaceWeights(context=0.9, standing=0.1)
        rested_low = surface_score(
            cosine=0.6, confidence=0.0, standing=0.35,
            habituation=1.0, w=w,
        )
        repeated_high = surface_score(
            cosine=0.6, confidence=0.0, standing=1.0,
            habituation=0.35, w=w,
        )
        self.assertGreater(rested_low, repeated_high)

    def test_strong_topic_match_recovers_low_standing_concept(self) -> None:
        w = SurfaceWeights(context=0.9, standing=0.1)
        topical_low = surface_score(
            cosine=0.9, confidence=0.0, standing=0.35, w=w
        )
        weak_high = surface_score(
            cosine=0.2, confidence=0.0, standing=1.0, w=w
        )
        self.assertGreater(topical_low, weak_high)

    def test_activation_is_additive_boost(self) -> None:
        # activation is applied on top of the normalized base, so a primed
        # concept can rise above its raw cosine.
        w = SurfaceWeights(context=1.0, activation=0.5)
        self.assertAlmostEqual(
            surface_score(cosine=0.4, confidence=0.0, activation=1.0, w=w),
            0.9,
        )

    def test_stability_enters_the_blend(self) -> None:
        w = SurfaceWeights(context=0.5, stability=0.5)
        self.assertAlmostEqual(
            surface_score(cosine=1.0, confidence=0.0, stability=0.0, w=w), 0.5
        )
        self.assertAlmostEqual(
            surface_score(cosine=1.0, confidence=0.0, stability=1.0, w=w), 1.0
        )

    def test_result_clamped_to_unit_range(self) -> None:
        w = SurfaceWeights(context=1.0, activation=1.0)
        self.assertEqual(
            surface_score(cosine=1.0, confidence=0.0, activation=1.0, w=w), 1.0
        )


class SalienceTests(unittest.TestCase):
    def test_no_events_is_zero(self) -> None:
        self.assertEqual(event_charge([], _NOW, halflife_days=14.0), 0.0)

    def test_contradiction_charges_hardest(self) -> None:
        fresh = _NOW.isoformat()
        contra = event_charge(
            [("contradicted", fresh)], _NOW, halflife_days=14.0
        )
        shift = event_charge(
            [("plasticity_shift", fresh)], _NOW, halflife_days=14.0
        )
        self.assertAlmostEqual(contra, 1.0)
        self.assertGreaterEqual(contra, shift)
        self.assertGreater(shift, 0.0)

    def test_charge_takes_strongest_and_decays(self) -> None:
        fresh = _NOW.isoformat()
        old = (_NOW - timedelta(days=14.0)).isoformat()
        # A fresh promotion + an old contradiction: the decayed contradiction
        # (1.0 * 0.5) still beats the fresh promotion (0.4 * 1.0).
        mixed = event_charge(
            [("promoted", fresh), ("contradicted", old)],
            _NOW, halflife_days=14.0,
        )
        self.assertAlmostEqual(mixed, 0.5, places=3)

    def test_unknown_event_type_ignored(self) -> None:
        self.assertEqual(
            event_charge(
                [("discovered", _NOW.isoformat())], _NOW, halflife_days=14.0
            ),
            0.0,
        )

    def test_salience_soft_or(self) -> None:
        self.assertAlmostEqual(salience(change=0.0, affect=0.0), 0.0)
        self.assertAlmostEqual(salience(change=0.5, affect=0.0), 0.5)
        # Soft-OR: 0.5 and 0.5 -> 0.75, never exceeding 1.0.
        self.assertAlmostEqual(salience(change=0.5, affect=0.5), 0.75)
        self.assertAlmostEqual(salience(change=1.0, affect=0.9), 1.0)

    def test_detail_names_the_driving_event(self) -> None:
        fresh = _NOW.isoformat()
        old = (_NOW - timedelta(days=14.0)).isoformat()
        charge, driver = event_charge_detail(
            [("promoted", fresh), ("contradicted", old)],
            _NOW, halflife_days=14.0,
        )
        # Same winner as ``event_charge``, but now we know who it was.
        self.assertAlmostEqual(charge, 0.5, places=3)
        self.assertEqual(driver, "contradicted")

    def test_detail_has_no_driver_without_charge(self) -> None:
        self.assertEqual(
            event_charge_detail(
                [("discovered", _NOW.isoformat())], _NOW, halflife_days=14.0
            ),
            (0.0, None),
        )


class SurfaceReasonTests(unittest.TestCase):
    def test_above_neutral_standing_can_be_debug_reason(self) -> None:
        reason = surface_reason(
            lane="flex", cosine=0.05, standing=1.0,
            w=SurfaceWeights(context=0.1, standing=0.9),
        )
        self.assertEqual(reason, "earned_standing")
        self.assertIn(reason, SURFACE_REASON_LABELS)

    """L35: name the signal that won a concept its place in the prompt."""

    # Every signal weighted, so no single term wins by default.
    _W = SurfaceWeights(
        context=1.0, confidence=1.0, recency=1.0, stability=1.0,
        salience=1.0, activation=1.0,
    )

    def test_lanes_that_answer_themselves(self) -> None:
        # Core is pinned on confidence before any scoring runs; the
        # activation lane is reached only by concepts with no cosine to the
        # turn at all. Neither ran a contest, so the signals can't override.
        self.assertEqual(
            surface_reason(lane="core", cosine=1.0, w=self._W), "core_belief"
        )
        self.assertEqual(
            surface_reason(
                lane="activation", stability=1.0, activation=0.1, w=self._W
            ),
            "association",
        )

    def test_neutral_recency_never_wins(self) -> None:
        """``recency_boost`` returns 1.0 -- its maximum -- for a concept that
        was never reinforced. That's a "don't penalise" default, not
        freshness, so it must not be reported as the reason."""
        kw = dict(lane="flex", cosine=0.3, recency=1.0, w=self._W)
        self.assertEqual(surface_reason(**kw), "recently_reinforced")
        self.assertEqual(
            surface_reason(**kw, recency_known=False), "topic_match"
        )

    def test_dominant_cosine_is_a_topic_match(self) -> None:
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=0.9, confidence=0.2, recency=0.1,
                stability=0.1, salience=0.0, w=self._W,
            ),
            "topic_match",
        )

    def test_dominant_activation_is_an_association(self) -> None:
        # A flex-lane concept (it had its own cosine) that was nonetheless
        # carried by priming still reports the association.
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=0.05, confidence=0.1,
                activation=0.9, w=self._W,
            ),
            "association",
        )

    def test_salience_win_names_the_event_behind_it(self) -> None:
        """The same charge means different things; the reason has to say
        which, or it's no more legible than the number was."""
        for event, expected in (
            ("contradicted", "unresolved_contradiction"),
            ("revived", "recently_revived"),
            ("plasticity_shift", "loosening_boundary"),
            ("promoted", "newly_promoted"),
        ):
            with self.subTest(event=event):
                self.assertEqual(
                    surface_reason(
                        lane="flex", cosine=0.1, salience=0.9,
                        change_event=event, w=self._W,
                    ),
                    expected,
                )

    def test_salience_win_without_a_known_driver_degrades(self) -> None:
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=0.1, salience=0.9, change_event=None,
                w=self._W,
            ),
            "recent_change",
        )

    def test_weight_decides_the_winner_not_the_raw_value(self) -> None:
        """A perfect cosine against a zero context weight won nothing --
        the reason has to reflect what the scorer actually used."""
        w = SurfaceWeights(context=0.0, confidence=0.0, recency=0.0,
                           stability=1.0, salience=0.0)
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=1.0, stability=0.3, w=w,
            ),
            "settled_belief",
        )

    def test_recency_and_confidence_wins(self) -> None:
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=0.1, recency=0.95, w=self._W,
            ),
            "recently_reinforced",
        )
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=0.1, confidence=0.95, w=self._W,
            ),
            "high_confidence",
        )

    def test_default_weights_are_context_only(self) -> None:
        # DEFAULT_SURFACE_WEIGHTS is context-only, so a kind that never
        # opted into the blend can only ever be a topic match.
        self.assertEqual(
            surface_reason(
                lane="flex", cosine=0.4, confidence=0.9, recency=0.9,
                stability=0.9, salience=0.9, w=DEFAULT_SURFACE_WEIGHTS,
            ),
            "topic_match",
        )

    def test_all_signals_zero_is_a_topic_match(self) -> None:
        self.assertEqual(surface_reason(lane="flex", w=self._W), "topic_match")

    def test_every_reason_has_a_human_label(self) -> None:
        reasons = {
            surface_reason(lane="core"),
            surface_reason(lane="activation"),
            surface_reason(lane="flex", cosine=0.9, w=self._W),
            surface_reason(lane="flex", confidence=0.9, w=self._W),
            surface_reason(lane="flex", recency=0.9, w=self._W),
            surface_reason(lane="flex", stability=0.9, w=self._W),
            surface_reason(lane="flex", salience=0.9, w=self._W),
            surface_reason(
                lane="flex", standing=1.0,
                w=SurfaceWeights(context=0.1, standing=0.9),
            ),
        }
        for event in ("contradicted", "revived", "plasticity_shift", "promoted"):
            reasons.add(
                surface_reason(
                    lane="flex", salience=0.9, change_event=event, w=self._W
                )
            )
        self.assertEqual(len(reasons), len(SURFACE_REASON_LABELS))
        for reason in reasons:
            self.assertIn(reason, SURFACE_REASON_LABELS)


class HabituationStateTests(unittest.TestCase):
    def test_load_empty_or_junk_is_empty(self) -> None:
        self.assertEqual(load_habituation(lambda _k: None), {})
        self.assertEqual(load_habituation(lambda _k: ""), {})
        self.assertEqual(load_habituation(lambda _k: "not-json"), {})
        self.assertEqual(load_habituation(lambda _k: "[1,2,3]"), {})

    def test_save_load_round_trip(self) -> None:
        box: dict[str, str] = {}
        save_habituation(box.__setitem__, {7: 3, 9: 5})
        from app.core.concepts.concept_surfacing import HABITUATION_KV_KEY
        self.assertIn(HABITUATION_KV_KEY, box)
        loaded = load_habituation(box.get)
        self.assertEqual(loaded, {7: 3, 9: 5})

    def test_save_prunes_to_cap_keeping_newest(self) -> None:
        box: dict[str, str] = {}
        state = {cid: cid for cid in range(10)}  # turn == id
        save_habituation(box.__setitem__, state, cap=3)
        loaded = load_habituation(box.get)
        self.assertEqual(set(loaded), {7, 8, 9})

    def test_turns_since_surfaced(self) -> None:
        state = {7: 10}
        self.assertIsNone(turns_since_surfaced(state, 99, 12))
        self.assertEqual(turns_since_surfaced(state, 7, 12), 2)
        # Same turn / clock skew never yields a negative penalty.
        self.assertEqual(turns_since_surfaced(state, 7, 10), 0)
        self.assertEqual(turns_since_surfaced(state, 7, 8), 0)


class KindSurfaceWeightsTests(unittest.TestCase):
    """L18e: the three kinds that were still context-only (== raw cosine) now
    carry a tuned, non-default blend."""

    def test_narrative_aspiration_ritual_are_tuned(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        for name in ("narrative", "aspiration", "ritual"):
            kind = get_kind(name)
            self.assertIsNotNone(kind, name)
            self.assertNotEqual(
                kind.surface_weights, DEFAULT_SURFACE_WEIGHTS, name
            )

    def test_narrative_leans_on_stability(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        self.assertGreater(get_kind("narrative").surface_weights.stability, 0.0)

    def test_aspiration_weights_recency(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        self.assertGreater(get_kind("aspiration").surface_weights.recency, 0.0)


_ELLIPSIS = "\u2026"


def _concept(
    cid: int, label: str, *, rationale: str = "", subject: str = "user",
    kind: str = "trait", confidence: float = 0.9,
) -> SimpleNamespace:
    return SimpleNamespace(
        concept_id=cid, label=label, rationale=rationale, subject=subject,
        kind=kind, confidence=confidence, plasticity=0.2,
        last_reinforced_at=None,
    )


class _RenderHarness(InnerLifePart1Mixin):
    """Minimal stand-in exercising ``_render_relevant_concepts`` in isolation:
    real hedging / grounding / header statics, stubbed evidence + settings."""

    def __init__(self, *, core_rationale: bool = True, cap: int = 120) -> None:
        self._memory_settings = SimpleNamespace(
            concept_surfacing_core_rationale_enabled=core_rationale,
            concept_surfacing_rationale_max_chars=cap,
            concept_surfacing_state_cap=300,
        )

    @property
    def user_display_name(self) -> str:
        return "Alex"

    def _concept_supporting_labels(self, concept_id: int) -> list[str]:
        return []


class CoreRationaleClauseTests(unittest.TestCase):
    _MARK = "the sense of it traces back to"

    def test_pinned_concept_renders_rationale(self) -> None:
        h = _RenderHarness()
        c = _concept(
            7, "values careful reasoning",
            rationale="they slow down before deciding",
        )
        text, trace = h._render_relevant_concepts([c], pinned_ids={7})
        self.assertIn(
            f"{self._MARK} they slow down before deciding", text
        )
        self.assertTrue(trace["surfaced"][0]["rationale_surfaced"])

    def test_non_pinned_concept_omits_rationale(self) -> None:
        h = _RenderHarness()
        c = _concept(7, "values careful reasoning", rationale="a reason")
        text, trace = h._render_relevant_concepts([c], pinned_ids=set())
        self.assertNotIn(self._MARK, text)
        self.assertFalse(trace["surfaced"][0]["rationale_surfaced"])

    def test_disabled_flag_omits_rationale(self) -> None:
        h = _RenderHarness(core_rationale=False)
        c = _concept(7, "a trait", rationale="a reason")
        text, trace = h._render_relevant_concepts([c], pinned_ids={7})
        self.assertNotIn(self._MARK, text)
        self.assertFalse(trace["surfaced"][0]["rationale_surfaced"])

    def test_blank_rationale_no_clause(self) -> None:
        h = _RenderHarness()
        c = _concept(7, "a trait", rationale="   ")
        text, trace = h._render_relevant_concepts([c], pinned_ids={7})
        self.assertNotIn(self._MARK, text)
        self.assertFalse(trace["surfaced"][0]["rationale_surfaced"])

    def test_rationale_respects_char_cap(self) -> None:
        h = _RenderHarness(cap=20)
        c = _concept(
            7, "a trait",
            rationale=(
                "because they consistently choose the harder honest "
                "path over comfort"
            ),
        )
        text, trace = h._render_relevant_concepts([c], pinned_ids={7})
        tail = text.split(f"{self._MARK} ", 1)[1]
        self.assertTrue(tail.endswith(_ELLIPSIS))
        self.assertLessEqual(len(tail.rstrip(_ELLIPSIS).strip()), 20)
        self.assertTrue(trace["surfaced"][0]["rationale_surfaced"])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the J11 affection-style pure module.

Pure math + serde only — no controller, no I/O. Mirrors the
``tests/test_vulnerability_budget.py`` shape.
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from app.core.relationship.affection_style import (
    AFFECTION_KINDS,
    AffectionStyleState,
    REACTION_TO_KIND,
    apply_observation,
    apply_reaction_confirmation,
    bias_multiplier,
    classify_turn_affection,
    decay_toward_uniform,
    deserialize,
    engagement_to_signal,
    serialize,
    top_kind,
    uniform_state,
)


_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
_UNIFORM = 1.0 / len(AFFECTION_KINDS)


def _sum(state: AffectionStyleState) -> float:
    return sum(state.weights.values())


class TaxonomyTests(unittest.TestCase):
    def test_five_kinds(self) -> None:
        self.assertEqual(
            AFFECTION_KINDS,
            ("touch", "teasing", "appreciation", "words", "space"),
        )

    def test_uniform_state_sums_to_one(self) -> None:
        s = uniform_state(_NOW)
        self.assertAlmostEqual(_sum(s), 1.0, places=9)
        for k in AFFECTION_KINDS:
            self.assertAlmostEqual(s.weight_of(k), _UNIFORM, places=9)

    def test_reaction_map_targets_valid_kinds_and_skips_surprise(self) -> None:
        for kind in REACTION_TO_KIND.values():
            self.assertIn(kind, AFFECTION_KINDS)
        self.assertNotIn("surprise", REACTION_TO_KIND)
        # No reaction confirms "space" — you can't react to absence.
        self.assertNotIn("space", REACTION_TO_KIND.values())


class ClassifyTests(unittest.TestCase):
    def test_touch_tag_detected(self) -> None:
        out = classify_turn_affection("here, [[touch:hug]] come here", None)
        self.assertIn("touch", out)

    def test_tease_reaction(self) -> None:
        self.assertEqual(
            classify_turn_affection("oh you", "playful"), ["teasing"]
        )

    def test_warm_reaction_is_words(self) -> None:
        self.assertEqual(
            classify_turn_affection("i'm proud of you", "warm"), ["words"]
        )

    def test_appreciation_flag(self) -> None:
        out = classify_turn_affection(
            "thanks for earlier", None, appreciation_fired=True
        )
        self.assertIn("appreciation", out)

    def test_multiple_kinds_in_taxonomy_order(self) -> None:
        out = classify_turn_affection(
            "[[touch:hug]] you did great", "warm"
        )
        self.assertEqual(out, ["touch", "words"])

    def test_short_reply_reads_as_space(self) -> None:
        self.assertEqual(classify_turn_affection("mm, ok.", None), ["space"])

    def test_long_plain_reply_reads_as_words(self) -> None:
        long_text = "x" * 200
        self.assertEqual(classify_turn_affection(long_text, None), ["words"])

    def test_empty_reply_yields_nothing(self) -> None:
        self.assertEqual(classify_turn_affection("", None), [])


class EngagementSignalTests(unittest.TestCase):
    def test_bands(self) -> None:
        self.assertEqual(engagement_to_signal("engaged"), 1.0)
        self.assertEqual(engagement_to_signal("abandoned"), -1.0)
        self.assertAlmostEqual(engagement_to_signal("disengaged"), -0.6)
        self.assertEqual(engagement_to_signal("neutral"), 0.0)
        self.assertEqual(engagement_to_signal(None), 0.0)

    def test_length_z_refines_within_band_only(self) -> None:
        # length_z lifts a neutral band slightly but never flips sign.
        self.assertGreater(engagement_to_signal("neutral", 1.0), 0.0)
        self.assertLess(engagement_to_signal("neutral", -1.0), 0.0)
        # disengaged ignores length_z (band is decisive).
        self.assertAlmostEqual(engagement_to_signal("disengaged", 5.0), -0.6)

    def test_clamped(self) -> None:
        self.assertEqual(engagement_to_signal("engaged", 100.0), 1.0)


class ObservationTests(unittest.TestCase):
    def test_positive_signal_lifts_kind_share(self) -> None:
        s = uniform_state(_NOW)
        out = apply_observation(
            s, ["touch"], 1.0, _NOW, learning_rate=0.1, floor=0.05,
        )
        self.assertGreater(out.weight_of("touch"), _UNIFORM)
        self.assertAlmostEqual(_sum(out), 1.0, places=9)

    def test_negative_signal_lowers_kind_share(self) -> None:
        s = uniform_state(_NOW)
        out = apply_observation(
            s, ["teasing"], -1.0, _NOW, learning_rate=0.1, floor=0.05,
        )
        self.assertLess(out.weight_of("teasing"), _UNIFORM)
        self.assertAlmostEqual(_sum(out), 1.0, places=9)

    def test_floor_respected_under_repeated_negatives(self) -> None:
        s = uniform_state(_NOW)
        for _ in range(50):
            s = apply_observation(
                s, ["space"], -1.0, _NOW, learning_rate=0.5, floor=0.05,
            )
        self.assertGreaterEqual(s.weight_of("space"), 0.05 - 1e-9)
        self.assertAlmostEqual(_sum(s), 1.0, places=9)

    def test_split_across_multiple_kinds(self) -> None:
        s = uniform_state(_NOW)
        both = apply_observation(
            s, ["touch", "words"], 1.0, _NOW, learning_rate=0.1, floor=0.05,
        )
        single = apply_observation(
            s, ["touch"], 1.0, _NOW, learning_rate=0.1, floor=0.05,
        )
        # Splitting the signal means touch moves less when shared.
        self.assertLess(
            both.weight_of("touch"), single.weight_of("touch"),
        )

    def test_zero_signal_is_noop_but_advances_timestamp(self) -> None:
        s = uniform_state(_NOW)
        later = _NOW + timedelta(hours=1)
        out = apply_observation(
            s, ["touch"], 0.0, later, learning_rate=0.1, floor=0.05,
        )
        self.assertEqual(out.weights, s.weights)
        self.assertEqual(out.updated_at, later.isoformat())

    def test_empty_kinds_noop(self) -> None:
        s = uniform_state(_NOW)
        out = apply_observation(
            s, [], 1.0, _NOW, learning_rate=0.1, floor=0.05,
        )
        self.assertEqual(out.weights, s.weights)


class ReactionConfirmationTests(unittest.TestCase):
    def test_hug_confirms_touch(self) -> None:
        s = uniform_state(_NOW)
        out = apply_reaction_confirmation(
            s, "hug", _NOW, reaction_weight=0.1, floor=0.05,
        )
        self.assertGreater(out.weight_of("touch"), _UNIFORM)

    def test_surprise_is_noop(self) -> None:
        s = uniform_state(_NOW)
        out = apply_reaction_confirmation(
            s, "surprise", _NOW, reaction_weight=0.1, floor=0.05,
        )
        self.assertEqual(out.weights, s.weights)

    def test_grateful_confirms_appreciation(self) -> None:
        s = uniform_state(_NOW)
        out = apply_reaction_confirmation(
            s, "grateful", _NOW, reaction_weight=0.1, floor=0.05,
        )
        self.assertGreater(out.weight_of("appreciation"), _UNIFORM)


class DecayTests(unittest.TestCase):
    def test_decays_halfway_over_one_half_life(self) -> None:
        s = apply_observation(
            uniform_state(_NOW), ["touch"], 1.0, _NOW,
            learning_rate=0.5, floor=0.05,
        )
        gap_before = s.weight_of("touch") - _UNIFORM
        self.assertGreater(gap_before, 0.0)
        later = _NOW + timedelta(days=30)
        out = decay_toward_uniform(
            s, later, half_life_days=30.0, floor=0.05,
        )
        gap_after = out.weight_of("touch") - _UNIFORM
        # Halved (renormalisation perturbs it slightly, so use a loose
        # tolerance but assert it moved toward uniform).
        self.assertLess(gap_after, gap_before)
        self.assertAlmostEqual(gap_after, gap_before * 0.5, delta=0.02)

    def test_no_elapsed_is_noop(self) -> None:
        s = apply_observation(
            uniform_state(_NOW), ["touch"], 1.0, _NOW,
            learning_rate=0.5, floor=0.05,
        )
        out = decay_toward_uniform(s, _NOW, half_life_days=30.0, floor=0.05)
        for k in AFFECTION_KINDS:
            self.assertAlmostEqual(out.weight_of(k), s.weight_of(k), places=9)


class BiasMultiplierTests(unittest.TestCase):
    def test_uniform_is_one(self) -> None:
        s = uniform_state(_NOW)
        self.assertAlmostEqual(
            bias_multiplier(s, "touch", strength=0.5), 1.0, places=6,
        )

    def test_above_uniform_is_greater_than_one(self) -> None:
        s = apply_observation(
            uniform_state(_NOW), ["touch"], 1.0, _NOW,
            learning_rate=0.5, floor=0.05,
        )
        self.assertGreater(bias_multiplier(s, "touch", strength=0.5), 1.0)

    def test_clamped_to_band(self) -> None:
        s = AffectionStyleState(
            weights={"touch": 0.9, "teasing": 0.025, "appreciation": 0.025,
                     "words": 0.025, "space": 0.025},
            updated_at=_NOW.isoformat(),
        )
        hi = bias_multiplier(s, "touch", strength=2.0, floor=0.6, ceil=1.5)
        lo = bias_multiplier(s, "space", strength=2.0, floor=0.6, ceil=1.5)
        self.assertLessEqual(hi, 1.5 + 1e-9)
        self.assertGreaterEqual(lo, 0.6 - 1e-9)

    def test_strength_zero_is_flat(self) -> None:
        s = apply_observation(
            uniform_state(_NOW), ["touch"], 1.0, _NOW,
            learning_rate=0.5, floor=0.05,
        )
        self.assertEqual(bias_multiplier(s, "touch", strength=0.0), 1.0)


class SerdeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        s = apply_observation(
            uniform_state(_NOW), ["touch"], 1.0, _NOW,
            learning_rate=0.3, floor=0.05,
        )
        out = deserialize(serialize(s))
        for k in AFFECTION_KINDS:
            self.assertAlmostEqual(out.weight_of(k), s.weight_of(k), places=6)

    def test_corrupt_returns_uniform(self) -> None:
        for bad in (None, "", "not json", "[]", "{}", '{"weights": 3}'):
            out = deserialize(bad)
            self.assertAlmostEqual(_sum(out), 1.0, places=9)

    def test_missing_kind_backfilled(self) -> None:
        out = deserialize('{"weights": {"touch": 0.6}, "updated_at": "x"}')
        self.assertAlmostEqual(_sum(out), 1.0, places=9)
        self.assertGreater(out.weight_of("touch"), out.weight_of("space"))


class TopKindTests(unittest.TestCase):
    def test_top_kind(self) -> None:
        s = apply_observation(
            uniform_state(_NOW), ["teasing"], 1.0, _NOW,
            learning_rate=0.5, floor=0.05,
        )
        self.assertEqual(top_kind(s), "teasing")


if __name__ == "__main__":
    unittest.main()

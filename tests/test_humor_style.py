"""Pure-module tests for K74 humor-style calibration."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.relationship import humor_style as hs


_NOW = datetime(2026, 6, 30, tzinfo=timezone.utc)


class ClassifyTurnHumorTests(unittest.TestCase):
    def test_self_deprecating(self) -> None:
        self.assertEqual(
            hs.classify_turn_humor("honestly I'm hopeless at this", None),
            ["self_deprecating"],
        )

    def test_playful_roast(self) -> None:
        self.assertEqual(
            hs.classify_turn_humor("sure you did, you dork", None),
            ["playful_roast"],
        )

    def test_absurdist(self) -> None:
        self.assertEqual(
            hs.classify_turn_humor("plot twist: the cat did it", None),
            ["absurdist"],
        )

    def test_pun(self) -> None:
        self.assertEqual(
            hs.classify_turn_humor("that's punny, no pun intended", None),
            ["pun"],
        )

    def test_deadpan_fallback_from_reaction(self) -> None:
        # Humour reaction but no overt marker → deadpan (dry delivery).
        self.assertEqual(
            hs.classify_turn_humor("oh, fascinating.", "smug"),
            ["deadpan"],
        )

    def test_no_humor_returns_empty(self) -> None:
        self.assertEqual(
            hs.classify_turn_humor("Sure, here's the plan.", "warm"), []
        )

    def test_multiple_markers(self) -> None:
        kinds = hs.classify_turn_humor(
            "I'm hopeless, plot twist I forgot again", None
        )
        self.assertIn("self_deprecating", kinds)
        self.assertIn("absurdist", kinds)


class SignalTests(unittest.TestCase):
    def test_bands(self) -> None:
        self.assertEqual(hs.engagement_to_signal("engaged"), 1.0)
        self.assertEqual(hs.engagement_to_signal("abandoned"), -1.0)
        self.assertEqual(hs.engagement_to_signal("disengaged"), -0.6)
        self.assertEqual(hs.engagement_to_signal("neutral"), 0.0)
        self.assertEqual(hs.engagement_to_signal(None), 0.0)


class MutationTests(unittest.TestCase):
    def test_observation_lifts_kind(self) -> None:
        st = hs.uniform_state(_NOW)
        st2 = hs.apply_observation(
            st, ["deadpan"], 1.0, _NOW, learning_rate=0.1, floor=0.05,
        )
        self.assertGreater(st2.weight_of("deadpan"), st.weight_of("deadpan"))
        self.assertAlmostEqual(sum(st2.weights.values()), 1.0, places=5)

    def test_observation_floor_never_zeroes(self) -> None:
        st = hs.uniform_state(_NOW)
        for _ in range(50):
            st = hs.apply_observation(
                st, ["pun"], -1.0, _NOW, learning_rate=0.5, floor=0.05,
            )
        for k in hs.HUMOR_KINDS:
            self.assertGreater(st.weight_of(k), 0.0)

    def test_reaction_confirmation_boosts(self) -> None:
        st = hs.uniform_state(_NOW)
        st2 = hs.apply_reaction_confirmation(
            st, ["playful_roast"], _NOW, reaction_weight=0.1, floor=0.05,
        )
        self.assertGreater(
            st2.weight_of("playful_roast"), st.weight_of("playful_roast")
        )

    def test_reaction_confirmation_empty_noop(self) -> None:
        st = hs.uniform_state(_NOW)
        st2 = hs.apply_reaction_confirmation(
            st, [], _NOW, reaction_weight=0.1, floor=0.05,
        )
        self.assertEqual(st2.weights, st.weights)


class DecayTests(unittest.TestCase):
    def test_decay_pulls_toward_uniform(self) -> None:
        st = hs.HumorStyleState(
            weights={
                "pun": 0.6, "deadpan": 0.1, "absurdist": 0.1,
                "self_deprecating": 0.1, "playful_roast": 0.1,
            },
            updated_at=_NOW.isoformat(),
        )
        later = _NOW + timedelta(days=30)
        st2 = hs.decay_toward_uniform(
            st, later, half_life_days=30.0, floor=0.05,
        )
        # pun should be closer to 0.2 after one half-life.
        self.assertLess(st2.weight_of("pun"), 0.6)
        self.assertGreater(st2.weight_of("pun"), 0.2)


class RegisterHintTests(unittest.TestCase):
    def test_silent_near_uniform(self) -> None:
        self.assertEqual(hs.register_hint(hs.uniform_state(_NOW), "Jacob"), "")

    def test_fires_above_threshold(self) -> None:
        st = hs.HumorStyleState(
            weights={
                "deadpan": 0.5, "pun": 0.125, "absurdist": 0.125,
                "self_deprecating": 0.125, "playful_roast": 0.125,
            },
            updated_at=_NOW.isoformat(),
        )
        hint = hs.register_hint(st, "Jacob", min_rel=1.25)
        self.assertIn("Jacob", hint)
        self.assertIn("deadpan", hint)


class SerdeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        st = hs.apply_observation(
            hs.uniform_state(_NOW), ["absurdist"], 1.0, _NOW,
            learning_rate=0.2, floor=0.05,
        )
        back = hs.deserialize(hs.serialize(st))
        for k in hs.HUMOR_KINDS:
            self.assertAlmostEqual(
                back.weight_of(k), st.weight_of(k), places=5
            )

    def test_garbage_to_uniform(self) -> None:
        self.assertEqual(
            hs.deserialize("not json").weights, hs.uniform_state().weights
        )
        self.assertEqual(
            hs.deserialize(None).weights, hs.uniform_state().weights
        )


if __name__ == "__main__":
    unittest.main()

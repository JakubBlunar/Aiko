"""Pure-module tests for K68 embodied vitality (app/core/affect/vitality.py).

Covers the math (band, expressiveness multiplier, recovery relaxation,
turn cost, the interest boost / liven-up, apply_turn) and the kv
serialise / deserialise round-trip. No I/O, no controller -- runs in
milliseconds.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.affect import vitality as v


class BandTests(unittest.TestCase):
    def test_low_normal_high(self) -> None:
        self.assertEqual(v.band(0.10), "low")
        self.assertEqual(v.band(0.30), "low")  # boundary inclusive
        self.assertEqual(v.band(0.50), "normal")
        self.assertEqual(v.band(0.70), "high")  # boundary inclusive
        self.assertEqual(v.band(0.95), "high")

    def test_custom_thresholds(self) -> None:
        self.assertEqual(v.band(0.40, low_threshold=0.45), "low")
        self.assertEqual(v.band(0.80, high_threshold=0.90), "normal")

    def test_clamps_out_of_range(self) -> None:
        self.assertEqual(v.band(-5.0), "low")
        self.assertEqual(v.band(5.0), "high")


class ExpressivenessMultiplierTests(unittest.TestCase):
    def test_endpoints(self) -> None:
        self.assertAlmostEqual(v.expressiveness_multiplier(0.0), 0.7, places=4)
        self.assertAlmostEqual(v.expressiveness_multiplier(1.0), 1.2, places=4)

    def test_midpoint_is_between(self) -> None:
        mid = v.expressiveness_multiplier(0.5)
        self.assertTrue(0.7 < mid < 1.2)
        self.assertAlmostEqual(mid, 0.95, places=4)

    def test_low_energy_droops_below_one(self) -> None:
        self.assertLess(v.expressiveness_multiplier(0.2), 1.0)

    def test_inverted_floor_ceil_swapped(self) -> None:
        # A hostile config (floor > ceil) is swapped, not crashed.
        a = v.expressiveness_multiplier(0.0, floor=1.2, ceil=0.7)
        self.assertAlmostEqual(a, 0.7, places=4)


class RecoverTowardTests(unittest.TestCase):
    def test_no_elapsed_no_change(self) -> None:
        self.assertEqual(v.recover_toward(0.9, 0.2, 0.0), 0.9)

    def test_half_life_halves_gap(self) -> None:
        # energy 0.9, baseline 0.1, after one half-life the gap halves.
        out = v.recover_toward(0.9, 0.1, 2.0, half_life_hours=2.0)
        self.assertAlmostEqual(out, 0.5, places=4)

    def test_rises_toward_higher_baseline(self) -> None:
        out = v.recover_toward(0.2, 0.8, 2.0, half_life_hours=2.0)
        self.assertAlmostEqual(out, 0.5, places=4)

    def test_long_idle_approaches_baseline(self) -> None:
        out = v.recover_toward(0.95, 0.15, 24.0, half_life_hours=2.0)
        self.assertAlmostEqual(out, 0.15, places=2)

    def test_within_session_negligible(self) -> None:
        # A few seconds of elapsed time barely moves energy -> boosts
        # accumulate across a live conversation.
        out = v.recover_toward(0.9, 0.2, 5.0 / 3600.0, half_life_hours=2.0)
        self.assertAlmostEqual(out, 0.9, places=3)


class TurnCostTests(unittest.TestCase):
    def test_short_reply_cheap(self) -> None:
        cost = v.compute_turn_cost(reply_chars=50, emotion_intensity=0.0)
        self.assertLess(cost, 0.01)

    def test_long_reply_costs_more(self) -> None:
        short = v.compute_turn_cost(reply_chars=100)
        long = v.compute_turn_cost(reply_chars=1200)
        self.assertGreater(long, short)

    def test_emotion_adds_cost(self) -> None:
        flat = v.compute_turn_cost(reply_chars=100, emotion_intensity=0.0)
        heavy = v.compute_turn_cost(reply_chars=100, emotion_intensity=0.9)
        self.assertGreater(heavy, flat)

    def test_capped(self) -> None:
        cost = v.compute_turn_cost(
            reply_chars=100000, emotion_intensity=1.0, max_cost=0.12,
        )
        self.assertLessEqual(cost, 0.12)


class InterestBoostTests(unittest.TestCase):
    def test_silent_when_nothing_interesting(self) -> None:
        boost = v.compute_interest_boost(
            engagement_label="neutral", arousal=0.4, novelty_band=None,
        )
        self.assertEqual(boost, 0.0)

    def test_engaged_user_boosts(self) -> None:
        boost = v.compute_interest_boost(
            engagement_label="engaged", arousal=0.4, novelty_band=None,
        )
        self.assertAlmostEqual(boost, 0.05, places=4)

    def test_disengaged_no_boost(self) -> None:
        # A dead chat doesn't perk her up even with the engaged path off.
        boost = v.compute_interest_boost(
            engagement_label="disengaged", arousal=0.4, novelty_band=None,
        )
        self.assertEqual(boost, 0.0)

    def test_high_arousal_boosts(self) -> None:
        boost = v.compute_interest_boost(
            engagement_label=None, arousal=0.9, novelty_band=None,
        )
        # (0.9 - 0.55) * 0.22 ~= 0.077
        self.assertGreater(boost, 0.0)

    def test_arousal_below_threshold_no_boost(self) -> None:
        boost = v.compute_interest_boost(
            engagement_label=None, arousal=0.50, novelty_band=None,
        )
        self.assertEqual(boost, 0.0)

    def test_novelty_bands(self) -> None:
        strong = v.compute_interest_boost(
            engagement_label=None, arousal=None, novelty_band="strong_novelty",
        )
        mild = v.compute_interest_boost(
            engagement_label=None, arousal=None, novelty_band="mild_shift",
        )
        self.assertGreater(strong, mild)
        self.assertGreater(mild, 0.0)

    def test_combined_capped(self) -> None:
        boost = v.compute_interest_boost(
            engagement_label="engaged",
            arousal=1.0,
            novelty_band="strong_novelty",
            max_boost=0.15,
        )
        self.assertLessEqual(boost, 0.15)

    def test_the_liven_up_lifts_a_sleepy_aiko(self) -> None:
        # The headline: a sleepy Aiko (low energy) over an engaging,
        # exciting, novel turn climbs meaningfully.
        boost = v.compute_interest_boost(
            engagement_label="engaged",
            arousal=0.85,
            novelty_band="strong_novelty",
        )
        cost = v.compute_turn_cost(reply_chars=300, emotion_intensity=0.0)
        net = boost - cost
        self.assertGreater(net, 0.05)


class ApplyTurnTests(unittest.TestCase):
    def test_net_positive_perks_up(self) -> None:
        self.assertGreater(
            v.apply_turn(0.3, cost=0.02, boost=0.10), 0.3,
        )

    def test_net_negative_drains(self) -> None:
        self.assertLess(v.apply_turn(0.3, cost=0.10, boost=0.0), 0.3)

    def test_clamps(self) -> None:
        self.assertEqual(v.apply_turn(0.98, cost=0.0, boost=0.5), 1.0)
        self.assertEqual(v.apply_turn(0.02, cost=0.5, boost=0.0), 0.0)


class SerdeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        state = v.VitalityState(energy=0.42, last_update_at=now.isoformat())
        out = v.deserialize(v.serialize(state), baseline=0.5, now=now)
        self.assertAlmostEqual(out.energy, 0.42, places=4)
        self.assertEqual(out.last_update_at, now.isoformat())

    def test_missing_seeds_baseline(self) -> None:
        now = datetime.now(timezone.utc)
        out = v.deserialize(None, baseline=0.33, now=now)
        self.assertAlmostEqual(out.energy, 0.33, places=4)

    def test_corrupt_seeds_baseline(self) -> None:
        now = datetime.now(timezone.utc)
        out = v.deserialize("not json", baseline=0.6, now=now)
        self.assertAlmostEqual(out.energy, 0.6, places=4)

    def test_clamps_stored_energy(self) -> None:
        now = datetime.now(timezone.utc)
        out = v.deserialize(
            '{"energy": 5.0, "last_update_at": "x"}', baseline=0.5, now=now,
        )
        self.assertEqual(out.energy, 1.0)


class StepRecoverTests(unittest.TestCase):
    def test_advances_timestamp_and_recovers(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        state = v.VitalityState(energy=0.9, last_update_at=past.isoformat())
        now = datetime.now(timezone.utc)
        out = v.step_recover(state, 0.1, now, half_life_hours=2.0)
        self.assertAlmostEqual(out.energy, 0.5, places=2)
        self.assertEqual(out.last_update_at, now.isoformat())

    def test_future_timestamp_no_recovery(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(hours=5)
        state = v.VitalityState(energy=0.9, last_update_at=future.isoformat())
        now = datetime.now(timezone.utc)
        out = v.step_recover(state, 0.1, now)
        self.assertEqual(out.energy, 0.9)


class CircadianBaselineTests(unittest.TestCase):
    def test_in_range(self) -> None:
        b = v.circadian_baseline()
        self.assertGreaterEqual(b, 0.0)
        self.assertLessEqual(b, 1.0)

    def test_night_lower_than_midday(self) -> None:
        night = v.circadian_baseline(
            datetime(2026, 6, 30, 3, 0, 0).astimezone(),
        )
        midday = v.circadian_baseline(
            datetime(2026, 6, 30, 14, 0, 0).astimezone(),
        )
        self.assertLess(night, midday)

    def test_phase_shift_flip_inverts_day_night(self) -> None:
        # K68 rhythm: a ~12h phase shift flips the curve so noon reads
        # low and the small hours read high (the "nocturnal" day).
        noon = datetime(2026, 6, 30, 12, 0, 0).astimezone()
        small_hours = datetime(2026, 6, 30, 2, 0, 0).astimezone()
        noon_flipped = v.circadian_baseline(noon, phase_shift_hours=-12.0)
        noon_normal = v.circadian_baseline(noon)
        night_flipped = v.circadian_baseline(small_hours, phase_shift_hours=-12.0)
        night_normal = v.circadian_baseline(small_hours)
        # Flipping drags midday down and lifts the small hours up.
        self.assertLess(noon_flipped, noon_normal)
        self.assertGreater(night_flipped, night_normal)
        # And the flipped day's small hours beat its own midday.
        self.assertGreater(night_flipped, noon_flipped)

    def test_energy_scale_flattens(self) -> None:
        midday = datetime(2026, 6, 30, 14, 0, 0).astimezone()
        full = v.circadian_baseline(midday)
        half = v.circadian_baseline(midday, energy_scale=0.5)
        self.assertLess(half, full)

    def test_floor_boost_lifts_trough(self) -> None:
        small_hours = datetime(2026, 6, 30, 3, 0, 0).astimezone()
        plain = v.circadian_baseline(small_hours)
        wired = v.circadian_baseline(small_hours, floor_boost=0.25)
        self.assertGreater(wired, plain)


class RenderTests(unittest.TestCase):
    def test_low_cue_mentions_running_low_and_permission(self) -> None:
        out = v.render_inner_life_block(0.1, "low", user_display_name="Jacob")
        self.assertIn("running low", out)
        # The liven-up permission must be present in the low cue.
        self.assertIn("Jacob", out)
        self.assertTrue("grabs you" in out or "wake up" in out)

    def test_high_cue_mentions_lit_up(self) -> None:
        out = v.render_inner_life_block(0.9, "high")
        self.assertIn("lit up", out)

    def test_normal_is_silent(self) -> None:
        self.assertEqual(v.render_inner_life_block(0.5, "normal"), "")

    def test_rhythm_note_rides_low_cue(self) -> None:
        note = "(your body clock is flipped today -- foggy by day)"
        out = v.render_inner_life_block(0.1, "low", rhythm_note=note)
        self.assertIn("running low", out)
        self.assertTrue(out.endswith(note))

    def test_rhythm_note_silent_on_normal_band(self) -> None:
        # An off-rhythm note never resurrects the silent-normal case.
        note = "(low-battery day)"
        self.assertEqual(
            v.render_inner_life_block(0.5, "normal", rhythm_note=note), "",
        )


if __name__ == "__main__":
    unittest.main()

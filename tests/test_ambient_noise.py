"""Tests for the Phase 4b ambient-noise tracker."""
from __future__ import annotations

import math
import unittest

from app.core.ambient_noise import (
    AmbientNoiseTracker,
)


class AmbientNoiseTrackerTests(unittest.TestCase):
    def test_quiet_room_yields_no_block(self) -> None:
        tracker = AmbientNoiseTracker()
        for _ in range(50):
            tracker.observe(0.001)  # very quiet
        snap = tracker.snapshot()
        self.assertFalse(snap.is_noisy)
        self.assertFalse(snap.is_very_noisy)
        self.assertEqual(tracker.prompt_block(), "")
        self.assertEqual(tracker.tts_volume_db_offset(), 0.0)
        self.assertEqual(tracker.tts_speed_multiplier(), 1.0)

    def test_noisy_room_emits_prompt_cue(self) -> None:
        tracker = AmbientNoiseTracker()
        for _ in range(60):
            tracker.observe(0.020)  # above _LOUD_THRESHOLD
        snap = tracker.snapshot()
        self.assertTrue(snap.is_noisy)
        block = tracker.prompt_block().lower()
        self.assertNotEqual(block, "")
        self.assertIn("speak clearly", block)
        self.assertGreater(tracker.tts_volume_db_offset(), 0.0)
        self.assertLess(tracker.tts_speed_multiplier(), 1.0)

    def test_very_noisy_room_lifts_nudges(self) -> None:
        tracker = AmbientNoiseTracker()
        for _ in range(60):
            tracker.observe(0.060)
        self.assertTrue(tracker.snapshot().is_very_noisy)
        self.assertGreater(tracker.tts_volume_db_offset(), 1.0)
        self.assertLess(tracker.tts_speed_multiplier(), 0.99)

    def test_first_sample_seeds_floor(self) -> None:
        tracker = AmbientNoiseTracker()
        tracker.observe(0.05)
        self.assertAlmostEqual(tracker.snapshot().floor, 0.05, places=4)

    def test_ema_converges_toward_steady_input(self) -> None:
        tracker = AmbientNoiseTracker()
        for _ in range(120):  # ~12 seconds at 100ms chunks
            tracker.observe(0.010)
        floor = tracker.snapshot().floor
        # EMA should be very close to the steady value; allow 5% slack.
        self.assertAlmostEqual(floor, 0.010, delta=0.001)

    def test_ignores_non_finite_input(self) -> None:
        tracker = AmbientNoiseTracker()
        tracker.observe(0.005)
        before = tracker.snapshot().floor
        tracker.observe(math.inf)
        tracker.observe(math.nan)
        tracker.observe(-1.0)
        after = tracker.snapshot().floor
        # Negative values get clamped to 0 (still observed) and the
        # other non-finites are ignored — the EMA should not blow up.
        self.assertTrue(math.isfinite(after))
        self.assertGreaterEqual(after, 0.0)
        # Drift should be small relative to ``before``.
        self.assertLess(abs(after - before), 0.01)

    def test_reset_clears_state(self) -> None:
        tracker = AmbientNoiseTracker()
        for _ in range(20):
            tracker.observe(0.02)
        tracker.reset()
        snap = tracker.snapshot()
        self.assertEqual(snap.floor, 0.0)
        self.assertEqual(snap.samples, 0)
        self.assertFalse(snap.is_noisy)


if __name__ == "__main__":
    unittest.main()

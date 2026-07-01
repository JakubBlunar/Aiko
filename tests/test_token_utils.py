"""Tests for the adaptive token estimator (app.llm.token_utils)."""
from __future__ import annotations

import unittest

from app.llm import token_utils
from app.llm.token_utils import (
    calibration_state,
    chars_per_token,
    estimate_tokens,
    observe_actual_usage,
    reset_calibration,
)


class EstimateTokensTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_calibration()

    def tearDown(self) -> None:
        reset_calibration()

    def test_empty_is_zero(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_nonempty_is_at_least_one(self) -> None:
        self.assertGreaterEqual(estimate_tokens("a"), 1)

    def test_scales_with_length(self) -> None:
        short = estimate_tokens("hello")
        long = estimate_tokens("hello " * 100)
        self.assertGreater(long, short)

    def test_default_ratio(self) -> None:
        self.assertAlmostEqual(chars_per_token(), 3.5, places=3)


class CalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_calibration()

    def tearDown(self) -> None:
        reset_calibration()

    def test_tiny_sample_is_ignored(self) -> None:
        before = chars_per_token()
        observe_actual_usage(prompt_chars=50, actual_prompt_tokens=10)
        self.assertEqual(chars_per_token(), before)
        self.assertEqual(calibration_state()["samples"], 0)

    def test_implausible_ratio_rejected(self) -> None:
        before = chars_per_token()
        # 10000 chars / 100 tokens = 100 chars/token — absurd, must be rejected.
        observe_actual_usage(prompt_chars=10000, actual_prompt_tokens=100)
        self.assertEqual(chars_per_token(), before)
        self.assertEqual(calibration_state()["samples"], 0)

    def test_ema_moves_toward_observation(self) -> None:
        # Observe a consistent 4.5 chars/token (within band, above the 3.5
        # default). The EMA should drift upward but stay below the observation.
        for _ in range(20):
            observe_actual_usage(prompt_chars=4500, actual_prompt_tokens=1000)
        ratio = chars_per_token()
        self.assertGreater(ratio, 3.5)
        self.assertLessEqual(ratio, 4.5)
        self.assertEqual(calibration_state()["samples"], 20)

    def test_ratio_clamped_to_band(self) -> None:
        # Even hammering a near-max observation cannot exceed the clamp.
        for _ in range(500):
            observe_actual_usage(prompt_chars=5000, actual_prompt_tokens=1000)
        self.assertLessEqual(chars_per_token(), 5.0)
        self.assertGreaterEqual(chars_per_token(), 2.5)

    def test_estimate_reflects_calibration(self) -> None:
        text = "x" * 4500
        before = estimate_tokens(text)
        for _ in range(50):
            observe_actual_usage(prompt_chars=4500, actual_prompt_tokens=1000)
        after = estimate_tokens(text)
        # A higher chars/token ratio means fewer estimated tokens for the
        # same text.
        self.assertLess(after, before)

    def test_reset_restores_default(self) -> None:
        for _ in range(10):
            observe_actual_usage(prompt_chars=4500, actual_prompt_tokens=1000)
        self.assertNotAlmostEqual(chars_per_token(), 3.5, places=3)
        reset_calibration()
        self.assertAlmostEqual(chars_per_token(), 3.5, places=3)
        self.assertEqual(calibration_state()["samples"], 0)

    def test_nonpositive_tokens_ignored(self) -> None:
        before = chars_per_token()
        observe_actual_usage(prompt_chars=5000, actual_prompt_tokens=0)
        observe_actual_usage(prompt_chars=5000, actual_prompt_tokens=-5)
        self.assertEqual(chars_per_token(), before)


if __name__ == "__main__":
    unittest.main()

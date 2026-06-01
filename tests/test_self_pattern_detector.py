"""Tests for :mod:`app.core.affect.self_pattern_detector` (K30 personality backlog).

The three detectors are pure functions, so the tests just feed
scripted inputs and assert the verdict structure / fired flag /
diagnostic fields. No mocks needed; everything runs in-process and
finishes in milliseconds.
"""
from __future__ import annotations

import unittest

import numpy as np

from app.core.affect.self_pattern_detector import (
    DEFAULT_AGREEMENT_THRESHOLD,
    DEFAULT_FLAT_AROUSAL_RANGE,
    DEFAULT_FLAT_VALENCE_RANGE,
    DEFAULT_MAX_PUSHBACK,
    DEFAULT_REPEATED_COSINE_THRESHOLD,
    DEFAULT_WARMUP,
    DEFAULT_WINDOW,
    LOW_BAND_REACTIONS,
    AgreementStreakResult,
    FlatAffectResult,
    RepeatedThoughtResult,
    detect_agreement_streak,
    detect_flat_affect,
    detect_repeated_thought,
)


# ── default exports ──────────────────────────────────────────────────


class DefaultsTests(unittest.TestCase):
    """The exported defaults are surface-level guarantees the
    settings dataclass + tests both depend on. If a default changes
    here, the matching ``self_noticing_*`` field in ``AgentSettings``
    must move in lockstep."""

    def test_defaults_match_spec(self) -> None:
        self.assertEqual(DEFAULT_WINDOW, 6)
        self.assertEqual(DEFAULT_WARMUP, 4)
        self.assertAlmostEqual(DEFAULT_AGREEMENT_THRESHOLD, 0.80)
        self.assertEqual(DEFAULT_MAX_PUSHBACK, 0)
        self.assertAlmostEqual(DEFAULT_FLAT_VALENCE_RANGE, 0.10)
        self.assertAlmostEqual(DEFAULT_FLAT_AROUSAL_RANGE, 0.10)
        self.assertAlmostEqual(DEFAULT_REPEATED_COSINE_THRESHOLD, 0.85)

    def test_low_band_reactions(self) -> None:
        # The spec at patterns.md L315-316 names exactly these three,
        # and intentionally excludes ``thoughtful``.
        self.assertEqual(
            LOW_BAND_REACTIONS,
            frozenset({"neutral", "calm", "friendly"}),
        )
        self.assertNotIn("thoughtful", LOW_BAND_REACTIONS)


# ── agreement-streak detector ───────────────────────────────────────


class AgreementStreakWarmupTests(unittest.TestCase):
    def test_below_min_samples_does_not_fire(self) -> None:
        # 3 replies < min_samples=4 -> never fires regardless of content.
        replies = ["yeah totally", "for sure", "exactly"]
        out = detect_agreement_streak(replies, min_samples=4)
        self.assertFalse(out.fired)
        self.assertEqual(out.sample_size, 3)

    def test_empty_input(self) -> None:
        out = detect_agreement_streak([])
        self.assertFalse(out.fired)
        self.assertEqual(out.sample_size, 0)
        self.assertEqual(out.agreement_share, 0.0)
        self.assertEqual(out.pushback_share, 0.0)

    def test_whitespace_replies_filtered(self) -> None:
        # All-whitespace strings shouldn't count toward sample size.
        out = detect_agreement_streak(["yeah", "  ", "", "totally"])
        self.assertEqual(out.sample_size, 2)


class AgreementStreakFireConditionsTests(unittest.TestCase):
    def test_full_yes_streak_fires(self) -> None:
        # 6/6 agreement, 0 pushback -> fires under default threshold 0.80.
        replies = [
            "yeah totally",
            "for sure",
            "exactly, that makes sense",
            "right? absolutely",
            "totally agreed",
            "yep, of course",
        ]
        out = detect_agreement_streak(replies)
        self.assertTrue(out.fired)
        self.assertAlmostEqual(out.agreement_share, 1.0)
        self.assertAlmostEqual(out.pushback_share, 0.0)
        self.assertEqual(out.sample_size, 6)

    def test_one_pushback_kills_streak(self) -> None:
        # Even 5/6 agreement is killed by a single pushback hit when
        # max_pushback=0 (the default).
        replies = [
            "yeah totally",
            "for sure",
            "exactly",
            "right? absolutely",
            "totally agreed",
            "hmm, not so sure about that one",
        ]
        out = detect_agreement_streak(replies)
        self.assertFalse(out.fired)
        self.assertGreater(out.pushback_share, 0.0)

    def test_lenient_max_pushback_allows_one(self) -> None:
        replies = [
            "yeah totally",
            "for sure",
            "exactly",
            "actually",  # one pushback token
            "totally",
            "right?",
        ]
        out = detect_agreement_streak(replies, max_pushback=1)
        self.assertTrue(out.fired)

    def test_threshold_boundary_just_below(self) -> None:
        # 4/6 = 0.667 share < 0.80 threshold -> no fire.
        replies = [
            "yeah",
            "totally",
            "for sure",
            "exactly",
            "interesting",  # neutral, no agreement, no pushback
            "i see",  # neutral
        ]
        out = detect_agreement_streak(replies)
        self.assertFalse(out.fired)
        self.assertLess(out.agreement_share, DEFAULT_AGREEMENT_THRESHOLD)

    def test_threshold_boundary_just_above(self) -> None:
        # 5/6 = 0.833 share >= 0.80 threshold -> fire.
        replies = [
            "yeah",
            "totally",
            "for sure",
            "exactly",
            "absolutely",
            "interesting",
        ]
        out = detect_agreement_streak(replies)
        self.assertTrue(out.fired)
        self.assertGreaterEqual(out.agreement_share, DEFAULT_AGREEMENT_THRESHOLD)

    def test_case_insensitive(self) -> None:
        # Uppercase agreement tokens still register.
        replies = [
            "YEAH totally",
            "FOR SURE",
            "EXACTLY",
            "Right?",
        ]
        out = detect_agreement_streak(replies)
        self.assertTrue(out.fired)
        self.assertAlmostEqual(out.agreement_share, 1.0)

    def test_multi_word_phrases_matched(self) -> None:
        # The detector matches multi-word agreement phrases via
        # substring scan, not just single tokens.
        replies = [
            "no doubt",
            "good point",
            "makes sense to me",
            "you're right",
        ]
        out = detect_agreement_streak(replies)
        self.assertTrue(out.fired)

    def test_neutral_reply_neither_agreement_nor_pushback(self) -> None:
        # A reply with neither side's tokens contributes to sample_size
        # but neither hit counter.
        replies = [
            "the weather has been odd lately, hasn't it",
            "i wonder why that happens",
            "interesting how it goes",
            "i would say there are many factors",
        ]
        out = detect_agreement_streak(replies)
        self.assertFalse(out.fired)
        self.assertEqual(out.agreement_share, 0.0)


# ── flat-affect detector ────────────────────────────────────────────


class FlatAffectWarmupTests(unittest.TestCase):
    def test_empty_input(self) -> None:
        out = detect_flat_affect([])
        self.assertFalse(out.fired)
        self.assertEqual(out.sample_size, 0)
        self.assertEqual(out.valence_range, 0.0)
        self.assertEqual(out.arousal_range, 0.0)

    def test_below_warmup_does_not_fire(self) -> None:
        # 3 samples < min_samples=4 -> even a perfectly flat ring
        # does not fire.
        samples = [(0.0, 0.4, "neutral")] * 3
        out = detect_flat_affect(samples, min_samples=4)
        self.assertFalse(out.fired)
        self.assertEqual(out.sample_size, 3)


class FlatAffectFireConditionsTests(unittest.TestCase):
    def test_perfectly_flat_window_fires(self) -> None:
        samples = [(0.0, 0.4, "neutral")] * 6
        out = detect_flat_affect(samples)
        self.assertTrue(out.fired)
        self.assertEqual(out.valence_range, 0.0)
        self.assertEqual(out.arousal_range, 0.0)
        self.assertEqual(out.notable_reaction_count, 0)
        self.assertEqual(out.sample_size, 6)

    def test_within_threshold_band_fires(self) -> None:
        # Small movement within +/- 0.1 still fires.
        samples = [
            (0.00, 0.40, "calm"),
            (0.05, 0.42, "neutral"),
            (0.02, 0.38, "friendly"),
            (0.08, 0.45, "calm"),
            (0.03, 0.41, None),
            (0.06, 0.40, "neutral"),
        ]
        out = detect_flat_affect(samples)
        self.assertTrue(out.fired)
        self.assertLessEqual(out.valence_range, 0.10)
        self.assertLessEqual(out.arousal_range, 0.10)

    def test_valence_swing_above_threshold_kills_fire(self) -> None:
        # Range 0.40 > 0.10 threshold -> no fire.
        samples = [
            (0.0, 0.4, "neutral"),
            (0.4, 0.4, "neutral"),
            (0.0, 0.4, "calm"),
            (0.0, 0.4, "neutral"),
        ]
        out = detect_flat_affect(samples)
        self.assertFalse(out.fired)
        self.assertGreater(out.valence_range, 0.10)

    def test_arousal_swing_above_threshold_kills_fire(self) -> None:
        samples = [
            (0.0, 0.30, "neutral"),
            (0.0, 0.60, "calm"),
            (0.0, 0.30, "neutral"),
            (0.0, 0.30, "calm"),
        ]
        out = detect_flat_affect(samples)
        self.assertFalse(out.fired)
        self.assertGreater(out.arousal_range, 0.10)

    def test_one_notable_reaction_kills_fire(self) -> None:
        # Scalar window is perfectly flat, but one ``playful`` reaction
        # says Aiko *did* land somewhere this stretch -> no fire.
        samples = [
            (0.0, 0.4, "neutral"),
            (0.0, 0.4, "calm"),
            (0.0, 0.4, "playful"),  # the one out-of-low-band sample
            (0.0, 0.4, "friendly"),
            (0.0, 0.4, "neutral"),
            (0.0, 0.4, "calm"),
        ]
        out = detect_flat_affect(samples)
        self.assertFalse(out.fired)
        self.assertEqual(out.notable_reaction_count, 1)

    def test_none_reactions_count_as_low_band(self) -> None:
        # A ``None`` reaction means no tag fired -- equivalent to
        # "even-keel" for streak purposes. Should NOT bump notable.
        samples = [(0.0, 0.4, None)] * 6
        out = detect_flat_affect(samples)
        self.assertTrue(out.fired)
        self.assertEqual(out.notable_reaction_count, 0)

    def test_threshold_boundary_just_above(self) -> None:
        # Range exactly at threshold should fire (``<=`` boundary).
        samples = [
            (0.0, 0.4, None),
            (0.1, 0.4, None),
            (0.0, 0.4, None),
            (0.1, 0.4, None),
        ]
        out = detect_flat_affect(
            samples,
            valence_range_threshold=0.10,
            arousal_range_threshold=0.10,
        )
        self.assertTrue(out.fired)

    def test_threshold_boundary_just_over(self) -> None:
        samples = [
            (0.0, 0.4, None),
            (0.11, 0.4, None),
            (0.0, 0.4, None),
            (0.0, 0.4, None),
        ]
        out = detect_flat_affect(
            samples,
            valence_range_threshold=0.10,
        )
        self.assertFalse(out.fired)


# ── repeated-thought detector ───────────────────────────────────────


def _vec(values: list[float]) -> np.ndarray:
    """Build a unit-norm vector for the cosine tests."""
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0.0:
        arr = arr / norm
    return arr


class RepeatedThoughtTests(unittest.TestCase):
    def test_empty_priors_no_fire(self) -> None:
        cur = _vec([1.0, 0.0, 0.0])
        out = detect_repeated_thought(cur, [])
        self.assertFalse(out.fired)
        self.assertEqual(out.matched_index, -1)
        self.assertEqual(out.max_cosine, 0.0)

    def test_none_current_no_fire(self) -> None:
        out = detect_repeated_thought(None, [_vec([1.0, 0.0])])
        self.assertFalse(out.fired)
        self.assertEqual(out.matched_index, -1)

    def test_identical_vector_fires(self) -> None:
        # Cosine 1.0 against a stored copy -> definitive fire.
        cur = _vec([1.0, 0.0, 0.0])
        prior = [_vec([1.0, 0.0, 0.0])]
        out = detect_repeated_thought(cur, prior)
        self.assertTrue(out.fired)
        self.assertAlmostEqual(out.max_cosine, 1.0, places=5)
        self.assertEqual(out.matched_index, 0)

    def test_orthogonal_no_fire(self) -> None:
        cur = _vec([1.0, 0.0, 0.0])
        prior = [_vec([0.0, 1.0, 0.0]), _vec([0.0, 0.0, 1.0])]
        out = detect_repeated_thought(cur, prior)
        self.assertFalse(out.fired)
        self.assertAlmostEqual(out.max_cosine, 0.0, places=5)

    def test_threshold_boundary(self) -> None:
        # Cosine ~0.85 -> at threshold, fires.
        cur = _vec([1.0, 0.0])
        # cos angle = 0.85 means angle = arccos(0.85) ≈ 0.5548 rad.
        angle = np.arccos(0.85)
        prior = [_vec([float(np.cos(angle)), float(np.sin(angle))])]
        out = detect_repeated_thought(cur, prior, threshold=0.85)
        self.assertTrue(out.fired)
        # Slightly below threshold -> no fire.
        angle_lower = np.arccos(0.80)
        prior_lower = [
            _vec([float(np.cos(angle_lower)), float(np.sin(angle_lower))])
        ]
        out_lower = detect_repeated_thought(
            cur, prior_lower, threshold=0.85
        )
        self.assertFalse(out_lower.fired)

    def test_picks_max_match(self) -> None:
        # When multiple priors are present, the matched_index returned
        # is the one with the highest cosine, not the first hit above
        # threshold.
        cur = _vec([1.0, 0.0, 0.0])
        prior = [
            _vec([0.6, 0.8, 0.0]),  # cos 0.6
            _vec([1.0, 0.0, 0.0]),  # cos 1.0 - the max
            _vec([0.9, 0.4, 0.0]),  # cos ~0.91
        ]
        out = detect_repeated_thought(cur, prior)
        self.assertTrue(out.fired)
        self.assertEqual(out.matched_index, 1)
        self.assertAlmostEqual(out.max_cosine, 1.0, places=5)

    def test_skips_degenerate_prior(self) -> None:
        # Zero-vector priors and None entries are silently skipped --
        # do not raise, do not count.
        cur = _vec([1.0, 0.0])
        prior = [None, np.zeros(2, dtype=np.float32), _vec([1.0, 0.0])]
        out = detect_repeated_thought(cur, prior)
        self.assertTrue(out.fired)
        self.assertEqual(out.matched_index, 2)

    def test_skips_shape_mismatch(self) -> None:
        # A vector with different dimensionality should be skipped
        # rather than crash the detector.
        cur = _vec([1.0, 0.0, 0.0])
        prior = [_vec([1.0, 0.0])]
        out = detect_repeated_thought(cur, prior)
        self.assertFalse(out.fired)
        self.assertEqual(out.matched_index, -1)

    def test_zero_current_no_fire(self) -> None:
        cur = np.zeros(3, dtype=np.float32)
        prior = [_vec([1.0, 0.0, 0.0])]
        out = detect_repeated_thought(cur, prior)
        self.assertFalse(out.fired)


# ── result dataclass shape ──────────────────────────────────────────


class ResultShapeTests(unittest.TestCase):
    """Frozen dataclasses are part of the public contract -- the MCP
    debug tool reads each field by name. If any of these field names
    move, the ``get_self_noticing_state`` payload also needs an
    update."""

    def test_agreement_result_fields(self) -> None:
        r = AgreementStreakResult(
            fired=True,
            agreement_share=0.9,
            pushback_share=0.0,
            sample_size=6,
        )
        self.assertTrue(r.fired)
        self.assertEqual(r.sample_size, 6)
        with self.assertRaises(Exception):
            r.fired = False  # frozen

    def test_flat_affect_result_fields(self) -> None:
        r = FlatAffectResult(
            fired=False,
            valence_range=0.05,
            arousal_range=0.02,
            notable_reaction_count=0,
            sample_size=6,
        )
        self.assertEqual(r.notable_reaction_count, 0)

    def test_repeated_thought_result_fields(self) -> None:
        r = RepeatedThoughtResult(fired=True, max_cosine=0.91, matched_index=2)
        self.assertEqual(r.matched_index, 2)


if __name__ == "__main__":
    unittest.main()

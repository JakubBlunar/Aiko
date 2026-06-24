"""Tests for :mod:`app.core.conversation.topic_stagnation` (K18 personality backlog).

The detector is a pure streak counter -- no embedder, no rag_store --
so the tests just feed scripted distance streams and assert the
band classification, cooldown, warmup, and post-novelty suppression
behaviour.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.conversation.topic_stagnation import (
    BAND_MILD_LULL,
    BAND_STRONG_LULL,
    StagnationResult,
    TopicStagnationDetector,
    render_inner_life_block,
)


# ── stub helpers ────────────────────────────────────────────────────


def _settings(**overrides: object) -> SimpleNamespace:
    """Compact ``MemorySettings`` stub via ``SimpleNamespace`` getattr."""
    base: dict[str, object] = dict(
        stagnation_window=4,
        stagnation_mild_threshold=0.18,
        stagnation_strong_threshold=0.10,
        stagnation_cooldown_turns=2,
        stagnation_post_novelty_suppression_turns=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(**overrides: object) -> TopicStagnationDetector:
    return TopicStagnationDetector(memory_settings=_settings(**overrides))


# ── tests ───────────────────────────────────────────────────────────


class WarmupTests(unittest.TestCase):
    def test_silent_until_window_full(self) -> None:
        # Window=4 -> first three measurements just fill the deque.
        # Fourth call has the full window AND mean below mild, so it
        # should fire. Three earlier calls must stay silent regardless
        # of how low their values are.
        det = _build(stagnation_window=4)
        self.assertIsNone(det.detect(0.05))
        self.assertIsNone(det.detect(0.05))
        self.assertIsNone(det.detect(0.05))
        out = det.detect(0.05)
        self.assertIsNotNone(out)

    def test_distance_none_skips_without_appending(self) -> None:
        # A None distance must NOT advance the streak counter.
        # Otherwise three None turns followed by a real low distance
        # would prematurely fill the window with phantom samples.
        det = _build(stagnation_window=3)
        self.assertIsNone(det.detect(None))
        self.assertIsNone(det.detect(None))
        self.assertIsNone(det.detect(None))
        # Now feed three real low distances; only on the third do we
        # have a full window and may fire.
        self.assertIsNone(det.detect(0.05))
        self.assertIsNone(det.detect(0.05))
        out = det.detect(0.05)
        self.assertIsNotNone(out)


class BandClassificationTests(unittest.TestCase):
    def test_mean_below_strong_fires_strong_lull(self) -> None:
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.20,
            stagnation_strong_threshold=0.10,
        )
        det.detect(0.05)
        det.detect(0.05)
        out = det.detect(0.05)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.band, BAND_STRONG_LULL)
        self.assertAlmostEqual(out.mean_distance, 0.05, places=4)
        self.assertEqual(out.window_size, 3)

    def test_mean_in_mild_band_fires_mild_lull(self) -> None:
        # Mean = 0.15 sits in [strong=0.10, mild=0.20).
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.20,
            stagnation_strong_threshold=0.10,
        )
        det.detect(0.15)
        det.detect(0.15)
        out = det.detect(0.15)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.band, BAND_MILD_LULL)

    def test_mean_above_mild_stays_silent(self) -> None:
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.20,
            stagnation_strong_threshold=0.10,
        )
        det.detect(0.30)
        det.detect(0.40)
        self.assertIsNone(det.detect(0.50))

    def test_misordered_thresholds_collapse_safely(self) -> None:
        # If config ships with strong > mild (a misconfiguration the
        # parser still happily accepts), the detector must not
        # over-fire. With strong=0.30 > mild=0.10 we should clamp
        # strong down to mild (0.10), so a 0.15 mean stays silent.
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.10,
            stagnation_strong_threshold=0.30,
        )
        det.detect(0.15)
        det.detect(0.15)
        # Mean = 0.15 is > clamped strong (0.10) and >= clamped mild
        # (0.10) -> silent (mild gate is `< 0.10`).
        self.assertIsNone(det.detect(0.15))


class CooldownTests(unittest.TestCase):
    def test_post_hit_cooldown_suppresses_consecutive_fires(self) -> None:
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.20,
            stagnation_strong_threshold=0.10,
            stagnation_cooldown_turns=2,
        )
        det.detect(0.05)
        det.detect(0.05)
        first = det.detect(0.05)
        self.assertIsNotNone(first)
        # Cooldown=2 -> next two calls are suppressed even though the
        # rolling mean stays comfortably under both thresholds.
        self.assertIsNone(det.detect(0.05))
        self.assertIsNone(det.detect(0.05))
        # Cooldown expired: the still-low mean fires again.
        third = det.detect(0.05)
        self.assertIsNotNone(third)


class PostNoveltySuppressionTests(unittest.TestCase):
    def test_novelty_just_fired_arms_suppression(self) -> None:
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.20,
            stagnation_strong_threshold=0.10,
            stagnation_post_novelty_suppression_turns=2,
        )
        # Fill the window with low distances.
        det.detect(0.05)
        det.detect(0.05)
        # On the *next* call, novelty just fired. Even though our
        # window is full and the mean is below threshold, the
        # suppression window must mute the next two turns.
        suppressed = det.detect(0.05, novelty_just_fired=True)
        self.assertIsNone(suppressed)
        self.assertIsNone(det.detect(0.05))
        # Two suppression turns have ticked off; the next fire is
        # allowed again.
        out = det.detect(0.05)
        self.assertIsNotNone(out)

    def test_novelty_none_distance_still_arms_suppression(self) -> None:
        # ``novelty_just_fired`` should arm suppression even when the
        # K6 detector didn't actually measure (e.g. a long user
        # message hit the band but K6 returned a band+None distance
        # combo, which doesn't happen today but the suppression
        # arming must not depend on it).
        det = _build(
            stagnation_window=3,
            stagnation_mild_threshold=0.20,
            stagnation_strong_threshold=0.10,
            stagnation_post_novelty_suppression_turns=2,
        )
        # Pre-fill the window with above-threshold distances so the
        # detector is "warm" but hasn't fired (no cooldown to muddy
        # the test).
        det.detect(0.30)
        det.detect(0.30)
        self.assertIsNone(det.detect(0.30))
        # Now arm suppression with a None distance; the suppression
        # counter must be set even though no measurement happened.
        det.detect(None, novelty_just_fired=True)
        # The next two real measurements should be quiet because
        # suppression is hot. Even though the deque slowly fills
        # with low distances and the mean drifts down, no fire is
        # allowed.
        self.assertIsNone(det.detect(0.05))
        self.assertIsNone(det.detect(0.05))
        # Suppression has fully ticked off; the third real low
        # measurement is allowed to fire (window now [0.05, 0.05,
        # 0.05], mean below strong threshold).
        out = det.detect(0.05)
        self.assertIsNotNone(out)


class RenderTests(unittest.TestCase):
    def test_render_strong_lull(self) -> None:
        block = render_inner_life_block(
            StagnationResult(
                band=BAND_STRONG_LULL, mean_distance=0.05, window_size=6,
            ),
        )
        self.assertIn("Heads-up", block)
        self.assertIn("looped", block)

    def test_render_mild_lull_uses_user_display_name(self) -> None:
        block = render_inner_life_block(
            StagnationResult(
                band=BAND_MILD_LULL, mean_distance=0.15, window_size=6,
            ),
            user_display_name="Sam",
        )
        self.assertIn("Heads-up", block)
        self.assertIn("Sam", block)
        self.assertNotIn("{user_name}", block)

    def test_render_falls_back_to_default_name(self) -> None:
        block = render_inner_life_block(
            StagnationResult(
                band=BAND_MILD_LULL, mean_distance=0.15, window_size=6,
            ),
            user_display_name="",
        )
        # Empty/whitespace name falls back to "Jacob" so the rendered
        # text still reads naturally.
        self.assertIn("Jacob", block)

    def test_render_none_is_empty(self) -> None:
        self.assertEqual(render_inner_life_block(None), "")

    def test_render_names_clean_topic_label(self) -> None:
        # F10k: a clean cluster label is spliced as a don't-quote clause.
        block = render_inner_life_block(
            StagnationResult(
                band=BAND_MILD_LULL, mean_distance=0.15, window_size=6,
            ),
            topic_label="work stress",
        )
        self.assertIn("work stress", block)
        self.assertIn("don't quote", block)

    def test_render_drops_dirty_topic_label(self) -> None:
        # F10k: an over-long / multiline label is not spliced verbatim.
        block = render_inner_life_block(
            StagnationResult(
                band=BAND_STRONG_LULL, mean_distance=0.05, window_size=6,
            ),
            topic_label="y" * 80,
        )
        self.assertNotIn("y" * 80, block)
        self.assertNotIn("Context", block)


if __name__ == "__main__":
    unittest.main()

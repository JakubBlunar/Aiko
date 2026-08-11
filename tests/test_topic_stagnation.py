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
    in_standing_lull,
    lull_band,
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


class SelfCalibrationTests(unittest.TestCase):
    """The bands are percentiles of the install's own history.

    The shipped constants (mild 0.18) turned out to be below the lowest
    reading this corpus has ever produced — 52 consecutive windows in
    0.310-0.422, all silent — so K18 could not fire and the
    dormant-interest re-opener downstream of it never rendered once.
    Absolute cosine thresholds do not survive a change of embedding
    model; the distribution does.
    """

    def _kv(self):
        store: dict[str, str] = {}
        return store, store.get, store.__setitem__

    def _feed(self, det, values):
        for value in values:
            det.detect(value)

    def test_the_configured_constants_hold_until_there_is_a_baseline(
        self,
    ) -> None:
        det = _build(stagnation_window=4)
        self.assertFalse(det.adaptive)
        self.assertAlmostEqual(det.mild_threshold, 0.18)
        self.assertAlmostEqual(det.strong_threshold, 0.10)

    def test_a_high_distance_corpus_still_produces_a_lull_band(self) -> None:
        # The live failure in miniature: every reading sits well above the
        # configured 0.18, so under absolute thresholds nothing ever fires.
        store, kv_get, kv_set = self._kv()
        det = TopicStagnationDetector(
            memory_settings=_settings(stagnation_window=2),
            kv_get=kv_get,
            kv_set=kv_set,
        )
        stream = [0.30 + (i % 13) * 0.01 for i in range(200)]
        self._feed(det, stream)
        self.assertTrue(det.adaptive)
        self.assertGreater(det.mild_threshold, 0.18)
        self.assertLessEqual(det.strong_threshold, det.mild_threshold)
        # And the band is genuinely reachable: the quietest stretch of
        # the same corpus now scores as a lull.
        self.assertLess(det.strong_threshold, det.mild_threshold + 1e-9)
        det._cooldown_remaining = 0
        det._post_novelty_suppression = 0
        out = det.detect(0.20)
        self.assertIsNotNone(out)

    def test_the_band_is_a_minority_of_her_own_readings(self) -> None:
        # A threshold that fires on half of all turns is not a lull.
        store, kv_get, kv_set = self._kv()
        det = TopicStagnationDetector(
            memory_settings=_settings(stagnation_window=2),
            kv_get=kv_get,
            kv_set=kv_set,
        )
        self._feed(det, [0.20 + (i % 40) * 0.01 for i in range(400)])
        snapshot = det.baseline_snapshot()
        below = sum(
            1 for v in det._baseline if v < det.mild_threshold
        )
        share = below / max(1, snapshot["samples"])
        self.assertLess(share, 0.25, f"mild band fires on {share:.0%} of turns")
        self.assertGreater(share, 0.02)

    def test_the_baseline_survives_a_restart(self) -> None:
        store, kv_get, kv_set = self._kv()
        first = TopicStagnationDetector(
            memory_settings=_settings(stagnation_window=2),
            kv_get=kv_get, kv_set=kv_set,
        )
        self._feed(first, [0.30 + (i % 11) * 0.01 for i in range(200)])
        self.assertTrue(first.adaptive)
        second = TopicStagnationDetector(
            memory_settings=_settings(stagnation_window=2),
            kv_get=kv_get, kv_set=kv_set,
        )
        self.assertTrue(second.adaptive)
        self.assertAlmostEqual(
            second.mild_threshold, first.mild_threshold, places=3
        )

    def test_no_kv_means_no_calibration_and_no_crash(self) -> None:
        det = _build(stagnation_window=2)
        self._feed(det, [0.30 + (i % 11) * 0.01 for i in range(200)])
        self.assertTrue(det.adaptive, "in-memory baseline should still build")
        self.assertIsNotNone(det.baseline_snapshot()["samples"])

    def test_a_corrupt_baseline_falls_back_instead_of_raising(self) -> None:
        det = TopicStagnationDetector(
            memory_settings=_settings(),
            kv_get=lambda _k: "{not json",
            kv_set=lambda _k, _v: None,
        )
        self.assertFalse(det.adaptive)
        self.assertAlmostEqual(det.mild_threshold, 0.18)


class StandingLullTests(unittest.TestCase):
    """The shared predicate five prompt blocks gate on.

    It exists because each of them used to spell the test out inline and
    most spelled it wrong: two inverted the comparison, and all but one
    read the configured constant instead of the band the detector had
    calibrated to.
    """

    def _det(self, mean: float | None, band: float = 0.20) -> SimpleNamespace:
        return SimpleNamespace(last_mean=mean, mild_threshold=band)

    def test_a_quiet_reading_is_a_lull(self) -> None:
        self.assertTrue(in_standing_lull(self._det(0.05)))

    def test_a_moving_conversation_is_not(self) -> None:
        # The direction that was backwards in the wild: a *high* mean is
        # divergence, which is the opposite of circling.
        self.assertFalse(in_standing_lull(self._det(0.9)))

    def test_an_unfilled_window_reads_as_no_lull(self) -> None:
        self.assertFalse(in_standing_lull(self._det(None)))

    def test_a_missing_detector_reads_as_no_lull(self) -> None:
        self.assertFalse(in_standing_lull(None, _settings()))

    def test_the_bar_is_the_calibrated_band_not_the_constant(self) -> None:
        settings = _settings(stagnation_mild_threshold=0.18)
        det = self._det(0.30, band=0.35)
        self.assertAlmostEqual(lull_band(det, settings), 0.35)
        self.assertTrue(in_standing_lull(det, settings))

    def test_the_constant_stands_in_before_a_detector_exists(self) -> None:
        self.assertAlmostEqual(
            lull_band(None, _settings(stagnation_mild_threshold=0.42)), 0.42
        )

    def test_a_live_detector_agrees_with_its_own_band(self) -> None:
        det = _build()
        for _ in range(4):
            det.detect(0.05)
        self.assertTrue(in_standing_lull(det))


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

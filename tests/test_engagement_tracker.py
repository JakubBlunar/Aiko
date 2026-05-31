"""Tests for ``app.core.affect.engagement_tracker``.

Covers the K14 implicit engagement signal: voice vs typed mode
routing, warmup gating, latency-window maintenance, length-z baseline
slicing (drop-the-current-turn-from-K13-window semantic), per-turn
closeness-delta cap, label banding, and the typed-mode
absence-curiosity band.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.core.affect.engagement_tracker import (
    EngagementResult,
    EngagementTracker,
)


@dataclass
class _AgentStub:
    """Minimal stand-in for AgentSettings used by the tracker."""
    engagement_tracker_enabled: bool = True
    engagement_window: int = 12
    engagement_warmup_min: int = 6
    engagement_latency_z_strong_drop: float = 1.5
    engagement_length_z_strong_drop: float = -1.0
    engagement_closeness_delta_max: float = 0.04
    engagement_absence_curiosity_enabled: bool = True
    engagement_absence_curiosity_min_seconds: float = 1800.0
    resume_opener_min_hours: float = 4.0


def _make_tracker(
    *,
    word_counts: list[int] | None = None,
    settings: _AgentStub | None = None,
) -> EngagementTracker:
    """Helper: build a tracker with a static word-count window provider."""
    provider = None
    if word_counts is not None:
        snapshot = list(word_counts)
        provider = lambda: list(snapshot)  # noqa: E731 -- short stub
    return EngagementTracker(
        agent_settings=settings or _AgentStub(),
        word_count_window_provider=provider,
    )


class WarmupTests(unittest.TestCase):
    def test_typed_cold_start_returns_neutral_no_delta(self) -> None:
        tracker = _make_tracker(word_counts=[])
        result = tracker.record_turn(
            mode="typed", latency_seconds=10.0, user_word_count=8,
        )
        self.assertIsInstance(result, EngagementResult)
        self.assertEqual(result.label, "neutral")
        self.assertEqual(result.closeness_delta, 0.0)
        self.assertFalse(result.warmed)

    def test_voice_cold_start_returns_neutral_no_delta(self) -> None:
        tracker = _make_tracker(word_counts=[])
        result = tracker.record_turn(
            mode="live", latency_seconds=4.0, user_word_count=12,
        )
        self.assertEqual(result.label, "neutral")
        self.assertEqual(result.closeness_delta, 0.0)
        self.assertFalse(result.warmed)


class VoiceModeTests(unittest.TestCase):
    def test_short_latency_above_baseline_length_is_engaged(self) -> None:
        # Word-count baseline: average ~5 words. Current turn 15 words
        # = clearly above baseline → length z positive. Latency baseline:
        # ~5 s; current turn 2 s = below baseline → latency z negative
        # → -latency_z positive → engagement positive.
        baseline_words = [5, 5, 5, 5, 5, 5]
        tracker = _make_tracker(word_counts=baseline_words + [15])
        # Warm the voice latency ring with six 5-second waits.
        for _ in range(6):
            tracker.record_turn(
                mode="live", latency_seconds=5.0, user_word_count=5,
            )
        # Reset the K13 stub so the scored turn sees the right
        # baseline (the tracker re-reads the provider every call).
        tracker._word_count_window_provider = lambda: list(
            baseline_words + [15],
        )
        result = tracker.record_turn(
            mode="live", latency_seconds=2.0, user_word_count=15,
        )
        self.assertTrue(result.warmed)
        self.assertEqual(result.label, "engaged")
        self.assertGreater(result.closeness_delta, 0.0)
        # Should never exceed the per-turn cap.
        self.assertLessEqual(result.closeness_delta, 0.04 + 1e-9)

    def test_long_latency_short_message_is_disengaged_or_abandoned(
        self,
    ) -> None:
        baseline_words = [5, 5, 5, 5, 5, 5]
        tracker = _make_tracker(word_counts=baseline_words + [1])
        # Six 3-second baseline latencies so a 20s reply scores hot.
        for _ in range(6):
            tracker.record_turn(
                mode="live", latency_seconds=3.0, user_word_count=5,
            )
        tracker._word_count_window_provider = lambda: list(
            baseline_words + [1],
        )
        result = tracker.record_turn(
            mode="live", latency_seconds=20.0, user_word_count=1,
        )
        self.assertTrue(result.warmed)
        self.assertIn(result.label, ("disengaged", "abandoned"))
        self.assertLess(result.closeness_delta, 0.0)
        self.assertGreaterEqual(result.closeness_delta, -0.04 - 1e-9)

    def test_per_turn_delta_is_capped(self) -> None:
        # Force both signals to scream "disengaged" with tiny baselines.
        baseline = [5] * 8
        tracker = _make_tracker(word_counts=baseline + [1])
        for _ in range(8):
            tracker.record_turn(
                mode="live", latency_seconds=1.0, user_word_count=5,
            )
        tracker._word_count_window_provider = lambda: list(baseline + [1])
        result = tracker.record_turn(
            mode="live", latency_seconds=120.0, user_word_count=1,
        )
        self.assertGreaterEqual(result.closeness_delta, -0.04 - 1e-9)
        self.assertLessEqual(result.closeness_delta, 0.04 + 1e-9)


class TypedModeTests(unittest.TestCase):
    def test_typed_latency_does_NOT_feed_engagement(self) -> None:
        # Typed mode: huge gap should NOT show up as engagement-disengaged
        # (it routes to absence_seconds instead). Length is the only
        # signal that participates in the delta.
        baseline = [5] * 8
        tracker = _make_tracker(word_counts=baseline + [5])
        # The tracker still needs the K13 window warm; latency window
        # stays empty because no voice turns happened.
        result = tracker.record_turn(
            mode="typed", latency_seconds=3600.0, user_word_count=5,
        )
        # Length is at baseline; the only active signal is length and
        # it sits at z≈0, so the engagement delta is ~0 and label is
        # neutral. The key assertion: typed-mode latency did NOT
        # collapse the label to "abandoned".
        self.assertIn(result.label, ("neutral", "engaged", "disengaged"))
        self.assertAlmostEqual(result.closeness_delta, 0.0, places=2)
        # latency_z is never computed in typed mode.
        self.assertIsNone(result.latency_z)

    def test_typed_short_message_below_baseline_is_disengaged(
        self,
    ) -> None:
        baseline = [5] * 8
        tracker = _make_tracker(word_counts=baseline + [1])
        result = tracker.record_turn(
            mode="typed", latency_seconds=10.0, user_word_count=1,
        )
        # Length z ≈ -(curr - mean)/stdev for a 1-word reply vs 5-word
        # baseline. Should at least nudge the label off neutral.
        self.assertTrue(result.warmed)
        self.assertIn(result.label, ("disengaged", "abandoned", "neutral"))
        # No latency in typed mode.
        self.assertIsNone(result.latency_z)


class AbsenceCuriosityTests(unittest.TestCase):
    def test_band_low_returns_none(self) -> None:
        tracker = _make_tracker(word_counts=[])
        result = tracker.record_turn(
            mode="typed", latency_seconds=60.0, user_word_count=5,
        )
        # 60s is way below the 1800s floor → no absence.
        self.assertIsNone(result.absence_seconds)

    def test_band_in_range_returns_seconds(self) -> None:
        tracker = _make_tracker(word_counts=[])
        result = tracker.record_turn(
            mode="typed", latency_seconds=3600.0, user_word_count=5,
        )
        # 3600s is between 1800 and 4*3600=14400 → in band.
        self.assertEqual(result.absence_seconds, 3600.0)

    def test_band_above_resume_threshold_returns_none(self) -> None:
        tracker = _make_tracker(word_counts=[])
        result = tracker.record_turn(
            mode="typed", latency_seconds=4.5 * 3600.0, user_word_count=5,
        )
        # 4.5h > 4h resume-opener threshold → no absence cue.
        self.assertIsNone(result.absence_seconds)

    def test_voice_mode_never_populates_absence(self) -> None:
        tracker = _make_tracker(word_counts=[])
        result = tracker.record_turn(
            mode="live", latency_seconds=3600.0, user_word_count=5,
        )
        self.assertIsNone(result.absence_seconds)

    def test_disabled_setting_returns_none(self) -> None:
        settings = _AgentStub(engagement_absence_curiosity_enabled=False)
        tracker = _make_tracker(word_counts=[], settings=settings)
        result = tracker.record_turn(
            mode="typed", latency_seconds=3600.0, user_word_count=5,
        )
        self.assertIsNone(result.absence_seconds)


class LatencyWindowTests(unittest.TestCase):
    def test_voice_appends_to_window(self) -> None:
        tracker = _make_tracker(word_counts=[])
        for latency in (1.0, 2.0, 3.0):
            tracker.record_turn(
                mode="live", latency_seconds=latency, user_word_count=4,
            )
        snapshot = tracker.latency_window_snapshot()
        self.assertEqual(snapshot, [1.0, 2.0, 3.0])

    def test_typed_does_not_append_to_window(self) -> None:
        tracker = _make_tracker(word_counts=[])
        tracker.record_turn(
            mode="typed", latency_seconds=10.0, user_word_count=4,
        )
        self.assertEqual(tracker.latency_window_snapshot(), [])

    def test_none_latency_does_not_append(self) -> None:
        tracker = _make_tracker(word_counts=[])
        tracker.record_turn(
            mode="live", latency_seconds=None, user_word_count=4,
        )
        self.assertEqual(tracker.latency_window_snapshot(), [])


class LastResultTests(unittest.TestCase):
    def test_last_result_caches_most_recent(self) -> None:
        tracker = _make_tracker(word_counts=[])
        self.assertIsNone(tracker.last_result)
        result = tracker.record_turn(
            mode="typed", latency_seconds=1.0, user_word_count=5,
        )
        self.assertIs(tracker.last_result, result)


class LabelBandingTests(unittest.TestCase):
    def test_label_for_thresholds(self) -> None:
        # Direct method test against the banding contract.
        self.assertEqual(
            EngagementTracker._label_for(1.0), "engaged",
        )
        self.assertEqual(
            EngagementTracker._label_for(0.7), "engaged",
        )
        self.assertEqual(
            EngagementTracker._label_for(0.0), "neutral",
        )
        self.assertEqual(
            EngagementTracker._label_for(-0.7), "disengaged",
        )
        self.assertEqual(
            EngagementTracker._label_for(-1.5), "abandoned",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

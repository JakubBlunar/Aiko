"""J5 — reconnection ritual: pure helpers + provider plumbing."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.relationship import reconnection as rc
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


class ThresholdTests(unittest.TestCase):
    def test_neutral_closeness_is_base(self) -> None:
        self.assertAlmostEqual(
            rc.reconnection_threshold_hours(0.0, base_hours=24.0), 24.0,
        )

    def test_close_lowers_threshold(self) -> None:
        # closeness +1 -> 70% of base.
        self.assertAlmostEqual(
            rc.reconnection_threshold_hours(1.0, base_hours=24.0), 16.8,
        )

    def test_distant_raises_threshold(self) -> None:
        self.assertAlmostEqual(
            rc.reconnection_threshold_hours(-1.0, base_hours=24.0), 31.2,
        )

    def test_min_floor_applies(self) -> None:
        # Tiny base + max closeness must not drop below the 6h floor.
        self.assertEqual(
            rc.reconnection_threshold_hours(1.0, base_hours=4.0), 6.0,
        )

    def test_none_closeness_treated_neutral(self) -> None:
        self.assertAlmostEqual(
            rc.reconnection_threshold_hours(None, base_hours=24.0), 24.0,
        )


class ShouldReconnectTests(unittest.TestCase):
    def test_none_gap_is_false(self) -> None:
        self.assertFalse(
            rc.should_reconnect(None, closeness=0.0, base_hours=24.0)
        )

    def test_below_threshold_false(self) -> None:
        self.assertFalse(
            rc.should_reconnect(10 * 3600, closeness=0.0, base_hours=24.0)
        )

    def test_above_threshold_true(self) -> None:
        self.assertTrue(
            rc.should_reconnect(30 * 3600, closeness=0.0, base_hours=24.0)
        )

    def test_closeness_pulls_threshold_down(self) -> None:
        # 18h gap: below the neutral 24h bar, but a very close
        # relationship (threshold 16.8h) reconnects.
        gap = 18 * 3600
        self.assertFalse(
            rc.should_reconnect(gap, closeness=0.0, base_hours=24.0)
        )
        self.assertTrue(
            rc.should_reconnect(gap, closeness=1.0, base_hours=24.0)
        )


class HumanizeGapTests(unittest.TestCase):
    def test_buckets(self) -> None:
        self.assertEqual(rc.humanize_gap(None), "a while")
        self.assertEqual(rc.humanize_gap(10 * 3600), "several hours")
        self.assertEqual(rc.humanize_gap(26 * 3600), "about a day")
        self.assertEqual(rc.humanize_gap(3 * 24 * 3600), "3 days")
        self.assertEqual(rc.humanize_gap(7 * 24 * 3600), "about a week")
        self.assertEqual(rc.humanize_gap(21 * 24 * 3600), "3 weeks")
        self.assertEqual(rc.humanize_gap(30 * 24 * 3600), "about a month")
        self.assertEqual(rc.humanize_gap(90 * 24 * 3600), "3 months")


class _AxesStore:
    def __init__(self, closeness: float) -> None:
        self._c = closeness

    def get(self, _user_id: str) -> SimpleNamespace:
        return SimpleNamespace(closeness=self._c)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        enabled: bool = True,
        gap_info: tuple[float, str] | None,
        closeness: float = 0.0,
        stage: str = "new",
    ) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                reconnection_enabled=enabled,
                reconnection_base_gap_hours=24.0,
            )
        )
        self._user_id = "jacob"
        self.user_display_name = "Jacob"
        self._relationship_axes_store = _AxesStore(closeness)
        self._reconnection_anchored_at: str | None = None
        self._gap_info = gap_info
        self.relationship_stage_now = lambda: stage  # type: ignore[assignment]

    def _last_assistant_gap_info(self):  # type: ignore[override]
        return self._gap_info


class ReconnectionProviderTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(enabled=False, gap_info=(48 * 3600, "t0"))
        self.assertEqual(host._render_reconnection_block(), "")

    def test_no_history_returns_empty(self) -> None:
        host = _Host(gap_info=None)
        self.assertEqual(host._render_reconnection_block(), "")

    def test_short_gap_returns_empty(self) -> None:
        host = _Host(gap_info=(5 * 3600, "t0"))
        self.assertEqual(host._render_reconnection_block(), "")

    def test_long_gap_fires_and_anchors(self) -> None:
        host = _Host(gap_info=(3 * 24 * 3600, "t0"))
        block = host._render_reconnection_block()
        self.assertIn("Jacob", block)
        self.assertIn("3 days", block)
        self.assertIn("good to see", block.lower())
        self.assertEqual(host._reconnection_anchored_at, "t0")

    def test_one_shot_same_return(self) -> None:
        host = _Host(gap_info=(3 * 24 * 3600, "t0"))
        self.assertTrue(host._render_reconnection_block())
        # Same anchor -> suppressed.
        self.assertEqual(host._render_reconnection_block(), "")

    def test_new_return_fires_again(self) -> None:
        host = _Host(gap_info=(3 * 24 * 3600, "t0"))
        self.assertTrue(host._render_reconnection_block())
        host._gap_info = (3 * 24 * 3600, "t1")
        self.assertTrue(host._render_reconnection_block())

    def test_close_stage_warmer_tone(self) -> None:
        host = _Host(gap_info=(3 * 24 * 3600, "t0"), stage="close")
        block = host._render_reconnection_block().lower()
        self.assertIn("warmth show", block)

    def test_new_stage_light_tone(self) -> None:
        host = _Host(gap_info=(3 * 24 * 3600, "t0"), stage="new")
        block = host._render_reconnection_block().lower()
        self.assertIn("light and genuine", block)

    def test_never_guilt_trips(self) -> None:
        host = _Host(gap_info=(3 * 24 * 3600, "t0"))
        self.assertIn("never guilt-trip", host._render_reconnection_block().lower())

    def test_closeness_lowers_bar(self) -> None:
        # 18h gap: silent at neutral, fires for a close relationship.
        neutral = _Host(gap_info=(18 * 3600, "t0"), closeness=0.0)
        self.assertEqual(neutral._render_reconnection_block(), "")
        close = _Host(gap_info=(18 * 3600, "t0"), closeness=1.0)
        self.assertTrue(close._render_reconnection_block())


if __name__ == "__main__":
    unittest.main()

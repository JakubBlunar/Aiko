"""Pure-helper tests for the K25 memory confidence time-decay.

Exercises :func:`_compute_effective_confidence` (the linear-with-floor
math) and :func:`_is_distant_memory` (the predicate that drives the
``(distant)`` suffix).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.rag.rag_retriever import (
    _CONFIDENCE_DECAY_DEFAULT_FLOOR,
    _CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS,
    _CONFIDENCE_DECAY_DEFAULT_THRESHOLD,
    _compute_effective_confidence,
    _is_distant_memory,
)


def _iso_days_ago(now: datetime, days: float) -> str:
    return (now - timedelta(days=days)).isoformat()


class EffectiveConfidenceMathTests(unittest.TestCase):
    """Verifies the linear-with-floor formula returns expected values."""

    def test_zero_age_returns_stored(self) -> None:
        self.assertAlmostEqual(
            _compute_effective_confidence(
                0.7, age_days=0, horizon_days=365, floor=0.3,
            ),
            0.7,
        )

    def test_half_horizon_half_decay(self) -> None:
        # multiplier = 1 - 182.5/365 = 0.5; stored * 0.5 = 0.35
        result = _compute_effective_confidence(
            0.7, age_days=182.5, horizon_days=365, floor=0.3,
        )
        self.assertAlmostEqual(result, 0.7 * 0.5, places=4)

    def test_full_horizon_hits_floor(self) -> None:
        # multiplier saturates at floor (0.3) once age >= horizon.
        # stored * floor = 0.7 * 0.3 = 0.21
        result = _compute_effective_confidence(
            0.7, age_days=365, horizon_days=365, floor=0.3,
        )
        self.assertAlmostEqual(result, 0.7 * 0.3, places=4)

    def test_beyond_horizon_clamped_at_floor(self) -> None:
        # Three years past horizon -- multiplier still clamped at floor.
        result = _compute_effective_confidence(
            0.7, age_days=365 * 4, horizon_days=365, floor=0.3,
        )
        self.assertAlmostEqual(result, 0.7 * 0.3, places=4)

    def test_floor_one_disables_decay(self) -> None:
        # floor=1.0 means multiplier saturates at 1.0 for all ages.
        result = _compute_effective_confidence(
            0.7, age_days=10_000, horizon_days=365, floor=1.0,
        )
        self.assertAlmostEqual(result, 0.7, places=4)

    def test_horizon_zero_returns_stored(self) -> None:
        # Defensive: horizon_days <= 0 would zero-divide; helper
        # short-circuits to stored.
        result = _compute_effective_confidence(
            0.7, age_days=180, horizon_days=0, floor=0.3,
        )
        self.assertAlmostEqual(result, 0.7, places=4)

    def test_horizon_negative_returns_stored(self) -> None:
        result = _compute_effective_confidence(
            0.7, age_days=180, horizon_days=-5, floor=0.3,
        )
        self.assertAlmostEqual(result, 0.7, places=4)

    def test_high_confidence_decays(self) -> None:
        # stored 0.9 at half-horizon -> 0.9 * 0.5 = 0.45
        result = _compute_effective_confidence(
            0.9, age_days=182.5, horizon_days=365, floor=0.3,
        )
        self.assertAlmostEqual(result, 0.45, places=4)

    def test_result_is_clamped_to_unit_interval(self) -> None:
        # Defensive: a caller passing stored=1.2 still gets <=1.0.
        result = _compute_effective_confidence(
            1.2, age_days=0, horizon_days=365, floor=0.3,
        )
        self.assertLessEqual(result, 1.0)
        # And a caller passing negative stored doesn't go below 0.
        result = _compute_effective_confidence(
            -0.5, age_days=0, horizon_days=365, floor=0.3,
        )
        self.assertGreaterEqual(result, 0.0)


class IsDistantMemoryTests(unittest.TestCase):
    """Predicate boundary + bypass behaviour."""

    def setUp(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _detect(
        self,
        *,
        stored: float | None = 0.7,
        age_days: float = 180.0,
        pinned: bool = False,
        horizon: int = _CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS,
        floor: float = _CONFIDENCE_DECAY_DEFAULT_FLOOR,
        threshold: float = _CONFIDENCE_DECAY_DEFAULT_THRESHOLD,
        created_at: str | None = None,
    ) -> bool:
        if created_at is None and age_days is not None:
            created_at = _iso_days_ago(self.now, age_days)
        return _is_distant_memory(
            stored_confidence=stored,
            created_at=created_at,
            now=self.now,
            horizon_days=horizon,
            floor=floor,
            threshold=threshold,
            pinned=pinned,
        )

    def test_fires_for_old_default_confidence(self) -> None:
        # 0.7 * (1 - 180/365) = 0.355 < 0.5 -> distant.
        self.assertTrue(self._detect(stored=0.7, age_days=180))

    def test_doesnt_fire_for_recent_memory(self) -> None:
        # 0.7 * (1 - 5/365) = 0.690 > 0.5 -> not distant.
        self.assertFalse(self._detect(stored=0.7, age_days=5))

    def test_high_confidence_resists_short_age(self) -> None:
        # 0.9 * (1 - 60/365) = 0.752 > 0.5 -> not distant.
        self.assertFalse(self._detect(stored=0.9, age_days=60))

    def test_high_confidence_eventually_decays(self) -> None:
        # 0.9 * (1 - 200/365) = 0.407 < 0.5 -> distant.
        self.assertTrue(self._detect(stored=0.9, age_days=200))

    def test_pinned_bypasses_even_when_old(self) -> None:
        # Pinned row at age 5 years with stored=0.95 -> NOT distant.
        self.assertFalse(self._detect(stored=0.95, age_days=365 * 5, pinned=True))

    def test_none_confidence_returns_false(self) -> None:
        self.assertFalse(self._detect(stored=None, age_days=200))

    def test_none_created_at_returns_false(self) -> None:
        # Pass an explicit None for created_at (bypasses the age helper).
        result = _is_distant_memory(
            stored_confidence=0.7,
            created_at=None,
            now=self.now,
            horizon_days=365,
            floor=0.3,
            threshold=0.5,
            pinned=False,
        )
        self.assertFalse(result)

    def test_malformed_created_at_returns_false(self) -> None:
        result = _is_distant_memory(
            stored_confidence=0.7,
            created_at="not-a-date",
            now=self.now,
            horizon_days=365,
            floor=0.3,
            threshold=0.5,
            pinned=False,
        )
        self.assertFalse(result)

    def test_zulu_suffix_parsed(self) -> None:
        # ``Z``-suffixed ISO strings (common from older code paths)
        # must parse correctly.
        zulu = (self.now - timedelta(days=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        result = _is_distant_memory(
            stored_confidence=0.7,
            created_at=zulu,
            now=self.now,
            horizon_days=365,
            floor=0.3,
            threshold=0.5,
            pinned=False,
        )
        self.assertTrue(result)

    def test_threshold_boundary_default_confidence(self) -> None:
        # default-confidence (0.7) hits threshold (0.5) when
        # multiplier <= 0.7143 -> age_days >= 0.2857 * 365 ~= 104.3
        # 100 days -> still not distant
        self.assertFalse(self._detect(stored=0.7, age_days=100))
        # 110 days -> distant
        self.assertTrue(self._detect(stored=0.7, age_days=110))

    def test_threshold_override_makes_predicate_stricter(self) -> None:
        # Lowering threshold to 0.3 means even decayed rows
        # only fire when effective < 0.3. At stored=0.7 age=180:
        # effective = 0.355, NOT < 0.3.
        self.assertFalse(
            self._detect(stored=0.7, age_days=180, threshold=0.3),
        )
        # At stored=0.7 age=300: 0.7 * 0.178 = 0.125 < 0.3.
        self.assertTrue(
            self._detect(stored=0.7, age_days=300, threshold=0.3),
        )

    def test_threshold_override_loosens_predicate(self) -> None:
        # Raising threshold to 0.7 means even fresh-ish rows can fire.
        # stored=0.7 age=10: 0.7 * (1 - 10/365) = 0.681 < 0.7 -> distant.
        self.assertTrue(
            self._detect(stored=0.7, age_days=10, threshold=0.7),
        )

    def test_horizon_override_speeds_decay(self) -> None:
        # horizon=90 means 60 days is 2/3 of the way to floor.
        # 0.7 * max(0.3, 1 - 60/90) = 0.7 * 0.333 = 0.233 < 0.5
        self.assertTrue(
            self._detect(stored=0.7, age_days=60, horizon=90),
        )


if __name__ == "__main__":
    unittest.main()

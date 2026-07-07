"""Tests for the L18 composite surfacing scorer (``concept_surfacing``).

Covers the pure helpers used to rank the turn-relevant concept fill by a
per-kind blend of context (cosine) + confidence + recency:

* ``recency_boost`` half-life decay + neutral fallback on missing/junk,
* ``composite_score`` normalization, the default-weights == cosine back-compat
  guarantee, and that a recency-heavy weight set reorders a fresher-but-lower
  cosine concept above a stale-but-higher cosine one.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.concepts.concept_kinds import DEFAULT_SURFACE_WEIGHTS, SurfaceWeights
from app.core.concepts.concept_surfacing import composite_score, recency_boost

_UTC = timezone.utc
_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=_UTC)


class RecencyBoostTests(unittest.TestCase):
    def test_fresh_is_one(self) -> None:
        self.assertAlmostEqual(
            recency_boost(_NOW.isoformat(), _NOW, halflife_days=14.0), 1.0
        )

    def test_halflife_decay(self) -> None:
        past = (_NOW - timedelta(days=14.0)).isoformat()
        self.assertAlmostEqual(
            recency_boost(past, _NOW, halflife_days=14.0), 0.5, places=4
        )
        two = (_NOW - timedelta(days=28.0)).isoformat()
        self.assertAlmostEqual(
            recency_boost(two, _NOW, halflife_days=14.0), 0.25, places=4
        )

    def test_missing_or_junk_is_neutral(self) -> None:
        # A missing/unparseable timestamp must never *suppress* a concept.
        self.assertEqual(recency_boost(None, _NOW, 14.0), 1.0)
        self.assertEqual(recency_boost("", _NOW, 14.0), 1.0)
        self.assertEqual(recency_boost("not-a-date", _NOW, 14.0), 1.0)

    def test_nonpositive_halflife_is_neutral(self) -> None:
        past = (_NOW - timedelta(days=100.0)).isoformat()
        self.assertEqual(recency_boost(past, _NOW, 0.0), 1.0)

    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive = (_NOW.replace(tzinfo=None)).isoformat()
        self.assertAlmostEqual(
            recency_boost(naive, _NOW, halflife_days=14.0), 1.0, places=4
        )


class CompositeScoreTests(unittest.TestCase):
    def test_default_weights_equal_cosine(self) -> None:
        # The default (context-only) blend is exactly the pre-L18 ranking.
        for cos in (0.0, 0.3, 0.77, 1.0):
            self.assertAlmostEqual(
                composite_score(
                    cosine=cos, confidence=0.9, recency=0.1,
                    w=DEFAULT_SURFACE_WEIGHTS,
                ),
                cos,
            )

    def test_normalized_to_unit_range(self) -> None:
        w = SurfaceWeights(context=0.5, confidence=0.2, recency=0.3)
        score = composite_score(cosine=1.0, confidence=1.0, recency=1.0, w=w)
        self.assertAlmostEqual(score, 1.0)
        score0 = composite_score(cosine=0.0, confidence=0.0, recency=0.0, w=w)
        self.assertAlmostEqual(score0, 0.0)

    def test_zero_weights_fall_back_to_cosine(self) -> None:
        w = SurfaceWeights(context=0.0, confidence=0.0, recency=0.0)
        self.assertAlmostEqual(
            composite_score(cosine=0.42, confidence=1.0, recency=1.0, w=w),
            0.42,
        )

    def test_recency_heavy_reorders_fresh_above_stale(self) -> None:
        # Boundary-like weights: a fresher, slightly-less-relevant concept
        # should outrank a stale, slightly-more-relevant one.
        w = SurfaceWeights(
            context=0.5, confidence=0.2, recency=0.3, recency_halflife_days=14.0
        )
        fresh = composite_score(
            cosine=0.55, confidence=0.8,
            recency=recency_boost(_NOW.isoformat(), _NOW, 14.0), w=w,
        )
        stale = composite_score(
            cosine=0.62, confidence=0.8,
            recency=recency_boost(
                (_NOW - timedelta(days=60.0)).isoformat(), _NOW, 14.0
            ),
            w=w,
        )
        self.assertGreater(fresh, stale)

    def test_context_only_weights_keep_cosine_order(self) -> None:
        # With the default context-only weights, recency can't flip the order.
        w = DEFAULT_SURFACE_WEIGHTS
        hi = composite_score(cosine=0.62, confidence=0.1, recency=0.1, w=w)
        lo = composite_score(cosine=0.55, confidence=1.0, recency=1.0, w=w)
        self.assertGreater(hi, lo)


if __name__ == "__main__":
    unittest.main()

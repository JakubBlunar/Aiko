"""Tests for the K28 turning-over picker.

Pure-function tests on
:func:`app.core.session.inner_life.turning_over.pick_turning_over`
covering the four gates the picker enforces:

1. **Age window** -- reflections outside ``[min_age_hours,
   max_age_hours]`` are dropped.
2. **Topical-similarity threshold** -- candidates must clear
   ``min_topical_similarity`` against the union of goal vectors
   and recent user-message vectors.
3. **Recency tie-break** -- among surviving candidates, the
   freshest one wins.
4. **Empty / degenerate inputs** -- empty reflection iterable,
   empty pools, missing embeddings, unparseable timestamps all
   return ``None`` without raising.

Render-output tests assert the cue distinguishes ``dream`` vs
``reflection`` framings and strips the ``[dream] `` prefix from
the rendered snippet.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.session.inner_life.turning_over import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MIN_AGE_HOURS,
    DEFAULT_MIN_TOPICAL_SIMILARITY,
    TurningOverResult,
    pick_turning_over,
    render_inner_life_block,
)


# Minimal ``Memory``-shaped stub. The picker only reads ``id``,
# ``content``, ``embedding``, and ``created_at`` -- everything
# else on the real ``Memory`` dataclass is irrelevant.
@dataclass(slots=True)
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    created_at: str


def _vec(*values: float) -> np.ndarray:
    """Build a unit-normalized 1D float32 vector for cosine math."""
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return arr
    return arr / norm


def _iso(now: datetime, *, hours_ago: float) -> str:
    """Build an ISO timestamp ``hours_ago`` before ``now``."""
    return (now - timedelta(hours=hours_ago)).isoformat()


def _now() -> datetime:
    """Fixed deterministic UTC ``now`` for age-arithmetic tests."""
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── Age window ──────────────────────────────────────────────────────────


class AgeWindowTests(unittest.TestCase):
    def test_within_window_passes(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="thought about debugging",
            embedding=vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.memory_id, 1)
        self.assertAlmostEqual(result.age_hours, 30.0, places=2)
        self.assertAlmostEqual(result.topical_score, 1.0, places=2)

    def test_too_young_dropped(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="just minutes old reflection",
            embedding=vec,
            created_at=_iso(now, hours_ago=2.0),
        )
        # Default min is 24h; a 2h-old row is too young.
        self.assertIsNone(
            pick_turning_over(
                reflections=[mem],
                active_goal_vecs=[vec],
                recent_user_vecs=[],
                now=now,
            )
        )

    def test_too_old_dropped(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="ancient reflection",
            embedding=vec,
            created_at=_iso(now, hours_ago=200.0),
        )
        # Default max is 72h; a 200h-old row is too old.
        self.assertIsNone(
            pick_turning_over(
                reflections=[mem],
                active_goal_vecs=[vec],
                recent_user_vecs=[],
                now=now,
            )
        )

    def test_custom_window(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="reflection",
            embedding=vec,
            created_at=_iso(now, hours_ago=4.0),
        )
        # Custom window [2h, 8h] includes this row.
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
            min_age_hours=2.0,
            max_age_hours=8.0,
        )
        self.assertIsNotNone(result)


# ── Topical-similarity gate ─────────────────────────────────────────────


class TopicalSimilarityTests(unittest.TestCase):
    def test_below_threshold_dropped(self) -> None:
        now = _now()
        mem = _StubMemory(
            id=1,
            content="reflection",
            embedding=_vec(1.0, 0.0, 0.0),
            created_at=_iso(now, hours_ago=30.0),
        )
        # Orthogonal vectors → cosine 0.0 → below 0.30 threshold.
        self.assertIsNone(
            pick_turning_over(
                reflections=[mem],
                active_goal_vecs=[_vec(0.0, 1.0, 0.0)],
                recent_user_vecs=[],
                now=now,
            )
        )

    def test_goal_match_passes(self) -> None:
        now = _now()
        ref_vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="goal-aligned reflection",
            embedding=ref_vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[ref_vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.topical_source, "goal")
        self.assertAlmostEqual(result.topical_score, 1.0, places=2)

    def test_thread_match_passes(self) -> None:
        now = _now()
        ref_vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="thread-aligned reflection",
            embedding=ref_vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[],
            recent_user_vecs=[ref_vec],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.topical_source, "thread")

    def test_threshold_zero_accepts_everything(self) -> None:
        now = _now()
        mem = _StubMemory(
            id=1,
            content="orthogonal reflection",
            embedding=_vec(1.0, 0.0, 0.0),
            created_at=_iso(now, hours_ago=30.0),
        )
        # threshold 0.0 means even zero-cosine candidates pass --
        # but only if the pool is non-empty. Empty pool would still
        # produce a 0.0 score and pass at threshold 0.0 too, so we
        # test that the candidate is returned regardless.
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[_vec(0.0, 1.0, 0.0)],
            recent_user_vecs=[],
            now=now,
            min_topical_similarity=0.0,
        )
        self.assertIsNotNone(result)
        assert result is not None
        # Best cosine was 0 (orthogonal) so topical_source stays "".
        self.assertEqual(result.topical_source, "")

    def test_max_of_two_pools(self) -> None:
        now = _now()
        ref_vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="reflection",
            embedding=ref_vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        # Goal pool has a 0.3 match, thread pool has a 0.9 match.
        # The picker uses max() → 0.9 → topical_source=thread.
        goal_partial = _vec(0.3, 0.95, 0.0)
        thread_strong = _vec(0.9, 0.44, 0.0)
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[goal_partial],
            recent_user_vecs=[thread_strong],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        # Thread side won.
        self.assertEqual(result.topical_source, "thread")
        # Score should be roughly the cosine vs thread_strong.
        self.assertGreater(result.topical_score, 0.85)


# ── Recency tie-break ───────────────────────────────────────────────────


class RecencyTieBreakTests(unittest.TestCase):
    def test_youngest_wins(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        old = _StubMemory(
            id=1,
            content="older",
            embedding=vec,
            created_at=_iso(now, hours_ago=60.0),
        )
        young = _StubMemory(
            id=2,
            content="younger",
            embedding=vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[old, young],
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        # Younger row (id=2) wins on recency.
        self.assertEqual(result.memory_id, 2)

    def test_iteration_order_does_not_matter(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        old = _StubMemory(
            id=1,
            content="older",
            embedding=vec,
            created_at=_iso(now, hours_ago=60.0),
        )
        young = _StubMemory(
            id=2,
            content="younger",
            embedding=vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        # Insert order [young, old] should pick the same row.
        result = pick_turning_over(
            reflections=[young, old],
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.memory_id, 2)

    def test_equal_ages_higher_score_wins(self) -> None:
        now = _now()
        ts = _iso(now, hours_ago=30.0)
        weak = _StubMemory(
            id=1,
            content="weak match",
            embedding=_vec(0.5, 0.866, 0.0),
            created_at=ts,
        )
        strong = _StubMemory(
            id=2,
            content="strong match",
            embedding=_vec(1.0, 0.0, 0.0),
            created_at=ts,
        )
        result = pick_turning_over(
            reflections=[weak, strong],
            active_goal_vecs=[_vec(1.0, 0.0, 0.0)],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        # Strong-cosine row wins the tie-break.
        self.assertEqual(result.memory_id, 2)


# ── Empty / degenerate inputs ──────────────────────────────────────────


class EmptyInputsTests(unittest.TestCase):
    def test_no_reflections_returns_none(self) -> None:
        self.assertIsNone(
            pick_turning_over(
                reflections=[],
                active_goal_vecs=[_vec(1.0, 0.0, 0.0)],
                recent_user_vecs=[],
                now=_now(),
            )
        )

    def test_both_pools_empty_with_default_threshold_returns_none(self) -> None:
        now = _now()
        mem = _StubMemory(
            id=1,
            content="reflection",
            embedding=_vec(1.0, 0.0, 0.0),
            created_at=_iso(now, hours_ago=30.0),
        )
        # No pools → 0.0 topical score → below default 0.30 threshold.
        self.assertIsNone(
            pick_turning_over(
                reflections=[mem],
                active_goal_vecs=[],
                recent_user_vecs=[],
                now=now,
            )
        )

    def test_missing_embedding_skipped(self) -> None:
        now = _now()
        good = _StubMemory(
            id=2,
            content="good",
            embedding=_vec(1.0, 0.0, 0.0),
            created_at=_iso(now, hours_ago=30.0),
        )
        bad = _StubMemory(
            id=1,
            content="missing embedding",
            embedding=None,  # type: ignore[arg-type]
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[bad, good],
            active_goal_vecs=[_vec(1.0, 0.0, 0.0)],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.memory_id, 2)

    def test_unparseable_timestamp_skipped(self) -> None:
        now = _now()
        bad = _StubMemory(
            id=1,
            content="bad ts",
            embedding=_vec(1.0, 0.0, 0.0),
            created_at="not-a-date",
        )
        self.assertIsNone(
            pick_turning_over(
                reflections=[bad],
                active_goal_vecs=[_vec(1.0, 0.0, 0.0)],
                recent_user_vecs=[],
                now=now,
            )
        )

    def test_none_in_reflections_iterable_skipped(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="real reflection",
            embedding=vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        # Defensive: a ``None`` slot in the iterable shouldn't crash.
        result = pick_turning_over(
            reflections=[None, mem],  # type: ignore[list-item]
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)


# ── Dream wording ──────────────────────────────────────────────────────


class DreamVariantTests(unittest.TestCase):
    def test_dream_prefix_flagged(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="[dream] I was lost in a basil greenhouse",
            embedding=vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.dream)

    def test_no_prefix_not_flagged(self) -> None:
        now = _now()
        vec = _vec(1.0, 0.0, 0.0)
        mem = _StubMemory(
            id=1,
            content="Jacob's interview prep felt heavier than last week.",
            embedding=vec,
            created_at=_iso(now, hours_ago=30.0),
        )
        result = pick_turning_over(
            reflections=[mem],
            active_goal_vecs=[vec],
            recent_user_vecs=[],
            now=now,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.dream)

    def test_render_dream_uses_dream_framing(self) -> None:
        result = TurningOverResult(
            memory_id=1,
            content="[dream] I was lost in a basil greenhouse",
            dream=True,
            topical_score=0.8,
            age_hours=30.0,
            topical_source="thread",
        )
        rendered = render_inner_life_block(result, user_display_name="Jacob")
        # Dream-flavour framing.
        self.assertIn("dreamed about", rendered)
        # Anti-announcement discipline lives in the body.
        self.assertIn("casual aside", rendered)
        # Stripped the prefix.
        self.assertNotIn("[dream]", rendered)
        # Stance text rendered inline.
        self.assertIn("basil greenhouse", rendered)

    def test_render_reflection_uses_waking_framing(self) -> None:
        result = TurningOverResult(
            memory_id=2,
            content="Jacob's interview prep felt heavier than last week.",
            dream=False,
            topical_score=0.8,
            age_hours=30.0,
            topical_source="thread",
        )
        rendered = render_inner_life_block(result, user_display_name="Jacob")
        # Waking-thought framing.
        self.assertIn("thinking about", rendered)
        self.assertNotIn("dreamed about", rendered)
        # Stance text rendered inline.
        self.assertIn("interview prep", rendered)

    def test_render_trims_long_content(self) -> None:
        long_text = "x" * 400
        result = TurningOverResult(
            memory_id=1,
            content=long_text,
            dream=False,
            topical_score=0.8,
            age_hours=30.0,
            topical_source="thread",
        )
        rendered = render_inner_life_block(result, user_display_name="Jacob")
        # Trimmed to ~200 chars + ellipsis.
        self.assertLess(len(rendered), 700)
        self.assertIn("\u2026", rendered)


# ── Defaults sanity ────────────────────────────────────────────────────


class DefaultsSanityTests(unittest.TestCase):
    def test_default_thresholds_are_sane(self) -> None:
        self.assertEqual(DEFAULT_MIN_AGE_HOURS, 24.0)
        self.assertEqual(DEFAULT_MAX_AGE_HOURS, 72.0)
        self.assertAlmostEqual(DEFAULT_MIN_TOPICAL_SIMILARITY, 0.30, places=2)


if __name__ == "__main__":
    unittest.main()

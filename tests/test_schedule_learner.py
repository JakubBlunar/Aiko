"""Tests for the G2 schedule learner.

The worker is deliberately small (no LLM, no embedder), so we can
unit-test the bucket math directly *and* exercise the IdleWorker
contract end-to-end against a real :class:`ChatDatabase` +
:class:`UserProfileStore`.

Coverage targets:

* Bucket classification — local-tz aware, weekday/weekend, four
  hour bands.
* ``_summarize_buckets`` — dominant cluster picking, share floor,
  empty-result behaviour.
* End-to-end ``run()`` — writes ``usual_hours`` for a populated DB,
  short-circuits when below ``min_samples``, idempotent on a no-op
  pass.
* ``is_ready`` — honours the disable flag, the interval, and a
  fresh worker.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.schedule_learner import (
    _RITUAL_LABELS,
    ScheduleLearner,
    _classify_local,
    _confidence_from_samples,
    _summarize_buckets,
    _summarize_routines,
)
from app.core.infra.user_profile import UserProfileStore


@dataclass
class _StubAgent:
    schedule_learner_enabled: bool = True
    schedule_learner_min_samples: int = 5
    schedule_learner_window_days: int = 30
    # K3 knobs — defaults match config/default.json so the existing
    # G2 tests behave identically when the new pass runs.
    routine_detection_enabled: bool = True


@dataclass
class _StubMemory:
    schedule_learner_interval_seconds: int = 86400
    routine_min_touches: int = 3
    routine_min_share: float = 0.30
    routine_max_active: int = 5


def _insert_user_message(
    chat_db: ChatDatabase, *, when: datetime, session_id: str = "s",
) -> None:
    """Insert a single ``role='user'`` row at ``when`` (UTC)."""
    chat_db.execute_commit(
        "INSERT INTO messages (session_id, role, content, token_count, "
        "created_at) VALUES (?, 'user', '.', 1, ?)",
        (session_id, when.astimezone(timezone.utc).isoformat()),
    )


def _build_world(
    *,
    enabled: bool = True,
    min_samples: int = 5,
    window_days: int = 30,
    interval_seconds: int = 86400,
    user_id: str = "default",
    routine_detection_enabled: bool = True,
    routine_min_touches: int = 3,
    routine_min_share: float = 0.30,
    routine_max_active: int = 5,
) -> dict[str, Any]:
    d = tempfile.mkdtemp()
    path = Path(d) / "chat.db"
    chat_db = ChatDatabase(path)
    profile_store = UserProfileStore(chat_db)
    agent = _StubAgent(
        schedule_learner_enabled=enabled,
        schedule_learner_min_samples=min_samples,
        schedule_learner_window_days=window_days,
        routine_detection_enabled=routine_detection_enabled,
    )
    memory = _StubMemory(
        schedule_learner_interval_seconds=interval_seconds,
        routine_min_touches=routine_min_touches,
        routine_min_share=routine_min_share,
        routine_max_active=routine_max_active,
    )
    worker = ScheduleLearner(
        chat_db=chat_db,
        profile_store=profile_store,
        user_id_provider=lambda: user_id,
        agent_settings=agent,
        memory_settings=memory,
    )
    return {
        "chat_db": chat_db,
        "profile_store": profile_store,
        "worker": worker,
        "user_id": user_id,
        "path": path,
    }


# ── pure helpers ──────────────────────────────────────────────────────


class TestBucketClassification(unittest.TestCase):
    """Local-time bucket math is the worker's only nontrivial logic."""

    def test_weekday_evening(self) -> None:
        # 2026-05-26 is a Tuesday.
        when = datetime(2026, 5, 26, 19, 30, tzinfo=timezone.utc).astimezone()
        self.assertEqual(_classify_local(when), ("weekday", "evening"))

    def test_weekday_morning(self) -> None:
        when = datetime(2026, 5, 26, 8, 0, tzinfo=timezone.utc).astimezone()
        self.assertEqual(_classify_local(when), ("weekday", "morning"))

    def test_weekday_late_2am(self) -> None:
        when = datetime(2026, 5, 26, 2, 30, tzinfo=timezone.utc).astimezone()
        self.assertEqual(_classify_local(when), ("weekday", "late"))

    def test_weekday_late_23(self) -> None:
        when = datetime(2026, 5, 26, 23, 30, tzinfo=timezone.utc).astimezone()
        self.assertEqual(_classify_local(when), ("weekday", "late"))

    def test_weekend_afternoon_saturday(self) -> None:
        # 2026-05-30 is a Saturday.
        when = datetime(2026, 5, 30, 14, 0, tzinfo=timezone.utc).astimezone()
        self.assertEqual(_classify_local(when), ("weekend", "afternoon"))

    def test_weekend_morning_sunday(self) -> None:
        # 2026-05-31 is a Sunday.
        when = datetime(2026, 5, 31, 9, 30, tzinfo=timezone.utc).astimezone()
        self.assertEqual(_classify_local(when), ("weekend", "morning"))


class TestSummarizeBuckets(unittest.TestCase):
    def test_picks_top_two_above_share_floor(self) -> None:
        counts = {
            ("weekday", "evening"): 12,
            ("weekday", "morning"): 4,
            ("weekend", "afternoon"): 6,
            ("weekday", "late"): 1,
        }
        rendered, top = _summarize_buckets(counts, total=23)
        self.assertIn("weekday evenings", rendered)
        self.assertIn("weekend afternoons", rendered)
        # Morning has 4/23 ~ 17% which is below the 20% floor; should
        # not appear in either rendered string or top list slice.
        self.assertNotIn("weekday mornings", rendered)
        # Top list keeps everything sorted by count desc, even
        # below-share ones — useful for diagnostics.
        kinds = [(d, b) for d, b, _c, _s in top]
        self.assertEqual(kinds[0], ("weekday", "evening"))

    def test_empty_when_no_dominant_cluster(self) -> None:
        # Eight equal-share buckets -> 12.5% each -> all below 20%.
        counts = {
            ("weekday", "morning"): 1,
            ("weekday", "afternoon"): 1,
            ("weekday", "evening"): 1,
            ("weekday", "late"): 1,
            ("weekend", "morning"): 1,
            ("weekend", "afternoon"): 1,
            ("weekend", "evening"): 1,
            ("weekend", "late"): 1,
        }
        rendered, _top = _summarize_buckets(counts, total=8)
        self.assertEqual(rendered, "")

    def test_empty_when_total_is_zero(self) -> None:
        rendered, top = _summarize_buckets({}, total=0)
        self.assertEqual(rendered, "")
        self.assertEqual(top, [])


class TestConfidenceFromSamples(unittest.TestCase):
    def test_floor_zero(self) -> None:
        self.assertEqual(_confidence_from_samples(0), 0.0)

    def test_caps_at_0_95(self) -> None:
        self.assertAlmostEqual(_confidence_from_samples(500), 0.95, places=4)

    def test_linear_below_cap(self) -> None:
        self.assertAlmostEqual(_confidence_from_samples(25), 0.5, places=4)


# ── end-to-end ────────────────────────────────────────────────────────


class TestRun(unittest.TestCase):
    def test_run_below_min_samples_does_not_write(self) -> None:
        world = _build_world(min_samples=5)
        # Insert only 2 user messages.
        now = datetime.now(timezone.utc)
        for delta_h in (1, 2):
            _insert_user_message(
                world["chat_db"], when=now - timedelta(hours=delta_h),
            )
        result = world["worker"].run()
        self.assertEqual(result["wrote"], False)
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["reason"], "below_min_samples")
        self.assertNotIn(
            "usual_hours",
            world["profile_store"].fields(world["user_id"]),
        )

    def test_run_writes_usual_hours_when_dominant_cluster(self) -> None:
        world = _build_world(min_samples=3)
        # 8 messages on different weekday evenings (UTC 19:00). In
        # most non-Pacific timezones this lands in the evening bucket,
        # but the test only asserts that a string was written, not
        # which bucket — that depends on the test runner's local TZ.
        # 2026-05-26 is a Tuesday.
        base = datetime(2026, 5, 26, 19, 0, tzinfo=timezone.utc)
        for d in range(8):
            _insert_user_message(
                world["chat_db"],
                when=base - timedelta(days=d),
            )
        result = world["worker"].run()
        self.assertEqual(result["wrote"], True)
        self.assertGreaterEqual(result["samples"], 3)
        self.assertIn("value", result)
        stored = world["profile_store"].fields(world["user_id"]).get(
            "usual_hours",
        )
        self.assertIsNotNone(stored)
        assert stored is not None  # mypy
        self.assertEqual(stored.value, result["value"])

    def test_run_idempotent_when_value_unchanged(self) -> None:
        world = _build_world(min_samples=3)
        base = datetime(2026, 5, 26, 19, 0, tzinfo=timezone.utc)
        for d in range(10):
            _insert_user_message(
                world["chat_db"], when=base - timedelta(days=d),
            )
        first = world["worker"].run()
        self.assertTrue(first["wrote"])
        second = world["worker"].run()
        self.assertEqual(second["wrote"], False)
        self.assertEqual(second["reason"], "unchanged")
        self.assertEqual(second["value"], first["value"])

    def test_run_skips_messages_outside_window(self) -> None:
        world = _build_world(min_samples=3, window_days=7)
        # 5 messages 30 days ago — outside the 7-day window — should
        # not contribute to the bucket count.
        ancient = datetime.now(timezone.utc) - timedelta(days=30)
        for d in range(5):
            _insert_user_message(
                world["chat_db"], when=ancient - timedelta(hours=d),
            )
        result = world["worker"].run()
        self.assertEqual(result["samples"], 0)
        self.assertEqual(result["wrote"], False)


class TestSummarizeRoutines(unittest.TestCase):
    """Pure-function tests on the K3 detector. Synthetic
    ``weekly_seen`` dicts let us pin the math without depending on
    the test runner's local timezone."""

    def test_picks_recurring_slot_above_thresholds(self) -> None:
        # Sunday-morning lit up in 4 of 5 weeks; clearly a routine.
        weekly_seen = {
            ("sunday", "morning"): {
                (2026, 18), (2026, 19), (2026, 20), (2026, 21),
            },
        }
        rendered, top = _summarize_routines(
            weekly_seen,
            total_weeks=5,
            min_touches=3,
            min_share=0.30,
            max_active=5,
        )
        self.assertEqual(rendered, "Sunday-morning chats")
        self.assertEqual(len(top), 1)
        weekday, bucket, weeks_seen, share = top[0]
        self.assertEqual((weekday, bucket), ("sunday", "morning"))
        self.assertEqual(weeks_seen, 4)
        self.assertAlmostEqual(share, 0.8, places=4)

    def test_skips_slot_with_too_few_distinct_weeks(self) -> None:
        # Two weeks isn't enough — could be a coincidence, not a
        # routine. The min_share check would otherwise pass (2/5=0.40).
        weekly_seen = {
            ("sunday", "morning"): {(2026, 19), (2026, 20)},
        }
        rendered, top = _summarize_routines(
            weekly_seen,
            total_weeks=5,
            min_touches=3,
            min_share=0.30,
            max_active=5,
        )
        self.assertEqual(rendered, "")
        self.assertEqual(top, [])

    def test_skips_slot_below_min_share(self) -> None:
        # Three weeks lit up, but a 20-week window means share=0.15
        # which is below the 0.30 floor — long-window noise floor.
        weekly_seen = {
            ("friday", "evening"): {(2026, 1), (2026, 2), (2026, 3)},
        }
        rendered, _top = _summarize_routines(
            weekly_seen,
            total_weeks=20,
            min_touches=3,
            min_share=0.30,
            max_active=5,
        )
        self.assertEqual(rendered, "")

    def test_caps_at_max_active(self) -> None:
        # Four qualifying cells, cap=2 — only the densest two land.
        weekly_seen = {
            ("monday", "morning"): {(2026, 18), (2026, 19), (2026, 20)},
            ("tuesday", "evening"): {
                (2026, 18), (2026, 19), (2026, 20), (2026, 21),
            },
            ("friday", "evening"): {
                (2026, 18), (2026, 19), (2026, 20),
            },
            ("sunday", "morning"): {
                (2026, 18), (2026, 19), (2026, 20),
                (2026, 21), (2026, 22),
            },
        }
        rendered, top = _summarize_routines(
            weekly_seen,
            total_weeks=5,
            min_touches=3,
            min_share=0.30,
            max_active=2,
        )
        # ``top`` returns the capped slice (top-N by recurrence
        # density), not the full qualifying set.
        self.assertEqual(len(top), 2)
        chosen_keys = [(weekday, bucket) for weekday, bucket, _w, _s in top]
        self.assertEqual(
            chosen_keys,
            [("sunday", "morning"), ("tuesday", "evening")],
        )
        # Rendered phrase honours the cap and the sort order.
        self.assertEqual(
            rendered,
            "Sunday-morning chats, Tuesday-evening unwinds",
        )

    def test_empty_weekly_seen(self) -> None:
        rendered, top = _summarize_routines(
            {}, total_weeks=5,
            min_touches=3, min_share=0.30, max_active=5,
        )
        self.assertEqual(rendered, "")
        self.assertEqual(top, [])

    def test_zero_total_weeks_is_safe(self) -> None:
        # Defensive: callers should pass total_weeks >= 1, but we
        # short-circuit cleanly if they don't.
        rendered, top = _summarize_routines(
            {("sunday", "morning"): {(2026, 19)}},
            total_weeks=0,
            min_touches=1,
            min_share=0.0,
            max_active=5,
        )
        self.assertEqual(rendered, "")
        self.assertEqual(top, [])

    def test_deterministic_naming_via_label_dict(self) -> None:
        # Every label in _RITUAL_LABELS should produce a non-empty
        # rendered string when its slot qualifies — locks down full
        # 28-cell coverage.
        for (weekday, bucket), label in _RITUAL_LABELS.items():
            weekly_seen = {
                (weekday, bucket): {
                    (2026, 18), (2026, 19), (2026, 20), (2026, 21),
                },
            }
            rendered, _top = _summarize_routines(
                weekly_seen,
                total_weeks=5,
                min_touches=3,
                min_share=0.30,
                max_active=5,
            )
            self.assertEqual(rendered, label)


class TestRunRoutines(unittest.TestCase):
    """End-to-end coverage: routines flow through ``ScheduleLearner.run``
    and land in the ``UserProfileStore``."""

    def test_run_writes_routines_when_recurrence_qualifies(self) -> None:
        world = _build_world(
            min_samples=3,
            window_days=30,
            routine_min_touches=3,
            routine_min_share=0.30,
        )
        # Build local-tz-aware Sunday timestamps across 4 different
        # ISO weeks so the bucket math is unambiguous regardless of
        # the test runner's timezone. We anchor to a "now" that's
        # comfortably inside our 30-day window.
        now_local = datetime.now().astimezone()
        # Find the most recent Sunday at 09:00 local time.
        days_since_sunday = (now_local.weekday() + 1) % 7
        sunday_anchor = now_local.replace(
            hour=9, minute=0, second=0, microsecond=0,
        ) - timedelta(days=days_since_sunday)
        for week in range(4):
            when_local = sunday_anchor - timedelta(weeks=week)
            _insert_user_message(
                world["chat_db"],
                when=when_local.astimezone(timezone.utc),
            )
        result = world["worker"].run()
        self.assertIn("routines_value", result)
        self.assertEqual(result.get("routines_wrote"), True)
        # The label space at 09:00 local time is "morning"; on a
        # Sunday that maps to a known fixture string.
        self.assertEqual(result["routines_value"], "Sunday-morning chats")
        stored = world["profile_store"].fields(world["user_id"]).get(
            "routines",
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.value, "Sunday-morning chats")

    def test_run_does_not_write_routines_when_below_threshold(self) -> None:
        world = _build_world(
            min_samples=3,
            window_days=30,
            routine_min_touches=3,
        )
        # Only two distinct ISO weeks → recurrence floor not cleared,
        # but the eight messages comfortably clear ``min_samples`` so
        # G2 runs normally.
        now_local = datetime.now().astimezone()
        days_since_sunday = (now_local.weekday() + 1) % 7
        sunday_anchor = now_local.replace(
            hour=9, minute=0, second=0, microsecond=0,
        ) - timedelta(days=days_since_sunday)
        for week in range(2):
            for offset_h in range(4):
                when_local = (
                    sunday_anchor
                    - timedelta(weeks=week, hours=offset_h)
                )
                _insert_user_message(
                    world["chat_db"],
                    when=when_local.astimezone(timezone.utc),
                )
        result = world["worker"].run()
        # G2 may or may not write usual_hours depending on the local
        # bucket distribution; what we assert is that routines stay
        # empty — recurrence is too thin.
        self.assertNotIn(
            "routines",
            world["profile_store"].fields(world["user_id"]),
        )
        self.assertNotIn("routines_wrote", result)

    def test_run_routines_idempotent_when_unchanged(self) -> None:
        world = _build_world(
            min_samples=3,
            window_days=30,
            routine_min_touches=3,
        )
        now_local = datetime.now().astimezone()
        days_since_sunday = (now_local.weekday() + 1) % 7
        sunday_anchor = now_local.replace(
            hour=9, minute=0, second=0, microsecond=0,
        ) - timedelta(days=days_since_sunday)
        for week in range(4):
            when_local = sunday_anchor - timedelta(weeks=week)
            _insert_user_message(
                world["chat_db"],
                when=when_local.astimezone(timezone.utc),
            )
        first = world["worker"].run()
        self.assertEqual(first.get("routines_wrote"), True)
        second = world["worker"].run()
        # ``routines_wrote`` is absent on the no-op pass; instead we
        # see the "unchanged" reason and the value is preserved.
        self.assertEqual(second.get("routines_reason"), "unchanged")
        self.assertEqual(
            second.get("routines_value"), first["routines_value"],
        )
        self.assertNotIn("routines_wrote", second)

    def test_run_skips_routines_when_disabled(self) -> None:
        world = _build_world(
            min_samples=3,
            window_days=30,
            routine_min_touches=3,
            routine_detection_enabled=False,
        )
        now_local = datetime.now().astimezone()
        days_since_sunday = (now_local.weekday() + 1) % 7
        sunday_anchor = now_local.replace(
            hour=9, minute=0, second=0, microsecond=0,
        ) - timedelta(days=days_since_sunday)
        for week in range(4):
            when_local = sunday_anchor - timedelta(weeks=week)
            _insert_user_message(
                world["chat_db"],
                when=when_local.astimezone(timezone.utc),
            )
        result = world["worker"].run()
        # Disable flag short-circuits the K3 pass entirely; G2 may
        # still run depending on the local bucket distribution.
        self.assertNotIn("routines_value", result)
        self.assertNotIn(
            "routines",
            world["profile_store"].fields(world["user_id"]),
        )


class TestIsReady(unittest.TestCase):
    def test_disabled_is_not_ready(self) -> None:
        world = _build_world(enabled=False)
        ready = world["worker"].is_ready(
            now=datetime.now(timezone.utc),
            last_run_at=None,
        )
        self.assertFalse(ready)

    def test_first_run_is_ready(self) -> None:
        world = _build_world()
        ready = world["worker"].is_ready(
            now=datetime.now(timezone.utc),
            last_run_at=None,
        )
        self.assertTrue(ready)

    def test_within_interval_not_ready(self) -> None:
        world = _build_world(interval_seconds=86400)
        now = datetime.now(timezone.utc)
        last = now - timedelta(hours=1)
        ready = world["worker"].is_ready(now=now, last_run_at=last)
        self.assertFalse(ready)

    def test_past_interval_is_ready(self) -> None:
        world = _build_world(interval_seconds=3600)
        now = datetime.now(timezone.utc)
        last = now - timedelta(hours=2)
        ready = world["worker"].is_ready(now=now, last_run_at=last)
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()

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

from app.core.chat_database import ChatDatabase
from app.core.schedule_learner import (
    ScheduleLearner,
    _classify_local,
    _confidence_from_samples,
    _summarize_buckets,
)
from app.core.user_profile import UserProfileStore


@dataclass
class _StubAgent:
    schedule_learner_enabled: bool = True
    schedule_learner_min_samples: int = 5
    schedule_learner_window_days: int = 30


@dataclass
class _StubMemory:
    schedule_learner_interval_seconds: int = 86400


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
) -> dict[str, Any]:
    d = tempfile.mkdtemp()
    path = Path(d) / "chat.db"
    chat_db = ChatDatabase(path)
    profile_store = UserProfileStore(chat_db)
    agent = _StubAgent(
        schedule_learner_enabled=enabled,
        schedule_learner_min_samples=min_samples,
        schedule_learner_window_days=window_days,
    )
    memory = _StubMemory(
        schedule_learner_interval_seconds=interval_seconds,
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

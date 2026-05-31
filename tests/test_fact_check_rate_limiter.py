"""Tests for :class:`FactCheckRateLimiter` (algorithm + state_key).

The limiter doubles as the budget tracker for the F1 fact-checker
and the G3 idle curiosity worker. Each worker passes its own
``state_key=`` so the two budgets persist to distinct ``kv_meta``
rows; this file proves they really are independent.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter


class StateKeyIndependenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self.db = ChatDatabase(Path(self._dir) / "chat.db")

    def test_default_state_key(self) -> None:
        limiter = FactCheckRateLimiter(
            self.db, per_hour_cap=2, per_day_cap=4,
        )
        self.assertEqual(limiter.state_key, "fact_checker.rate_state")

    def test_two_limiters_with_different_keys_dont_share_counters(
        self,
    ) -> None:
        # Use the F1 default for one, an idle-curiosity key for the
        # other — same shape as session_controller wires today.
        f1 = FactCheckRateLimiter(
            self.db,
            per_hour_cap=1,
            per_day_cap=10,
            state_key="fact_checker.rate_state",
        )
        g3 = FactCheckRateLimiter(
            self.db,
            per_hour_cap=1,
            per_day_cap=10,
            state_key="idle_curiosity.rate_state",
        )

        now = datetime.now(timezone.utc)
        # Burn the F1 hour budget.
        self.assertTrue(f1.allow(now))
        self.assertFalse(f1.allow(now))

        # G3 must still have its full hour budget.
        self.assertEqual(g3.snapshot(now)["hour_used"], 0)
        self.assertTrue(g3.allow(now))

        # F1 still rate-limited; G3 now also at its hourly cap, but
        # they tripped that cap on independent counters.
        self.assertFalse(f1.allow(now))
        self.assertFalse(g3.allow(now))

    def test_two_limiters_persist_to_distinct_kv_rows(self) -> None:
        f1 = FactCheckRateLimiter(
            self.db,
            per_hour_cap=5,
            per_day_cap=20,
            state_key="fact_checker.rate_state",
        )
        g3 = FactCheckRateLimiter(
            self.db,
            per_hour_cap=5,
            per_day_cap=20,
            state_key="idle_curiosity.rate_state",
        )
        now = datetime.now(timezone.utc)
        for _ in range(3):
            f1.allow(now)
        for _ in range(1):
            g3.allow(now)

        self.assertEqual(f1.snapshot(now)["hour_used"], 3)
        self.assertEqual(g3.snapshot(now)["hour_used"], 1)

        # Confirm the two rows actually exist in kv_meta with the
        # state values we expect.
        f1_raw = self.db.kv_get("fact_checker.rate_state")
        g3_raw = self.db.kv_get("idle_curiosity.rate_state")
        self.assertIsNotNone(f1_raw)
        self.assertIsNotNone(g3_raw)
        self.assertNotEqual(f1_raw, g3_raw)


class HourBucketRolloverTests(unittest.TestCase):
    """Sanity check: an hour boundary rollover resets the hour
    counter while leaving the day counter intact. The pre-existing
    F1 tests cover the algorithm too, but moving the file here gives
    us a stable home for limiter tests now that two workers depend
    on it."""

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self.db = ChatDatabase(Path(self._dir) / "chat.db")
        self.limiter = FactCheckRateLimiter(
            self.db, per_hour_cap=2, per_day_cap=10,
        )

    def test_hour_rollover_resets_hour_only(self) -> None:
        t0 = datetime(2026, 5, 28, 10, 30, tzinfo=timezone.utc)
        self.assertTrue(self.limiter.allow(t0))
        self.assertTrue(self.limiter.allow(t0))
        self.assertFalse(self.limiter.allow(t0))

        # +1h crosses the hour bucket boundary.
        t1 = t0 + timedelta(hours=1)
        snapshot_after = self.limiter.snapshot(t1)
        self.assertEqual(snapshot_after["hour_used"], 0)
        self.assertEqual(snapshot_after["day_used"], 2)
        self.assertTrue(self.limiter.allow(t1))


if __name__ == "__main__":
    unittest.main()

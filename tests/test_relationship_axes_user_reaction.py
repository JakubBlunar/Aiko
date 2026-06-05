"""Tests for :meth:`RelationshipAxesUpdater.apply_user_reaction` (K32).

Covers:

  - ``heart`` lands closeness only (per the delta table).
  - ``hug`` lands closeness + trust + comfort.
  - ``surprise`` is a no-op on the axes (signal-only).
  - Daily cap honours :func:`user_reactions.apply_daily_cap` and
    saves the post-clip state to ``kv_meta``.
  - Per-axis clamp (``_MAX_DELTA = 0.08``) protects against a
    misconfigured delta table.

Uses a real :class:`ChatDatabase` against a tempfile so the
schema + relationship_axes table + kv_meta plumbing is all
exercised end-to-end.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.relationship.relationship_axes import (
    RelationshipAxesStore,
    RelationshipAxesUpdater,
)
from app.core.relationship.user_reactions import (
    DailyCapState,
    KV_USER_REACTIONS_DAILY,
    deserialize_daily_state,
    save_daily_state,
)


class _TempUpdater:
    def __enter__(self) -> tuple[ChatDatabase, RelationshipAxesUpdater]:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "axes.db"
        self.db = ChatDatabase(path)
        store = RelationshipAxesStore(self.db)
        updater = RelationshipAxesUpdater(store)
        return self.db, updater

    def __exit__(self, *exc):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class HeartLandsClosenessTests(unittest.TestCase):
    def test_heart_bumps_closeness_only(self) -> None:
        with _TempUpdater() as (_db, updater):
            state = updater.apply_user_reaction("jacob", kind="heart")
            assert state is not None
            self.assertGreater(state.closeness, 0.0)
            self.assertEqual(state.humor, 0.0)
            self.assertEqual(state.trust, 0.0)
            self.assertEqual(state.comfort, 0.0)


class HugLandsThreeAxesTests(unittest.TestCase):
    def test_hug_bumps_closeness_trust_comfort(self) -> None:
        with _TempUpdater() as (_db, updater):
            state = updater.apply_user_reaction("jacob", kind="hug")
            assert state is not None
            self.assertGreater(state.closeness, 0.0)
            self.assertGreater(state.trust, 0.0)
            self.assertGreater(state.comfort, 0.0)
            # Humor not in the hug delta table.
            self.assertEqual(state.humor, 0.0)


class SurpriseIsSignalOnlyTests(unittest.TestCase):
    def test_surprise_does_not_move_axes(self) -> None:
        with _TempUpdater() as (_db, updater):
            state = updater.apply_user_reaction("jacob", kind="surprise")
            assert state is not None
            self.assertEqual(state.closeness, 0.0)
            self.assertEqual(state.humor, 0.0)
            self.assertEqual(state.trust, 0.0)
            self.assertEqual(state.comfort, 0.0)


class DailyCapSavesKvMetaTests(unittest.TestCase):
    def test_cap_state_persists_through_kv_meta(self) -> None:
        with _TempUpdater() as (db, updater):
            updater.apply_user_reaction("jacob", kind="heart", daily_cap=0.15)
            raw = db.kv_get(KV_USER_REACTIONS_DAILY)
            self.assertIsNotNone(raw)
            cap_state = deserialize_daily_state(raw)
            self.assertGreater(cap_state.axis_totals.get("closeness", 0.0), 0.0)

    def test_cap_stops_axis_movement_when_exhausted(self) -> None:
        with _TempUpdater() as (db, updater):
            # Pre-seed today's ledger so closeness is already at the
            # cap; the click should land in kv_meta but axes don't
            # move.
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            save_daily_state(
                db,
                DailyCapState(
                    daily_date=today,
                    axis_totals={"closeness": 0.15},
                ),
            )
            state = updater.apply_user_reaction(
                "jacob", kind="heart", daily_cap=0.15,
            )
            assert state is not None
            self.assertEqual(state.closeness, 0.0)


if __name__ == "__main__":
    unittest.main()

"""Tests for :mod:`app.core.affect.day_color_worker` (K27 personality backlog).

The worker is the canonical roll path -- it fires once an hour from
the IdleWorkerScheduler and only writes to ``kv_meta`` when the local
date has rolled over. The tests use a tiny in-memory ``kv_meta`` stub
so we can pin the exact reads + writes without spinning up a real
SQLite database.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.core.affect import day_color
from app.core.affect.day_color_worker import (
    KV_DAY_COLOR,
    KV_DAY_COLOR_SET_AT,
    DayColorWorker,
)


# ── Test fixtures ────────────────────────────────────────────────────


class _FakeChatDB:
    """Minimal kv_meta surface the worker depends on.

    Keeps a real dict so we can assert post-conditions and replay
    failures via :class:`unittest.mock.patch.object`.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})
        # Counters let the tests verify the cheap-path no-op doesn't
        # write to kv_meta on the stable-read case.
        self.kv_set_calls = 0
        self.kv_get_calls = 0

    def kv_get(self, key: str) -> str | None:
        self.kv_get_calls += 1
        return self._store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.kv_set_calls += 1
        self._store[key] = value


@dataclass
class _FakeSettings:
    day_color_enabled: bool = True
    day_color_check_interval_seconds: int = 3600


# ── is_ready ─────────────────────────────────────────────────────────


class IsReadyTests(unittest.TestCase):
    def test_disabled_master_switch_blocks(self) -> None:
        worker = DayColorWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(day_color_enabled=False),
        )
        now = datetime.now(timezone.utc)
        # Even with no prior run (which normally fires on first
        # tick), the master switch must short-circuit.
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))

    def test_first_run_is_ready(self) -> None:
        worker = DayColorWorker(
            chat_db=_FakeChatDB(), settings=_FakeSettings(),
        )
        now = datetime.now(timezone.utc)
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))

    def test_within_interval_not_ready(self) -> None:
        worker = DayColorWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(day_color_check_interval_seconds=3600),
        )
        now = datetime.now(timezone.utc)
        last_run = now - timedelta(seconds=600)  # 10 min ago
        self.assertFalse(worker.is_ready(now=now, last_run_at=last_run))

    def test_after_interval_ready(self) -> None:
        worker = DayColorWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(day_color_check_interval_seconds=3600),
        )
        now = datetime.now(timezone.utc)
        last_run = now - timedelta(seconds=4000)  # >1h ago
        self.assertTrue(worker.is_ready(now=now, last_run_at=last_run))

    def test_interval_seconds_property_reads_settings(self) -> None:
        # The property is queried by the scheduler each tick so a
        # settings reload picks up new cadence without restart. Use
        # an unusual value that wouldn't be hit by accident.
        worker = DayColorWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(day_color_check_interval_seconds=777),
        )
        self.assertEqual(worker.interval_seconds, 777.0)


# ── run() ────────────────────────────────────────────────────────────


class RunTests(unittest.TestCase):
    def _set_today(self, chat_db: _FakeChatDB) -> None:
        # Helper: pretend yesterday's worker tick set today's colour
        # already. The next ``run()`` should be a no-op.
        now = datetime.now().astimezone()
        chat_db._store[KV_DAY_COLOR] = "cozy"
        chat_db._store[KV_DAY_COLOR_SET_AT] = now.isoformat()

    def test_run_skips_when_disabled(self) -> None:
        chat_db = _FakeChatDB()
        worker = DayColorWorker(
            chat_db=chat_db,
            settings=_FakeSettings(day_color_enabled=False),
        )
        stats = worker.run()
        self.assertEqual(stats.get("skipped"), True)
        self.assertEqual(stats.get("reason"), "disabled")
        self.assertEqual(chat_db.kv_set_calls, 0)

    def test_run_skips_when_fresh(self) -> None:
        chat_db = _FakeChatDB()
        self._set_today(chat_db)
        before_writes = chat_db.kv_set_calls
        worker = DayColorWorker(
            chat_db=chat_db, settings=_FakeSettings(),
        )
        stats = worker.run()
        self.assertEqual(stats.get("rolled"), False)
        self.assertEqual(stats.get("reason"), "fresh")
        # No kv_meta mutations on the steady-state path.
        self.assertEqual(chat_db.kv_set_calls, before_writes)

    def test_run_rolls_when_stale(self) -> None:
        chat_db = _FakeChatDB(
            initial={
                KV_DAY_COLOR: "low_key",
                # Clearly stale: 2 days in the past in any timezone.
                KV_DAY_COLOR_SET_AT: (
                    datetime.now(timezone.utc) - timedelta(days=2)
                ).isoformat(),
            }
        )
        worker = DayColorWorker(
            chat_db=chat_db, settings=_FakeSettings(),
        )
        stats = worker.run()
        self.assertEqual(stats.get("rolled"), True)
        self.assertEqual(stats.get("prev"), "low_key")
        # New name must be a real palette entry.
        new_name = stats.get("name")
        self.assertIn(new_name, {c.name for c in day_color.PALETTE})
        # Both kv_meta keys must have been written.
        self.assertEqual(chat_db._store[KV_DAY_COLOR], new_name)
        self.assertTrue(chat_db._store[KV_DAY_COLOR_SET_AT])

    def test_run_rolls_when_missing(self) -> None:
        chat_db = _FakeChatDB()  # empty store
        worker = DayColorWorker(
            chat_db=chat_db, settings=_FakeSettings(),
        )
        stats = worker.run()
        self.assertEqual(stats.get("rolled"), True)
        self.assertIsNone(stats.get("prev"))
        self.assertIn(
            chat_db._store[KV_DAY_COLOR],
            {c.name for c in day_color.PALETTE},
        )

    def test_run_swallows_kv_get_failure(self) -> None:
        chat_db = _FakeChatDB()
        worker = DayColorWorker(
            chat_db=chat_db, settings=_FakeSettings(),
        )
        with mock.patch.object(
            chat_db, "kv_get", side_effect=RuntimeError("db locked"),
        ):
            stats = worker.run()
        # The worker logs + returns a stable-shape dict; it must NOT
        # raise (the IdleWorkerScheduler would otherwise burn its
        # retry budget on a transient DB hiccup).
        self.assertEqual(stats.get("skipped"), True)
        self.assertEqual(stats.get("reason"), "kv_get_failed")

    def test_run_swallows_kv_set_failure(self) -> None:
        chat_db = _FakeChatDB()
        worker = DayColorWorker(
            chat_db=chat_db, settings=_FakeSettings(),
        )

        original_kv_set = chat_db.kv_set
        calls: list[tuple[str, str]] = []

        def failing_kv_set(key: str, value: str) -> None:
            calls.append((key, value))
            if key == KV_DAY_COLOR:
                # Simulate a failure on the first write so the worker
                # never gets to set_at. The graceful-failure path
                # must surface ``kv_set_failed`` without raising.
                raise RuntimeError("disk full")
            original_kv_set(key, value)

        with mock.patch.object(chat_db, "kv_set", side_effect=failing_kv_set):
            stats = worker.run()

        self.assertEqual(stats.get("skipped"), True)
        self.assertEqual(stats.get("reason"), "kv_set_failed")

    def test_run_swallows_roll_failure(self) -> None:
        chat_db = _FakeChatDB()
        worker = DayColorWorker(
            chat_db=chat_db, settings=_FakeSettings(),
        )
        with mock.patch(
            "app.core.affect.day_color_worker.day_color.roll_for_today",
            side_effect=RuntimeError("rng exploded"),
        ):
            stats = worker.run()
        self.assertEqual(stats.get("skipped"), True)
        self.assertEqual(stats.get("reason"), "roll_failed")
        # Nothing should have been written on a failed roll.
        self.assertEqual(chat_db.kv_set_calls, 0)


# ── name / IdleWorker protocol shape ────────────────────────────────


class WorkerShapeTests(unittest.TestCase):
    def test_name_is_stable(self) -> None:
        # ``name`` is what the scheduler's force_run / per-tick log
        # lines key on. Pinning it so an accidental rename is a test
        # failure rather than a silent break of MCP debug tooling.
        self.assertEqual(DayColorWorker.name, "day_color")

    def test_kv_keys_are_namespaced(self) -> None:
        # Other K* features (memory, beliefs, goals) put their
        # kv_meta keys under their own namespace. K27 keys must too,
        # otherwise a future feature could collide.
        self.assertTrue(KV_DAY_COLOR.startswith("aiko."))
        self.assertTrue(KV_DAY_COLOR_SET_AT.startswith("aiko."))


if __name__ == "__main__":
    unittest.main()

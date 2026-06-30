"""Worker-level tests for K72 WellbeingConcernWorker."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from app.core.affect import mood_drift as md
from app.core.proactive.wellbeing_concern_worker import WellbeingConcernWorker
from app.core.relationship import wellbeing_concern as wc


_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _local_ts(day_offset: int, hour: int) -> str:
    """ISO timestamp at a local-tz ``hour`` ``day_offset`` days ago."""
    base = datetime.now(_LOCAL_TZ) - timedelta(days=day_offset)
    return base.replace(
        hour=hour, minute=0, second=0, microsecond=0
    ).isoformat()


class FakeDB:
    def __init__(self, rows, kv=None) -> None:
        self._rows = rows
        self.kv = dict(kv or {})

    def execute_fetchall(self, sql, params):  # noqa: ANN001
        return list(self._rows)

    def kv_get(self, key):  # noqa: ANN001
        return self.kv.get(key)

    def kv_set(self, key, value):  # noqa: ANN001
        self.kv[key] = value


def _now() -> datetime:
    return datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


def _worker(db: FakeDB, **kw) -> WellbeingConcernWorker:
    return WellbeingConcernWorker(chat_db=db, clock=_now, **kw)


class WorkerTests(unittest.TestCase):
    def test_drafts_late_nights(self) -> None:
        rows = [(_local_ts(d, 3), "just chatting") for d in (1, 2, 3)]
        db = FakeDB(rows)
        w = _worker(db)
        result = w.run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["kind"], wc.KIND_LATE_NIGHTS)
        ring = wc.load_findings(db.kv_get)
        self.assertEqual(ring[-1]["kind"], wc.KIND_LATE_NIGHTS)
        self.assertTrue(db.kv.get("wellbeing_concern.last_signature"))

    def test_drafts_self_neglect_and_outranks(self) -> None:
        rows = [
            (_local_ts(1, 3), "haven't slept again"),
            (_local_ts(2, 3), "still haven't eaten today"),
            (_local_ts(3, 3), "running on no sleep"),
        ]
        db = FakeDB(rows)
        result = _worker(db).run()
        self.assertEqual(result["drafted"], 1)
        # Self-neglect outranks the (also-present) late-night pattern.
        self.assertEqual(result["kind"], wc.KIND_SELF_NEGLECT)

    def test_rough_stretch_from_ring(self) -> None:
        samples = [
            md.DriftSample(
                date=f"2026-01-0{i+1}", valence=-0.3, closeness=0.0,
                humor=0.0, trust=0.0, comfort=0.0,
            )
            for i in range(5)
        ]
        kv = {md.KV_SAMPLES: md.serialize_samples(samples)}
        # Afternoon, neutral content -> no behavioral signal.
        rows = [(_local_ts(1, 14), "nice afternoon")]
        db = FakeDB(rows, kv=kv)
        result = _worker(db).run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["kind"], wc.KIND_ROUGH_STRETCH)

    def test_no_finding_clean(self) -> None:
        rows = [(_local_ts(1, 14), "had a great day, big lunch")]
        db = FakeDB(rows)
        result = _worker(db).run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result["no_finding"])

    def test_cooldown_blocks(self) -> None:
        rows = [(_local_ts(d, 3), "hi") for d in (1, 2, 3)]
        kv = {
            "wellbeing_concern.last_fired_at": (
                _now() - timedelta(days=1)
            ).isoformat(),
        }
        db = FakeDB(rows, kv=kv)
        result = _worker(db, cooldown_days=7.0).run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result["skipped_cooldown"])

    def test_force_bypasses_cooldown(self) -> None:
        rows = [(_local_ts(d, 3), "hi") for d in (1, 2, 3)]
        kv = {
            "wellbeing_concern.last_fired_at": (
                _now() - timedelta(days=1)
            ).isoformat(),
        }
        db = FakeDB(rows, kv=kv)
        w = _worker(db, cooldown_days=7.0)
        w.force_next()
        result = w.run()
        self.assertEqual(result["drafted"], 1)

    def test_same_signature_suppressed(self) -> None:
        rows = [(_local_ts(d, 3), "hi") for d in (1, 2, 3)]
        kv = {"wellbeing_concern.last_signature": "late_nights:3"}
        db = FakeDB(rows, kv=kv)
        result = _worker(db).run()
        self.assertEqual(result["drafted"], 0)
        self.assertEqual(result["same_signature"], "late_nights:3")

    def test_disabled(self) -> None:
        db = FakeDB([(_local_ts(d, 3), "hi") for d in (1, 2, 3)])
        result = _worker(db, enabled_provider=lambda: False).run()
        self.assertTrue(result["disabled"])

    def test_select_failure_silent(self) -> None:
        class BadDB(FakeDB):
            def execute_fetchall(self, sql, params):  # noqa: ANN001
                raise RuntimeError("boom")

        db = BadDB([])
        result = _worker(db).run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result["no_finding"])


if __name__ == "__main__":
    unittest.main()

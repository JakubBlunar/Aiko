"""Worker-level tests for K72 WellbeingConcernWorker."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.affect import mood_drift as md
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
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


def _worker(db: FakeDB, *, cues=None, **kw) -> WellbeingConcernWorker:
    return WellbeingConcernWorker(
        chat_db=db,
        clock=_now,
        cue_store_provider=(lambda: cues) if cues is not None else None,
        user_name_provider=lambda: "Jacob",
        **kw,
    )


def _cue_store() -> tuple[CueStore, TemporaryDirectory]:
    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    return CueStore(ChatDatabase(Path(tmp.name) / "chat.db")), tmp


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

    def test_force_bypasses_the_signature_gate(self) -> None:
        rows = [(_local_ts(d, 3), "hi") for d in (1, 2, 3)]
        db = FakeDB(rows, kv={"wellbeing_concern.last_signature": "late_nights:3"})
        w = _worker(db)
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


class PoolProductionTests(unittest.TestCase):
    """What replaced the seven-day producer cooldown."""

    def setUp(self) -> None:
        self.cues, tmp = _cue_store()
        self.addCleanup(tmp.cleanup)

    def _late_nights(self) -> FakeDB:
        return FakeDB([(_local_ts(d, 3), "just chatting") for d in (1, 2, 3)])

    def test_a_drafted_concern_lands_in_the_pool(self) -> None:
        _worker(self._late_nights(), cues=self.cues).run()
        rows = self.cues.pending("wellbeing_concern")
        self.assertEqual(len(rows), 1)
        self.assertIn("night", rows[0].subject)
        # The rendered line, not the raw finding -- the pool row is what
        # the provider drops into the prompt.
        self.assertIn("jacob", rows[0].text.lower())
        self.assertEqual(rows[0].payload["kind"], wc.KIND_LATE_NIGHTS)

    def test_pressure_falls_once_a_worry_is_on_the_shelf(self) -> None:
        now = _now()
        worker = _worker(self._late_nights(), cues=self.cues)
        self.assertEqual(
            worker.demand(now=now, last_run_at=None).pressure, 1.0,
        )
        worker.run()
        self.assertEqual(
            worker.demand(now=now, last_run_at=None).pressure, 0.0,
        )

    def test_a_disabled_worker_reports_no_pressure(self) -> None:
        signal = _worker(
            self._late_nights(), cues=self.cues,
            enabled_provider=lambda: False,
        ).demand(now=_now(), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")

    def test_no_pool_leaves_the_worker_on_plain_intervals(self) -> None:
        self.assertIsNone(
            _worker(self._late_nights()).demand(now=_now(), last_run_at=None)
        )


class FakeActivityStore:
    def __init__(self, sessions, boom: bool = False) -> None:
        self._sessions = sessions
        self._boom = boom

    def recent_sessions(self, *, limit=20):  # noqa: ARG002
        if self._boom:
            raise RuntimeError("store down")
        return list(self._sessions)


def _session_at(day_offset: int, hour: int) -> dict:
    stamp = _local_ts(day_offset, hour)
    return {
        "source": "foreground",
        "app": "Cursor",
        "title": "secret.py — assistant",
        "started_at": stamp,
        "ended_at": stamp,
        "duration_seconds": 1800,
    }


def _focus_session(
    day_offset: int,
    hour: int = 10,
    *,
    seconds: int = 6 * 3600,
    app: str = "Cursor",
) -> dict:
    start = datetime.fromisoformat(_local_ts(day_offset, hour))
    end = start + timedelta(seconds=seconds)
    return {
        "source": "foreground",
        "app": app,
        "title": "secret.py — assistant",
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "duration_seconds": seconds,
    }


class ActivityLateNightTests(unittest.TestCase):
    def test_editor_sessions_supply_late_nights_without_chat(self) -> None:
        sessions = [_session_at(d, 3) for d in (1, 2, 3)]
        db = FakeDB([])
        w = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: True,
        )
        result = w.run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["kind"], wc.KIND_LATE_NIGHTS)

    def test_disabled_c6_does_not_use_sessions(self) -> None:
        sessions = [_session_at(d, 3) for d in (1, 2, 3)]
        db = FakeDB([])
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: False,
        ).run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("no_finding"))

    def test_store_exception_is_empty_extra_dates(self) -> None:
        db = FakeDB([])
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore([], boom=True),
            activity_enabled_provider=lambda: True,
        ).run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("no_finding"))


class ActivityLongFocusTests(unittest.TestCase):
    def test_daytime_coding_sessions_draft_without_chat(self) -> None:
        sessions = [_focus_session(d) for d in (1, 2, 3)]
        db = FakeDB([])
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: True,
        ).run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["kind"], wc.KIND_LONG_FOCUS)
        self.assertNotIn("secret.py", result["detail"])

    def test_media_sessions_do_not_count(self) -> None:
        sessions = [
            _focus_session(d, app="YouTube") for d in (1, 2, 3)
        ]
        db = FakeDB([])
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: True,
        ).run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("no_finding"))

    def test_short_days_do_not_count(self) -> None:
        sessions = [
            _focus_session(d, seconds=2 * 3600) for d in (1, 2, 3)
        ]
        db = FakeDB([])
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: True,
        ).run()
        self.assertEqual(result["drafted"], 0)

    def test_late_nights_outrank_long_focus(self) -> None:
        sessions = [_focus_session(d) for d in (1, 2, 3)]
        rows = [(_local_ts(d, 3), "just chatting") for d in (1, 2, 3)]
        db = FakeDB(rows)
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: True,
        ).run()
        self.assertEqual(result["kind"], wc.KIND_LATE_NIGHTS)

    def test_disabled_c6_does_not_use_focus_sessions(self) -> None:
        sessions = [_focus_session(d) for d in (1, 2, 3)]
        db = FakeDB([])
        result = _worker(
            db,
            activity_store_provider=lambda: FakeActivityStore(sessions),
            activity_enabled_provider=lambda: False,
        ).run()
        self.assertEqual(result["drafted"], 0)


if __name__ == "__main__":
    unittest.main()

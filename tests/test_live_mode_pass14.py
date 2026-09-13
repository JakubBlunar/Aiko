"""Pass 14: C6 Level-1 aggregation + C8 sleep_return OS-idle qualifier."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.activity.aggregation_worker import (
    KV_ROLLUP,
    KV_SESSION_ID,
    ActivityAggregationWorker,
)
from app.core.activity.evidence import (
    compute_activity_rollup,
    evidence_from_store,
)
from app.core.activity.ingest import ingest_envelope
from app.core.activity.store import ActivityStore
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.session.session_controller import SessionController


_NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-13T20:00:00Z",
        input_mode="typed",
        floor_transition="neither",
        dialogue_act="",
        arc="casual_check_in",
        arc_confidence=0.4,
        mood_label="content",
        vitality_band="normal",
        user_present=True,
        user_active_app="Cursor",
        world=WorldSituation(location_slug="beanbag", activity="idle"),
        inferred=None,
        inferred_stale=True,
        world_compatible=False,
        conflict_reason="",
        shared_commitment_active=False,
        since_user_activity_ms=0,
        sleep={"status": "awake"},
    )
    fields.update(kwargs)
    return ConversationSituationSnapshot(**fields)  # type: ignore[arg-type]


def _env(
    *,
    source: str = "foreground",
    app: str | None = "Cursor",
    title: str | None = "secret.md",
    at: str = "2026-09-13T20:00:00+00:00",
    kind: str = "focus",
) -> dict:
    return {
        "v": 1,
        "at": at,
        "source": source,
        "tier": "cheap",
        "subject": {"app": app, "title": title, "surface_id": "s1"},
        "signal": {"kind": kind},
        "payload": {},
    }


class _Settings:
    def __init__(self) -> None:
        self.agent = SimpleNamespace(
            activity_awareness_enabled=True,
            activity_title_allowlist=["Cursor"],
        )


class _TempDB:
    def __enter__(self) -> ChatDatabase:
        self._dir = tempfile.TemporaryDirectory()
        self.db = ChatDatabase(Path(self._dir.name) / "t.db")
        return self.db

    def __exit__(self, *exc: object) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class RollupTests(unittest.TestCase):
    def test_rollup_never_includes_title(self) -> None:
        rollup = compute_activity_rollup(
            [{
                "id": 3,
                "source": "foreground",
                "app": "Cursor",
                "title": "secret.md",
                "duration_seconds": 90,
            }, {
                "id": 2,
                "source": "idle",
                "app": "",
                "title": "ignored",
                "duration_seconds": 40,
            }, {
                "id": 1,
                "source": "lock",
                "app": "",
                "title": "also-secret",
                "duration_seconds": 12,
            }],
            now=_NOW,
        )
        blob = json.dumps(rollup)
        self.assertNotIn("title", rollup)
        self.assertNotIn("secret", blob)
        self.assertEqual(rollup["app"], "Cursor")
        self.assertEqual(rollup["session_count"], 3)
        self.assertEqual(rollup["idle_span_seconds"], 40)
        self.assertEqual(rollup["lock_span_seconds"], 12)
        self.assertEqual(rollup["last_session_id"], 3)


class AggregationWorkerTests(unittest.TestCase):
    def test_demand_and_persist_without_titles(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            settings = _Settings()
            ingest_envelope(_env(), settings=settings, store=store)
            ingest_envelope(
                _env(source="idle", app=None, title=None, kind="idle",
                     at="2026-09-13T20:00:08+00:00"),
                settings=settings, store=store,
            )
            worker = ActivityAggregationWorker(
                store, kv_get=db.kv_get, kv_set=db.kv_set,
            )
            signal = worker.demand(now=_NOW, last_run_at=None)
            assert signal is not None
            self.assertGreater(signal.pressure, 0.0)
            self.assertFalse(signal.needs_llm)
            result = worker.run()
            assert result is not None
            self.assertNotIn("title", result)
            stored = json.loads(db.kv_get(KV_ROLLUP) or "{}")
            self.assertNotIn("title", stored)
            self.assertNotIn("secret.md", json.dumps(stored))
            self.assertEqual(int(db.kv_get(KV_SESSION_ID) or 0), store.latest_session_id())
            again = worker.demand(now=_NOW, last_run_at=None)
            assert again is not None
            self.assertEqual(again.pressure, 0.0)

    def test_missing_store_does_not_stall(self) -> None:
        ev = evidence_from_store(None, now=_NOW)
        self.assertFalse(ev.present)
        worker = ActivityAggregationWorker(
            None,  # type: ignore[arg-type]
            kv_get=lambda _k: None,
            kv_set=lambda _k, _v: None,
        )
        signal = worker.demand(now=_NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)
        self.assertIsNone(worker.run())


class EvidenceRollupTests(unittest.TestCase):
    def test_evidence_fills_spans(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            settings = _Settings()
            ingest_envelope(_env(), settings=settings, store=store)
            ingest_envelope(
                _env(source="idle", app=None, title=None, kind="idle",
                     at="2026-09-13T20:00:08+00:00"),
                settings=settings, store=store,
            )
            ev = evidence_from_store(store, now=_NOW)
            self.assertGreaterEqual(ev.session_count, 1)
            self.assertGreaterEqual(ev.idle_span_seconds, 0)
            payload = ev.to_payload()
            self.assertNotIn("title", payload)
            self.assertNotIn("secret.md", json.dumps(payload))

    def test_frame_carries_rollup_counts(self) -> None:
        from app.core.activity.evidence import ActivityEvidence

        evidence = ActivityEvidence(
            app="Cursor",
            source="foreground",
            duration_seconds=90,
            os_idle="active",
            stale=False,
            confidence=0.7,
            present=True,
            session_count=4,
            idle_span_seconds=30,
            lock_span_seconds=5,
        )
        frame = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(),
                impulses=(),
                now=_NOW,
                monotonic_ms=10_000.0,
                trigger_kind="heartbeat",
                activity_evidence=evidence,
            ),
            generation=1,
        )
        self.assertEqual(frame.shared.session_count, 4)
        self.assertEqual(frame.shared.idle_span_s, 30)
        self.assertEqual(frame.shared.lock_span_s, 5)


class IdleGatePinTests(unittest.TestCase):
    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))


if __name__ == "__main__":
    unittest.main()

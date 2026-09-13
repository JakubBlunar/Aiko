"""Pass 15: C6 Level-2 interpretation worker."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.activity.aggregation_worker import ActivityAggregationWorker
from app.core.activity.evidence import ActivityEvidence
from app.core.activity.ingest import ingest_envelope
from app.core.activity.interpretation_worker import (
    KV_INTERP,
    KV_INTERP_SESSION_ID,
    ActivityInterpretationWorker,
    decide_interpretation,
    format_interpretation_prompt,
    load_activity_interpretation,
    parse_interpretation_payload,
)
from app.core.activity.store import ActivityStore
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.proactive.idle_worker import SLEEP_CONTINUE_WORKER_NAMES
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


class _FakeOllama:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, messages, *, model=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "model": model,
            "kwargs": kwargs,
        })
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, dict):
            return json.dumps(self.payload), None
        return str(self.payload), None


def _seed_rollup(db: ChatDatabase, store: ActivityStore) -> None:
    settings = _Settings()
    ingest_envelope(_env(), settings=settings, store=store)
    ingest_envelope(
        _env(
            source="idle", app=None, title=None, kind="idle",
            at="2026-09-13T20:00:08+00:00",
        ),
        settings=settings, store=store,
    )
    worker = ActivityAggregationWorker(
        store, kv_get=db.kv_get, kv_set=db.kv_set,
    )
    worker.run()


class ParseTests(unittest.TestCase):
    def test_confidence_required(self) -> None:
        self.assertIsNone(parse_interpretation_payload('{"reading": "coding"}'))
        parsed = parse_interpretation_payload(
            '{"reading": "coding in the editor", "kind": "coding", '
            '"confidence": 0.81}'
        )
        assert parsed is not None
        self.assertEqual(parsed["kind"], "coding")
        self.assertAlmostEqual(parsed["confidence"], 0.81)

    def test_empty_and_low_confidence_are_nothing(self) -> None:
        empty = decide_interpretation(
            {"reading": "", "kind": "", "confidence": 0.0},
        )
        self.assertTrue(empty["nothing"])
        weak = decide_interpretation(
            {"reading": "maybe gaming", "kind": "media", "confidence": 0.2},
        )
        self.assertTrue(weak["nothing"])
        self.assertEqual(weak["reason"], "low_confidence")

    def test_title_leak_is_nothing(self) -> None:
        decided = decide_interpretation(
            {
                "reading": "editing secret.md in Cursor",
                "kind": "coding",
                "confidence": 0.9,
            },
            titles=("secret.md",),
        )
        self.assertTrue(decided["nothing"])
        self.assertEqual(decided["reason"], "title_leak")
        self.assertEqual(decided["reading"], "")


class PromptTests(unittest.TestCase):
    def test_prompt_may_include_allowlisted_title(self) -> None:
        prompt = format_interpretation_prompt(
            {
                "app": "Cursor",
                "session_count": 1,
                "idle_span_seconds": 0,
                "lock_span_seconds": 0,
            },
            [{
                "source": "foreground",
                "app": "Cursor",
                "title": "secret.md",
                "duration_seconds": 90,
                "started_at": "2026-09-13T19:58:00+00:00",
            }],
            now=_NOW,
        )
        self.assertIn("secret.md", prompt)
        self.assertIn("Today is", prompt)
        self.assertIn("Cursor", prompt)


class InterpretationWorkerTests(unittest.TestCase):
    def test_demand_waits_for_rollup_then_needs_llm(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            client = _FakeOllama({
                "reading": "working in the editor",
                "kind": "coding",
                "confidence": 0.82,
            })
            worker = ActivityInterpretationWorker(
                store,
                kv_get=db.kv_get,
                kv_set=db.kv_set,
                ollama=client,
                model="worker",
            )
            ingest_envelope(_env(), settings=_Settings(), store=store)
            before = worker.demand(now=_NOW, last_run_at=None)
            assert before is not None
            self.assertEqual(before.pressure, 0.0)
            self.assertFalse(before.needs_llm)
            _seed_rollup(db, store)
            signal = worker.demand(now=_NOW, last_run_at=None)
            assert signal is not None
            self.assertGreater(signal.pressure, 0.0)
            self.assertTrue(signal.needs_llm)

    def test_run_persists_reading_without_titles(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            _seed_rollup(db, store)
            client = _FakeOllama({
                "reading": "working in the editor",
                "kind": "coding",
                "confidence": 0.82,
            })
            worker = ActivityInterpretationWorker(
                store,
                kv_get=db.kv_get,
                kv_set=db.kv_set,
                ollama=client,
                model="worker",
            )
            result = worker.run()
            assert result is not None
            self.assertFalse(result["nothing"])
            self.assertEqual(result["kind"], "coding")
            self.assertNotIn("title", result)
            blob = json.dumps(result)
            self.assertNotIn("secret.md", blob)
            prompt = client.calls[0]["messages"][1]["content"]
            self.assertIn("secret.md", prompt)
            stored = load_activity_interpretation(db.kv_get)
            assert stored is not None
            self.assertNotIn("title", stored)
            self.assertNotIn("secret.md", json.dumps(stored))
            self.assertEqual(
                int(db.kv_get(KV_INTERP_SESSION_ID) or 0),
                store.latest_session_id(),
            )
            again = worker.demand(now=_NOW, last_run_at=None)
            assert again is not None
            self.assertEqual(again.pressure, 0.0)

    def test_missing_confidence_and_leaked_title_return_nothing(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            _seed_rollup(db, store)
            client = _FakeOllama({"reading": "editing secret.md", "kind": "coding"})
            worker = ActivityInterpretationWorker(
                store,
                kv_get=db.kv_get,
                kv_set=db.kv_set,
                ollama=client,
                model="worker",
            )
            result = worker.run()
            assert result is not None
            self.assertTrue(result["nothing"])
            self.assertEqual(result["reading"], "")
            self.assertNotIn("secret.md", json.dumps(result))
            self.assertEqual(
                int(db.kv_get(KV_INTERP_SESSION_ID) or 0),
                store.latest_session_id(),
            )

            db.kv_set(KV_INTERP_SESSION_ID, "0")
            leak = _FakeOllama({
                "reading": "stuck on secret.md",
                "kind": "coding",
                "confidence": 0.95,
            })
            leaker = ActivityInterpretationWorker(
                store,
                kv_get=db.kv_get,
                kv_set=db.kv_set,
                ollama=leak,
                model="worker",
            )
            leaked = leaker.run()
            assert leaked is not None
            self.assertTrue(leaked["nothing"])
            self.assertEqual(leaked["reason"], "title_leak")

    def test_llm_failure_does_not_advance_watermark(self) -> None:
        with _TempDB() as db:
            store = ActivityStore(db)
            _seed_rollup(db, store)
            worker = ActivityInterpretationWorker(
                store,
                kv_get=db.kv_get,
                kv_set=db.kv_set,
                ollama=_FakeOllama(RuntimeError("down")),
                model="worker",
            )
            self.assertIsNone(worker.run())
            self.assertFalse(db.kv_get(KV_INTERP_SESSION_ID))
            self.assertIsNone(db.kv_get(KV_INTERP))

    def test_missing_store_or_model_does_not_stall(self) -> None:
        worker = ActivityInterpretationWorker(
            None,  # type: ignore[arg-type]
            kv_get=lambda _k: None,
            kv_set=lambda _k, _v: None,
            ollama=None,
            model="",
        )
        signal = worker.demand(now=_NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)
        self.assertIsNone(worker.run())


class FrameAndGateTests(unittest.TestCase):
    def test_frame_still_has_no_reading_or_title(self) -> None:
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
        self.assertFalse(hasattr(frame.shared, "reading"))
        payload = json.dumps(frame.shared.to_payload())
        self.assertNotIn("secret.md", payload)
        self.assertNotIn('"title"', payload)

    def test_sleep_continue_and_no_live_mode_enabled(self) -> None:
        self.assertIn("activity_interpretation", SLEEP_CONTINUE_WORKER_NAMES)
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

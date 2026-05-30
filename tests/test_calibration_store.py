"""Tests for the K20 calibration store + schema migration.

Covers:

  - Schema: ``user_calibration_state`` table exists on a fresh DB
    after schema v14 bump.
  - Round-trip: upsert / get preserves global score, last_updated_at,
    and all topic slot fields including the centroid bytes.
  - Reset: deletes the row; subsequent ``get`` returns baseline.
  - Missing row: ``get`` on an unknown user_id returns baseline.
  - Malformed JSON: corrupted ``state_json`` falls back to baseline
    (defensive parse).
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.calibration_store import (
    CalibrationState,
    CalibrationStore,
    TopicSlot,
    baseline_state,
)
from app.core.chat_database import ChatDatabase


class _TempDB:
    def __enter__(self) -> ChatDatabase:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "test.db"
        self.db = ChatDatabase(self.path)
        return self.db

    def __exit__(self, *exc):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


def _unit_vec(seed: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return (v / float(np.linalg.norm(v))).astype(np.float32)


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class SchemaTests(unittest.TestCase):
    def test_user_calibration_state_table_exists(self):
        with _TempDB() as db:
            conn = db._get_conn()
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("user_calibration_state", tables)

    def test_schema_version_is_at_least_14(self):
        with _TempDB() as db:
            conn = db._get_conn()
            row = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertGreaterEqual(row[0], 14)


class GetBaselineTests(unittest.TestCase):
    def test_missing_user_returns_baseline(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            state = store.get("nobody")
            self.assertEqual(state.global_score, 0.80)
            self.assertIsNone(state.last_updated_at)
            self.assertEqual(state.topics, tuple())

    def test_empty_user_id_returns_baseline(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            state = store.get("")
            self.assertEqual(state.global_score, 0.80)


class RoundTripTests(unittest.TestCase):
    def test_global_score_only_roundtrips(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            written = CalibrationState(
                global_score=0.42,
                last_updated_at=_now(),
                topics=tuple(),
            )
            store.upsert("u1", written)
            read = store.get("u1")
            self.assertAlmostEqual(read.global_score, 0.42, places=4)
            self.assertEqual(read.last_updated_at, _now())

    def test_topic_slots_roundtrip(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            v1 = _unit_vec(1, dim=8)
            v2 = _unit_vec(2, dim=8)
            slots = (
                TopicSlot(
                    centroid=v1, score=0.55,
                    last_signal_at=_now() - timedelta(hours=1),
                    signal_count=3,
                ),
                TopicSlot(
                    centroid=v2, score=0.30,
                    last_signal_at=_now() - timedelta(days=1),
                    signal_count=7,
                ),
            )
            written = CalibrationState(
                global_score=0.50,
                last_updated_at=_now(),
                topics=slots,
            )
            store.upsert("u2", written)
            read = store.get("u2")
            self.assertEqual(len(read.topics), 2)
            self.assertAlmostEqual(read.topics[0].score, 0.55, places=4)
            self.assertAlmostEqual(read.topics[1].score, 0.30, places=4)
            self.assertEqual(read.topics[0].signal_count, 3)
            self.assertEqual(read.topics[1].signal_count, 7)
            # Centroid float32 round-trip: must be near-identical
            np.testing.assert_allclose(
                read.topics[0].centroid, v1, atol=1e-5,
            )
            np.testing.assert_allclose(
                read.topics[1].centroid, v2, atol=1e-5,
            )

    def test_upsert_overwrites(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            store.upsert(
                "u3",
                CalibrationState(
                    global_score=0.30,
                    last_updated_at=_now(),
                    topics=tuple(),
                ),
            )
            store.upsert(
                "u3",
                CalibrationState(
                    global_score=0.90,
                    last_updated_at=_now(),
                    topics=tuple(),
                ),
            )
            read = store.get("u3")
            self.assertAlmostEqual(read.global_score, 0.90, places=4)


class ResetTests(unittest.TestCase):
    def test_reset_deletes_row_and_returns_baseline(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            store.upsert(
                "u4",
                CalibrationState(
                    global_score=0.20,
                    last_updated_at=_now(),
                    topics=tuple(),
                ),
            )
            self.assertAlmostEqual(
                store.get("u4").global_score, 0.20, places=4,
            )
            store.reset("u4")
            after = store.get("u4")
            self.assertEqual(after.global_score, 0.80)
            self.assertIsNone(after.last_updated_at)

    def test_reset_unknown_user_is_silent(self):
        with _TempDB() as db:
            store = CalibrationStore(db, baseline=0.80)
            store.reset("never_seen")  # must not raise


class MalformedJSONTests(unittest.TestCase):
    def test_corrupted_state_json_falls_back_to_baseline(self):
        with _TempDB() as db:
            # Hand-inject garbage directly into the table.
            db.execute_commit(
                "INSERT INTO user_calibration_state "
                "(user_id, state_json, updated_at) VALUES (?, ?, ?)",
                ("u5", "{not-json-at-all", _now().isoformat()),
            )
            store = CalibrationStore(db, baseline=0.80)
            state = store.get("u5")
            self.assertEqual(state.global_score, 0.80)

    def test_partially_malformed_topics_skipped(self):
        with _TempDB() as db:
            # Manually craft a JSON blob with one good slot + one
            # bad slot (missing centroid array). The good one should
            # still load.
            import json

            payload = {
                "global_score": 0.50,
                "last_updated_at": _now().isoformat(),
                "topics": [
                    {
                        "centroid": [1.0, 0.0, 0.0],
                        "score": 0.40,
                        "last_signal_at": _now().isoformat(),
                        "signal_count": 2,
                    },
                    {
                        "centroid": None,
                        "score": 0.30,
                        "last_signal_at": _now().isoformat(),
                        "signal_count": 1,
                    },
                ],
            }
            db.execute_commit(
                "INSERT INTO user_calibration_state "
                "(user_id, state_json, updated_at) VALUES (?, ?, ?)",
                ("u6", json.dumps(payload), _now().isoformat()),
            )
            store = CalibrationStore(db, baseline=0.80)
            state = store.get("u6")
            self.assertEqual(len(state.topics), 1)
            self.assertAlmostEqual(
                state.topics[0].score, 0.40, places=4,
            )


class BaselineFactoryTests(unittest.TestCase):
    def test_baseline_state_default(self):
        s = baseline_state()
        self.assertEqual(s.global_score, 0.80)
        self.assertIsNone(s.last_updated_at)

    def test_baseline_state_override(self):
        s = baseline_state(baseline=0.60)
        self.assertEqual(s.global_score, 0.60)


if __name__ == "__main__":
    unittest.main()

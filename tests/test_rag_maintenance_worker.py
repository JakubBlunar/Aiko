"""Tests for the vector-store compaction worker.

The worker exists because every LanceDB write here is a single row, and
Lance keeps a fragment plus a manifest version per append: one live store
reached 26,766 files and 1.0 GB for 1,796 memories and 4,379 messages
without a single row count looking wrong.

Two properties carry the design and so carry these tests. The probe must
cost a version read rather than a filesystem walk, because walking 26k
files is not a 50 ms probe; and the watermark must be taken *after* the
pass, because compaction itself commits versions and would otherwise
re-arm the worker against its own writes.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core.rag.rag_maintenance_worker import KV_KEY, RagMaintenanceWorker
from app.core.rag.rag_store import RagStore


def _now() -> datetime:
    return datetime(2026, 8, 13, 21, 0, tzinfo=timezone.utc)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.reads = 0
        self.writes = 0

    def get(self, key: str) -> str | None:
        self.reads += 1
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.writes += 1
        self.store[key] = value


class _FakeTable:
    def __init__(self, version: int = 1) -> None:
        self.version = version


class _FakeStore:
    """Stands in for RagStore where the test is about the worker's logic."""

    def __init__(self, versions: tuple[int, int, int] = (1, 1, 1)) -> None:
        self._memories = _FakeTable(versions[0])
        self._messages = _FakeTable(versions[1])
        self._documents = _FakeTable(versions[2])
        self.optimize_calls = 0
        self.result: dict[str, Any] = {
            "before": {"memories": {"files": 900}, "messages": {"files": 100}},
            "after": {"memories": {"files": 3}, "messages": {"files": 3}},
            "bytes_freed": 12345,
            "duration_ms": 42.0,
        }
        self.raises = False

    def optimize(self, **_kw: Any) -> dict[str, Any]:
        self.optimize_calls += 1
        if self.raises:
            raise RuntimeError("compaction exploded")
        # Compaction commits versions of its own, exactly as Lance does.
        self._memories.version += 2
        self._messages.version += 2
        return self.result


class DemandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kv = _FakeKV()

    def _worker(self, store: _FakeStore, **kw: Any) -> RagMaintenanceWorker:
        return RagMaintenanceWorker(
            store, kv_get=self.kv.get, kv_set=self.kv.set, **kw,  # type: ignore[arg-type]
        )

    def test_a_quiet_store_reports_no_pressure(self) -> None:
        worker = self._worker(_FakeStore((10, 10, 1)), floor_writes=250)
        signal = worker.demand(now=_now(), last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)
        self.assertIn("since last compaction", signal.reason)

    def test_pressure_climbs_with_writes_since_the_last_pass(self) -> None:
        low = self._worker(
            _FakeStore((300, 100, 1)), floor_writes=100, saturation_writes=2000,
        ).demand(now=_now(), last_run_at=None)
        high = self._worker(
            _FakeStore((1500, 400, 1)), floor_writes=100, saturation_writes=2000,
        ).demand(now=_now(), last_run_at=None)
        assert low is not None and high is not None
        self.assertGreater(high.pressure, low.pressure)

    def test_pressure_saturates_at_one(self) -> None:
        signal = self._worker(
            _FakeStore((90000, 1, 1)), saturation_writes=2000,
        ).demand(now=_now(), last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 1.0)

    def test_the_watermark_is_subtracted(self) -> None:
        # Same absolute version, but already compacted at 1000: almost no
        # backlog, so the worker must stay quiet.
        self.kv.store[KV_KEY] = "1000"
        signal = self._worker(
            _FakeStore((1000, 50, 1)), floor_writes=250,
        ).demand(now=_now(), last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)

    def test_compaction_is_charged_to_the_compute_lane(self) -> None:
        # It is pure IO and CPU; charging it to the LLM lane would make it
        # compete for the budget that exists to protect a shared GPU.
        signal = self._worker(_FakeStore((5000, 1, 1))).demand(
            now=_now(), last_run_at=None
        )
        assert signal is not None
        self.assertFalse(signal.needs_llm)
        self.assertEqual(signal.lane, "compute")

    def test_an_unreadable_version_defers_to_the_heartbeat(self) -> None:
        # Returning None means "no opinion, schedule me the old way", which
        # is safer than reporting zero pressure and never running again.
        class _Broken(_FakeStore):
            @property  # type: ignore[misc]
            def _memories(self) -> Any:  # noqa: D401
                raise RuntimeError("gone")

            @_memories.setter
            def _memories(self, _value: Any) -> None:
                pass

        self.assertIsNone(
            self._worker(_Broken()).demand(now=_now(), last_run_at=None)
        )

    def test_a_corrupt_watermark_is_treated_as_never_compacted(self) -> None:
        self.kv.store[KV_KEY] = "not a number"
        signal = self._worker(
            _FakeStore((5000, 1, 1)), saturation_writes=2000,
        ).demand(now=_now(), last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 1.0)

    def test_the_probe_reads_the_watermark_and_nothing_heavier(self) -> None:
        worker = self._worker(store := _FakeStore((5000, 1, 1)))
        worker.demand(now=_now(), last_run_at=None)
        self.assertEqual(store.optimize_calls, 0)
        self.assertLessEqual(self.kv.reads, 1)


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kv = _FakeKV()

    def _worker(self, store: _FakeStore) -> RagMaintenanceWorker:
        return RagMaintenanceWorker(
            store, kv_get=self.kv.get, kv_set=self.kv.set,  # type: ignore[arg-type]
        )

    def test_a_run_compacts_and_summarises(self) -> None:
        store = _FakeStore((900, 100, 1))
        found = self._worker(store).run()
        assert found is not None
        self.assertEqual(store.optimize_calls, 1)
        self.assertEqual(found["files_before"], 1000)
        self.assertEqual(found["files_after"], 6)
        self.assertEqual(found["bytes_freed"], 12345)

    def test_the_watermark_is_stored_after_the_pass_not_before(self) -> None:
        # Compaction commits versions of its own. A watermark captured
        # before the pass would count those as fresh writes and re-arm the
        # worker immediately against its own output.
        store = _FakeStore((900, 100, 1))
        worker = self._worker(store)
        worker.run()
        self.assertEqual(self.kv.store[KV_KEY], "1005")
        after = worker.demand(now=_now(), last_run_at=None)
        assert after is not None
        self.assertEqual(after.pressure, 0.0)

    def test_a_second_run_back_to_back_stays_quiet(self) -> None:
        store = _FakeStore((3000, 100, 1))
        worker = self._worker(store)
        worker.run()
        signal = worker.demand(now=_now(), last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)

    def test_a_failed_pass_is_reported_and_leaves_the_watermark_alone(self) -> None:
        store = _FakeStore((3000, 100, 1))
        store.raises = True
        found = self._worker(store).run()
        assert found is not None
        self.assertTrue(found.get("skipped"))
        self.assertNotIn(KV_KEY, self.kv.store)

    def test_per_table_errors_reach_the_summary(self) -> None:
        store = _FakeStore((900, 100, 1))
        store.result = dict(store.result, errors={"messages": "boom"})
        found = self._worker(store).run()
        assert found is not None
        self.assertIn("messages", found["errors"])

    def test_the_worker_runs_without_a_kv_store(self) -> None:
        # Tests construct it bare; a missing kv must degrade to "always
        # looks like backlog" rather than raising inside the scheduler.
        store = _FakeStore((3000, 100, 1))
        worker = RagMaintenanceWorker(store)  # type: ignore[arg-type]
        self.assertIsNotNone(worker.run())
        self.assertEqual(store.optimize_calls, 1)


class ProtocolTests(unittest.TestCase):
    def test_it_satisfies_the_idle_worker_protocol(self) -> None:
        from app.core.proactive.idle_worker import DemandAwareWorker, IdleWorker

        worker = RagMaintenanceWorker(_FakeStore())  # type: ignore[arg-type]
        self.assertIsInstance(worker, IdleWorker)
        self.assertIsInstance(worker, DemandAwareWorker)
        self.assertEqual(worker.name, "rag_maintenance")

    def test_the_heartbeat_is_hours_not_minutes(self) -> None:
        # The pass takes the store's exclusive write lock, so a turn that
        # arrives mid-compaction waits for it. Tidying eagerly would trade
        # a disk problem for a latency one.
        worker = RagMaintenanceWorker(_FakeStore())  # type: ignore[arg-type]
        self.assertGreaterEqual(worker.interval_seconds, 3600.0)

    def test_a_silly_interval_is_floored(self) -> None:
        worker = RagMaintenanceWorker(
            _FakeStore(), interval_seconds=0.0,  # type: ignore[arg-type]
        )
        self.assertGreaterEqual(worker.interval_seconds, 60.0)


class RealStoreTests(unittest.TestCase):
    """End to end against a real LanceDB store in a temp dir."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aiko-ragmaint-"))
        self.store = RagStore(self.tmp, embedding_model="x", vector_dim=4)
        self.kv = _FakeKV()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, n: int) -> None:
        for i in range(n):
            vec = np.zeros(4, dtype=np.float32)
            vec[i % 4] = 1.0
            self.store.add_memory(
                record_id=f"m{i}", content=f"memory {i}", kind="fact", embedding=vec,
            )

    def test_writes_raise_pressure_and_a_run_relieves_it(self) -> None:
        worker = RagMaintenanceWorker(
            self.store,
            kv_get=self.kv.get,
            kv_set=self.kv.set,
            floor_writes=10,
            saturation_writes=40,
        )
        self.assertEqual(
            worker.demand(now=_now(), last_run_at=None).pressure, 0.0  # type: ignore[union-attr]
        )
        self._add(40)
        armed = worker.demand(now=_now(), last_run_at=None)
        assert armed is not None
        self.assertGreater(armed.pressure, 0.0)

        summary = worker.run()
        assert summary is not None
        self.assertLess(summary["files_after"], summary["files_before"])
        self.assertEqual(self.store.counts()["memories"], 40)

        settled = worker.demand(now=_now(), last_run_at=None)
        assert settled is not None
        self.assertEqual(settled.pressure, 0.0)

    def test_the_probe_is_far_cheaper_than_the_run(self) -> None:
        # The premise of demand scheduling, and the reason the probe reads
        # a version instead of counting files on disk.
        import time

        self._add(40)
        worker = RagMaintenanceWorker(
            self.store, kv_get=self.kv.get, kv_set=self.kv.set, floor_writes=1,
        )
        worker.demand(now=_now(), last_run_at=None)
        started = time.perf_counter()
        for _ in range(20):
            worker.demand(now=_now(), last_run_at=None)
        probe_ms = (time.perf_counter() - started) * 1000.0 / 20.0
        self.assertLess(probe_ms, 50.0)

    def test_search_still_works_after_a_run(self) -> None:
        self._add(24)
        RagMaintenanceWorker(
            self.store, kv_get=self.kv.get, kv_set=self.kv.set, floor_writes=1,
        ).run()
        probe = np.zeros(4, dtype=np.float32)
        probe[2] = 1.0
        hits = self.store.search_memories(probe, top_k=2, min_score=0.0)
        self.assertTrue(hits)
        self.assertEqual(hits[0].record.content, "memory 2")


if __name__ == "__main__":
    unittest.main()

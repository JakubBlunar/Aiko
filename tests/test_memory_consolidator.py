"""Tests for MemoryConsolidator (Phase 4b)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_consolidator import (
    MemoryConsolidator,
    _clean_merge_output,
    _cluster_memories,
    _split_survivor,
)
from app.core.memory.memory_store import Memory, MemoryStore


def _make_memory(
    *,
    mid: int,
    content: str,
    embedding: np.ndarray,
    salience: float = 0.5,
    kind: str = "fact",
    use_count: int = 0,
    created_at: str | None = None,
) -> Memory:
    return Memory(
        id=mid,
        content=content,
        kind=kind,
        salience=salience,
        embedding=embedding.astype(np.float32),
        source_session=None,
        source_message_id=None,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        last_used_at=None,
        use_count=use_count,
    )


def _normed(vec: list[float]) -> np.ndarray:
    arr = np.array(vec, dtype=np.float32)
    n = np.linalg.norm(arr)
    if n > 0:
        arr = arr / n
    return arr


class _FakeOllama:
    def __init__(self, response: str = "merged note"):
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.store = MemoryStore(self.path, max_memories=50, dedupe_threshold=0.999)

    def close(self):
        try:
            self.store.close()
        except Exception:
            pass
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class ClusterMemoriesTests(unittest.TestCase):
    def test_clusters_close_pairs(self):
        e1 = _normed([1.0, 0.0, 0.0])
        e2 = _normed([0.95, 0.05, 0.0])
        e3 = _normed([0.0, 1.0, 0.0])
        items = [
            _make_memory(mid=1, content="a", embedding=e1),
            _make_memory(mid=2, content="b", embedding=e2),
            _make_memory(mid=3, content="c", embedding=e3),
        ]
        clusters = _cluster_memories(items, similarity=0.85, min_size=2)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({m.id for m in clusters[0]}, {1, 2})

    def test_below_min_size(self):
        e1 = _normed([1.0, 0.0])
        e2 = _normed([0.99, 0.01])
        items = [
            _make_memory(mid=1, content="a", embedding=e1),
            _make_memory(mid=2, content="b", embedding=e2),
        ]
        clusters = _cluster_memories(items, similarity=0.85, min_size=3)
        self.assertEqual(clusters, [])

    def test_no_cluster_when_dissimilar(self):
        e1 = _normed([1.0, 0.0, 0.0])
        e2 = _normed([0.0, 1.0, 0.0])
        e3 = _normed([0.0, 0.0, 1.0])
        items = [
            _make_memory(mid=1, content="a", embedding=e1),
            _make_memory(mid=2, content="b", embedding=e2),
            _make_memory(mid=3, content="c", embedding=e3),
        ]
        self.assertEqual(_cluster_memories(items, similarity=0.85, min_size=2), [])


class SplitSurvivorTests(unittest.TestCase):
    def test_picks_highest_salience(self):
        e = _normed([1.0, 0.0])
        a = _make_memory(mid=1, content="lo", embedding=e, salience=0.4)
        b = _make_memory(mid=2, content="hi", embedding=e, salience=0.9)
        c = _make_memory(mid=3, content="mid", embedding=e, salience=0.6)
        survivor, victims = _split_survivor([a, b, c])
        self.assertEqual(survivor.id, 2)
        self.assertEqual({v.id for v in victims}, {1, 3})


class CleanMergeOutputTests(unittest.TestCase):
    def test_strips_code_fences(self):
        self.assertEqual(_clean_merge_output("```\nhello\n```"), "hello")

    def test_strips_quotes(self):
        self.assertEqual(_clean_merge_output('"hi there"'), "hi there")

    def test_takes_first_bullet(self):
        self.assertEqual(_clean_merge_output("- one\n- two"), "one")

    def test_truncates(self):
        out = _clean_merge_output("x" * 800)
        self.assertTrue(out.endswith("…"))
        self.assertLess(len(out), 700)

    def test_empty(self):
        self.assertEqual(_clean_merge_output(""), "")


class ConsolidatorRunTests(unittest.TestCase):
    def _seed(self, store: MemoryStore, *, n_pairs: int = 1) -> list[int]:
        ids: list[int] = []
        for k in range(n_pairs):
            # Two vectors with cosine ~0.96 — clearly cluster-able but
            # below the MemoryStore's dedupe_threshold of 0.999.
            base = _normed([1.0, float(k) * 0.01, 0.0, 0.0])
            twin = _normed([1.0, float(k) * 0.01, 0.28, 0.0])
            m1 = store.add(
                f"Jacob is learning topic {k}",
                "fact",
                base,
                salience=0.6,
            )
            m2 = store.add(
                f"Jacob practices topic {k} daily",
                "fact",
                twin,
                salience=0.4,
            )
            assert m1 is not None and m2 is not None
            ids.append(m1.id)
            ids.append(m2.id)
        # Add a clearly separate one we don't expect to merge.
        far = _normed([0.0, 0.0, 0.0, 1.0])
        m3 = store.add("Jacob dislikes spam emails", "fact", far, salience=0.5)
        assert m3 is not None
        ids.append(m3.id)
        return ids

    def test_merges_cluster_without_llm(self):
        f = _Fixture()
        try:
            self._seed(f.store)
            consolidator = MemoryConsolidator(
                ollama=None,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.95,
                use_llm_merge=False,
                min_hours_between=0.0,
            )
            result = consolidator.maybe_run("user-1")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreaterEqual(result.merges_applied, 1)
            self.assertGreaterEqual(result.deletions, 1)
            # Survivor still in mirror.
            self.assertGreater(f.store.count(), 0)
        finally:
            f.close()

    def test_merges_cluster_with_llm(self):
        f = _Fixture()
        try:
            self._seed(f.store)
            ollama = _FakeOllama(response="Jacob is steadily building his topic 0 chops.")
            consolidator = MemoryConsolidator(
                ollama=ollama,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.95,
                use_llm_merge=True,
                min_hours_between=0.0,
            )
            result = consolidator.maybe_run("user-1")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreaterEqual(result.merges_applied, 1)
            self.assertEqual(len(ollama.calls), consolidator.stats()["llm_calls"])
            # Find the surviving memory; content should match the synthesised text.
            recent = f.store.list_recent(limit=10)
            contents = [m.content for m in recent]
            self.assertTrue(any("topic 0 chops" in c for c in contents))
        finally:
            f.close()

    def test_throttles_with_min_hours(self):
        f = _Fixture()
        try:
            self._seed(f.store)
            consolidator = MemoryConsolidator(
                ollama=None,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.95,
                use_llm_merge=False,
                min_hours_between=12.0,
            )
            first = consolidator.maybe_run("user-1")
            self.assertIsNotNone(first)
            second = consolidator.maybe_run("user-1")
            self.assertIsNone(second)
            self.assertEqual(consolidator.stats()["skipped_recent"], 1)
        finally:
            f.close()

    def test_should_run_after_cooldown(self):
        f = _Fixture()
        try:
            self._seed(f.store)
            consolidator = MemoryConsolidator(
                ollama=None,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.95,
                use_llm_merge=False,
                min_hours_between=12.0,
            )
            consolidator.maybe_run("user-1")
            future = datetime.now(timezone.utc) + timedelta(hours=24)
            self.assertTrue(consolidator.should_run("user-1", now_utc=future))
        finally:
            f.close()

    def test_force_run_ignores_throttling(self):
        f = _Fixture()
        try:
            self._seed(f.store)
            consolidator = MemoryConsolidator(
                ollama=None,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.95,
                use_llm_merge=False,
                min_hours_between=12.0,
            )
            consolidator.maybe_run("user-1")
            forced = consolidator.force_run("user-1")
            self.assertIsNotNone(forced)
        finally:
            f.close()

    def test_skips_when_no_memories(self):
        f = _Fixture()
        try:
            consolidator = MemoryConsolidator(
                ollama=None,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                min_hours_between=0.0,
            )
            self.assertIsNone(consolidator.maybe_run("user-1"))
            self.assertEqual(consolidator.stats()["skipped_no_memories"], 1)
        finally:
            f.close()

    def test_skips_self_and_reflection_kinds(self):
        f = _Fixture()
        try:
            e1 = _normed([1.0, 0.0, 0.0, 0.0])
            e2 = _normed([1.0, 0.0, 0.28, 0.0])
            f.store.add("I am thoughtful", "self", e1, salience=0.9)
            f.store.add("I am very thoughtful", "self", e2, salience=0.85)
            consolidator = MemoryConsolidator(
                ollama=None,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.9,
                use_llm_merge=False,
                min_hours_between=0.0,
            )
            result = consolidator.maybe_run("user-1")
            # No fact-kind clusters; result is None or has no merges.
            if result is not None:
                self.assertEqual(result.merges_applied, 0)
        finally:
            f.close()

    def test_llm_failure_falls_back_to_top_salience(self):
        f = _Fixture()
        try:
            self._seed(f.store)
            ollama = _FakeOllama()
            ollama.fail = True
            consolidator = MemoryConsolidator(
                ollama=ollama,
                memory_store=f.store,
                chat_db=f.db,
                model="m",
                similarity_threshold=0.95,
                use_llm_merge=True,
                min_hours_between=0.0,
            )
            result = consolidator.maybe_run("user-1")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreaterEqual(result.merges_applied, 1)
            self.assertEqual(consolidator.stats()["failed_llm"], result.merges_applied)
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()

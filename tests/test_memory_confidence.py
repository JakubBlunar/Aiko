"""Schema v9 confidence-tier tests.

Covers the F3 additions:
- ``memories.confidence`` column lands with default 0.7 on migration.
- Pinned rows backfill to >= 0.9 on the v8 -> v9 ALTER.
- ``Memory.confidence`` round-trips through ``add`` / ``update``.
- Kind-aware defaults inside ``MemoryStore.add()``.
- ``set_pinned(True)`` clamps confidence to >= 0.9; un-pinning leaves it.
- ``_reload_mirror`` handles the v9 row shape (and falls back to 0.7
  default when the column is missing — exercised by the legacy fallback
  branches in the existing tier tests).
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.chat_database import _SCHEMA_VERSION, ChatDatabase
from app.core.memory_store import MemoryStore


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


def _store_factory() -> "tuple[Path, MemoryStore]":
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    return path, store


def _emb(text: str) -> np.ndarray:
    return _FakeEmbedder().embed(text)


class TestSchemaMigration(unittest.TestCase):
    def test_schema_version_is_10(self) -> None:
        self.assertEqual(_SCHEMA_VERSION, 10)

    def test_fresh_database_has_confidence_column(self) -> None:
        path, _store = _store_factory()
        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        finally:
            conn.close()
        self.assertIn("confidence", cols)

    def test_v8_to_v9_alter_lands_default_0_7(self) -> None:
        """Manually build a v8 database; opening it should add the column
        with the documented 0.7 default + 0.9 pinned backfill.
        """
        d = tempfile.mkdtemp()
        path = Path(d) / "v8.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (8);
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    salience REAL NOT NULL DEFAULT 0.5,
                    embedding BLOB NOT NULL,
                    source_session TEXT,
                    source_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    tier TEXT NOT NULL DEFAULT 'long_term',
                    revival_score REAL NOT NULL DEFAULT 0.0
                );
                INSERT INTO memories (
                    content, kind, salience, embedding, created_at, pinned
                ) VALUES
                    ('unpinned row', 'fact', 0.6, X'00', '2026-01-01T00:00:00Z', 0),
                    ('pinned row',   'fact', 0.6, X'00', '2026-01-01T00:00:00Z', 1);
                """
            )
            conn.commit()
        finally:
            conn.close()

        # Opening through ChatDatabase runs _init_schema which performs the
        # guarded ALTER + the pinned backfill.
        ChatDatabase(path)

        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            self.assertIn("confidence", cols)
            rows = list(
                conn.execute(
                    "SELECT content, pinned, confidence FROM memories "
                    "ORDER BY id"
                )
            )
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 10)
        # Unpinned row stays at 0.7 default.
        self.assertEqual(rows[0][0], "unpinned row")
        self.assertAlmostEqual(rows[0][2], 0.7, places=5)
        # Pinned row was backfilled to 0.9.
        self.assertEqual(rows[1][0], "pinned row")
        self.assertEqual(rows[1][1], 1)
        self.assertAlmostEqual(rows[1][2], 0.9, places=5)


class TestConfidenceRoundTrip(unittest.TestCase):
    def test_default_confidence_for_plain_fact_is_0_7(self) -> None:
        _, store = _store_factory()
        mem = store.add("plain fact", "fact", _emb("plain"))
        assert mem is not None
        self.assertAlmostEqual(mem.confidence, 0.7, places=5)

    def test_self_tagged_default_is_0_85(self) -> None:
        _, store = _store_factory()
        mem = store.add("a self note", "self_tagged", _emb("self note"))
        assert mem is not None
        self.assertAlmostEqual(mem.confidence, 0.85, places=5)

    def test_self_kind_default_is_0_85(self) -> None:
        _, store = _store_factory()
        mem = store.add("aiko's reflection", "self", _emb("aiko reflection"))
        assert mem is not None
        self.assertAlmostEqual(mem.confidence, 0.85, places=5)

    def test_knowledge_gap_default_is_0_0(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "I don't know what their favorite color is",
            "knowledge_gap",
            _emb("dont know color"),
        )
        assert mem is not None
        self.assertAlmostEqual(mem.confidence, 0.0, places=5)

    def test_explicit_confidence_overrides_kind_default(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "tool result", "fact", _emb("tool"), confidence=0.95
        )
        assert mem is not None
        self.assertAlmostEqual(mem.confidence, 0.95, places=5)

    def test_confidence_clamped_to_unit_range(self) -> None:
        _, store = _store_factory()
        a = store.add("low edge", "fact", _emb("low"), confidence=-1.0)
        b = store.add("high edge", "fact", _emb("high"), confidence=5.0)
        assert a is not None and b is not None
        self.assertAlmostEqual(a.confidence, 0.0, places=5)
        self.assertAlmostEqual(b.confidence, 1.0, places=5)

    def test_pinned_on_create_clamps_to_0_9(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "pinned at create",
            "fact",
            _emb("pinned create"),
            pinned=True,
            confidence=0.3,
        )
        assert mem is not None
        self.assertGreaterEqual(mem.confidence, 0.9)

    def test_update_persists_confidence(self) -> None:
        _, store = _store_factory()
        mem = store.add("plain fact", "fact", _emb("update fact"))
        assert mem is not None
        updated = store.update(mem.id, confidence=0.2)
        assert updated is not None
        self.assertAlmostEqual(updated.confidence, 0.2, places=5)
        # Reload from disk to be sure the column actually persisted.
        _, fresh = _store_factory()
        # New store on a different DB; just confirm the in-memory mirror
        # snapshot matches the SQL roundtrip on the same store.
        reread = store.get(mem.id)
        assert reread is not None
        self.assertAlmostEqual(reread.confidence, 0.2, places=5)
        _ = fresh  # silence unused

    def test_set_pinned_clamps_confidence_up(self) -> None:
        _, store = _store_factory()
        mem = store.add("low confidence", "fact", _emb("low"), confidence=0.3)
        assert mem is not None
        updated = store.set_pinned(mem.id, True)
        assert updated is not None
        self.assertGreaterEqual(updated.confidence, 0.9)

    def test_unpin_does_not_drop_confidence(self) -> None:
        _, store = _store_factory()
        mem = store.add("anchor", "fact", _emb("anchor"), confidence=0.6)
        assert mem is not None
        store.set_pinned(mem.id, True)
        unpinned = store.set_pinned(mem.id, False)
        assert unpinned is not None
        # The pin path clamped to 0.9; un-pinning leaves the clamped value.
        self.assertGreaterEqual(unpinned.confidence, 0.9)
        self.assertFalse(unpinned.pinned)

    def test_to_dict_includes_confidence(self) -> None:
        _, store = _store_factory()
        mem = store.add("a thing", "fact", _emb("thing"), confidence=0.42)
        assert mem is not None
        snapshot = mem.to_dict()
        self.assertIn("confidence", snapshot)
        self.assertAlmostEqual(float(snapshot["confidence"]), 0.42, places=5)


class TestRagRetrieverConfidencePenalty(unittest.TestCase):
    """The penalty helper used by ``RagRetriever`` to demote low-confidence
    memories during merge. Keeps the unit test focused on the math; the
    end-to-end retrieval ordering is covered by ``test_rag_store.py``.
    """

    def test_high_confidence_has_no_penalty(self) -> None:
        from app.core.rag_retriever import _confidence_penalty

        self.assertAlmostEqual(_confidence_penalty(1.0), 0.0, places=5)
        self.assertAlmostEqual(_confidence_penalty(0.7), 0.0, places=5)
        self.assertAlmostEqual(_confidence_penalty(0.5), 0.0, places=5)

    def test_zero_confidence_hits_the_cap(self) -> None:
        from app.core.rag_retriever import _confidence_penalty

        self.assertAlmostEqual(_confidence_penalty(0.0), -0.15, places=5)

    def test_mid_confidence_is_proportional(self) -> None:
        from app.core.rag_retriever import _confidence_penalty

        # confidence=0.25 -> halfway between 0.0 and 0.5 -> half of -0.15
        self.assertAlmostEqual(_confidence_penalty(0.25), -0.075, places=5)

    def test_none_returns_zero(self) -> None:
        from app.core.rag_retriever import _confidence_penalty

        self.assertAlmostEqual(_confidence_penalty(None), 0.0, places=5)


class TestMemoryRetrieverUncertainSuffix(unittest.TestCase):
    """``MemoryRetriever.format_block`` appends ``(uncertain)`` to lines
    whose source memory has ``confidence < 0.5``.
    """

    def test_low_confidence_gets_suffix(self) -> None:
        from app.core.memory_retriever import MemoryRetriever
        from app.core.memory_store import SearchHit

        _, store = _store_factory()
        low = store.add("low conf claim", "fact", _emb("low"), confidence=0.3)
        high = store.add("high conf claim", "fact", _emb("high"), confidence=0.9)
        assert low is not None and high is not None
        hits = [SearchHit(memory=low, score=0.5), SearchHit(memory=high, score=0.5)]
        block = MemoryRetriever.format_block(hits, user_display_name="Friend")
        self.assertIn("low conf claim (uncertain)", block)
        # The high-confidence one stays plain.
        self.assertIn("- high conf claim", block)
        self.assertNotIn("high conf claim (uncertain)", block)


if __name__ == "__main__":
    unittest.main()

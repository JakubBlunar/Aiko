"""Tests for the LanceDB-backed RagStore + RagRetriever + MessageIndexer.

Uses a deterministic FakeEmbedder so the tests don't need Ollama or any
GPU. The RagStore is created in a fresh temp dir per test so we don't
poison data/lancedb/.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.message_indexer import MessageIndexer
from app.core.memory_store import MemoryStore
from app.core.rag_retriever import RagRetriever
from app.core.rag_store import RagStore


class FakeEmbedder:
    """Maps each unique input to a stable unit vector.

    The mapping is hash-based, so two distinct strings produce
    nearly-orthogonal vectors (good for similarity tests).
    """

    DIM = 16

    def __init__(self) -> None:
        self.model = "fake-embedder"

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=abs(hash(text)) % (2**31))
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


class _TmpRagBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aiko-rag-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class RagStoreCRUDTests(_TmpRagBase):
    def setUp(self) -> None:
        super().setUp()
        self.store = RagStore(self.tmp, embedding_model="x", vector_dim=4)

    def test_add_and_search_memory(self) -> None:
        v_match = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v_dist = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        self.store.add_memory(
            record_id="m1", content="Jacob loves coffee",
            kind="preference", embedding=v_match, salience=0.8,
        )
        self.store.add_memory(
            record_id="m2", content="Unrelated thing",
            kind="fact", embedding=v_dist, salience=0.3,
        )
        hits = self.store.search_memories(v_match, top_k=2, min_score=0.0)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "memory")
        self.assertEqual(hits[0].record.content, "Jacob loves coffee")

    def test_upsert_replaces_record(self) -> None:
        v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        self.store.add_memory(record_id="m1", content="old", kind="fact", embedding=v)
        self.store.add_memory(record_id="m1", content="new", kind="fact", embedding=v)
        hits = self.store.search_memories(v, top_k=5, min_score=0.0)
        self.assertEqual(len([h for h in hits if h.record.id == "m1"]), 1)
        self.assertEqual(hits[0].record.content, "new")

    def test_messages_round_trip(self) -> None:
        v = np.array([0.6, 0.8, 0.0, 0.0], dtype=np.float32)
        self.store.add_message(
            session_id="s1", message_id=42, role="user",
            content="What's the weather?", embedding=v,
        )
        self.assertTrue(self.store.has_message("s1", 42))
        hits = self.store.search_messages(v, top_k=3, min_score=0.0)
        self.assertEqual(hits[0].record.session_id, "s1")
        self.assertEqual(hits[0].record.message_id, 42)

    def test_session_filter(self) -> None:
        v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        self.store.add_message(
            session_id="s1", message_id=1, role="user",
            content="hello from s1", embedding=v,
        )
        self.store.add_message(
            session_id="s2", message_id=1, role="user",
            content="hello from s2", embedding=v,
        )
        hits = self.store.search_messages(v, top_k=5, min_score=0.0, session_id="s2")
        self.assertTrue(hits)
        for h in hits:
            self.assertEqual(h.record.session_id, "s2")

    def test_documents_listing_and_delete(self) -> None:
        v = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        self.store.add_document_chunk(
            document_id="d1", title="My notes", chunk_index=0,
            content="lorem ipsum", embedding=v,
        )
        self.store.add_document_chunk(
            document_id="d1", title="My notes", chunk_index=1,
            content="dolor sit amet", embedding=v,
        )
        listing = self.store.list_documents()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["chunk_count"], 2)
        self.store.delete_document("d1")
        self.assertEqual(self.store.list_documents(), [])


class RagStoreEmbeddingSwapTests(_TmpRagBase):
    def test_dim_change_triggers_rebuild(self) -> None:
        s1 = RagStore(self.tmp, embedding_model="model-a", vector_dim=4)
        v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        s1.add_memory(record_id="m1", content="hi", kind="fact", embedding=v)
        self.assertEqual(s1.counts()["memories"], 1)
        s2 = RagStore(self.tmp, embedding_model="model-b", vector_dim=8)
        # Tables wiped on swap.
        self.assertEqual(s2.counts()["memories"], 0)


class MessageIndexerBackfillTests(_TmpRagBase):
    def setUp(self) -> None:
        super().setUp()
        self.db = ChatDatabase(self.tmp / "chat.db")
        self.embedder = FakeEmbedder()
        self.store = RagStore(
            self.tmp / "lancedb",
            embedding_model="x",
            vector_dim=FakeEmbedder.DIM,
        )

    def test_backfill_is_idempotent(self) -> None:
        # Seed history with three messages.
        for i in range(3):
            self.db.add_message(
                session_id="s1", role="user", content=f"hello world {i}",
            )
        indexer1 = MessageIndexer(self.db, self.store, self.embedder)
        indexer1.start(backfill=True)
        # Wait briefly for the daemon backfill to run.
        for _ in range(40):
            if self.store.counts()["messages"] >= 3:
                break
            time.sleep(0.05)
        indexer1.stop()
        first = self.store.counts()["messages"]
        self.assertEqual(first, 3)
        # Re-run backfill -- counts should not change.
        indexer2 = MessageIndexer(self.db, self.store, self.embedder)
        indexer2.start(backfill=True)
        for _ in range(20):
            time.sleep(0.05)
        indexer2.stop()
        self.assertEqual(self.store.counts()["messages"], first)

    def test_live_listener_indexes_new_message(self) -> None:
        indexer = MessageIndexer(self.db, self.store, self.embedder)
        indexer.start(backfill=False)
        self.db.add_message(session_id="s1", role="user", content="this is a fresh message that should index")
        for _ in range(40):
            if self.store.counts()["messages"] >= 1:
                break
            time.sleep(0.05)
        indexer.stop()
        self.assertEqual(self.store.counts()["messages"], 1)


class RagRetrieverMergeTests(_TmpRagBase):
    def setUp(self) -> None:
        super().setUp()
        self.embedder = FakeEmbedder()
        self.store = RagStore(self.tmp, embedding_model="x", vector_dim=FakeEmbedder.DIM)

    def test_merges_sources_and_dedupes(self) -> None:
        # Two memories + a message; one duplicate text across sources.
        v1 = self.embedder.embed("Jacob loves coffee")
        v2 = self.embedder.embed("Jacob lives in Poland")
        v3 = self.embedder.embed("random older line")
        self.store.add_memory(record_id="m1", content="Jacob loves coffee", kind="preference", embedding=v1, salience=0.9)
        self.store.add_memory(record_id="m2", content="Jacob lives in Poland", kind="fact", embedding=v2, salience=0.7)
        self.store.add_message(
            session_id="s9", message_id=1, role="user",
            content="Jacob loves coffee",  # dup with m1
            embedding=v1,
        )
        retriever = RagRetriever(
            self.store,
            self.embedder,
            top_k=5,
            score_threshold=-1.0,  # FakeEmbedder produces orthogonal vectors for distinct text
            per_source_top_k=4,
        )
        # Query with the exact text so it shares a vector with the seeded
        # records; otherwise FakeEmbedder's random hashing collapses scores.
        hits = retriever.retrieve("Jacob loves coffee")
        self.assertTrue(hits, "expected at least one hit")
        contents = [h.text.strip() for h in hits]
        # Dedupe: "Jacob loves coffee" appears at most once.
        self.assertEqual(
            sum(1 for c in contents if c.lower() == "jacob loves coffee"), 1,
        )

    def test_format_block_splits_self_vs_jacob(self) -> None:
        v_self = self.embedder.embed("I prefer to keep things short")
        v_fact = self.embedder.embed("Jacob lives in Krakow")
        self.store.add_memory(record_id="ms1", content="I prefer to keep things short", kind="self", embedding=v_self, salience=0.7)
        self.store.add_memory(record_id="ms2", content="Jacob lives in Krakow", kind="fact", embedding=v_fact, salience=0.7)
        retriever = RagRetriever(self.store, self.embedder, top_k=5, score_threshold=-1.0)
        # Query verbatim against the fact so FakeEmbedder produces a real
        # similarity (it hashes per-string).
        block = retriever.block_for("Jacob lives in Krakow")
        # The "Jacob" header is required when any non-self memory is hit.
        self.assertIn("What you know about Jacob", block)


if __name__ == "__main__":
    unittest.main()

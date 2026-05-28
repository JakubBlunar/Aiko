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

    def test_list_recent_user_vectors_filters_role_and_prefix(self) -> None:
        # K6 helper: must return only role='user' rows from sessions
        # whose id starts with the given user-id prefix, most recent
        # first, capped at ``limit``.
        v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        v3 = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        v_other = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        # Alice user across two sessions.
        self.store.add_message(
            session_id="alice:s1", message_id=1, role="user",
            content="alice first message", embedding=v1,
            created_at="2026-01-01T10:00:00+00:00",
        )
        self.store.add_message(
            session_id="alice:s1", message_id=2, role="assistant",
            content="aiko reply -- should be excluded", embedding=v2,
            created_at="2026-01-01T10:00:30+00:00",
        )
        self.store.add_message(
            session_id="alice:s2", message_id=10, role="user",
            content="alice second session", embedding=v2,
            created_at="2026-01-02T11:00:00+00:00",
        )
        # Different user should never appear in alice's scan.
        self.store.add_message(
            session_id="bob:s1", message_id=1, role="user",
            content="bob is unrelated", embedding=v_other,
            created_at="2026-01-02T12:00:00+00:00",
        )
        # And a third alice message (most recent).
        self.store.add_message(
            session_id="alice:s2", message_id=11, role="user",
            content="alice newest message", embedding=v3,
            created_at="2026-01-03T09:00:00+00:00",
        )

        vectors = self.store.list_recent_user_vectors(
            user_id_prefix="alice", limit=5,
        )
        self.assertEqual(len(vectors), 3)
        # Most-recent first: newest alice message (v3) leads.
        np.testing.assert_allclose(vectors[0], v3, rtol=1e-5, atol=1e-5)
        # Limit must be respected.
        capped = self.store.list_recent_user_vectors(
            user_id_prefix="alice", limit=2,
        )
        self.assertEqual(len(capped), 2)
        # Prefix isolation: bob's vector never appears.
        for vec in vectors:
            self.assertFalse(
                np.allclose(vec, v_other, atol=1e-5),
                "bob's vector leaked into alice's scan",
            )
        # Empty prefix matches every user row (single-user installs).
        all_users = self.store.list_recent_user_vectors(
            user_id_prefix="", limit=10,
        )
        self.assertEqual(len(all_users), 4)

    def test_list_recent_user_vectors_empty_when_no_user_rows(self) -> None:
        self.assertEqual(
            self.store.list_recent_user_vectors(
                user_id_prefix="ghost", limit=5,
            ),
            [],
        )

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


class BulkAddMemoriesTests(_TmpRagBase):
    """``RagStore.add_memories_bulk`` is the workhorse behind
    :meth:`MemoryStore.migrate_to_rag` — startup mirror of every
    SQLite memory into LanceDB. The per-row path was 270 LanceDB
    write ops for 135 memories (~71s on Windows); the bulk path
    collapses each chunk into a single delete + add. These tests
    pin the contract: rows land, ids upsert, chunking matches the
    no-chunk result, and bad rows are skipped silently like the
    per-row path.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = RagStore(self.tmp, embedding_model="x", vector_dim=4)

    @staticmethod
    def _vec(seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed=seed)
        v = rng.normal(size=4).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v

    def _record(
        self,
        rid: str,
        *,
        content: str | None = None,
        kind: str = "fact",
        salience: float = 0.5,
        embedding: np.ndarray | None = None,
    ) -> dict[str, object]:
        return {
            "record_id": rid,
            "content": content if content is not None else f"memory {rid}",
            "kind": kind,
            "embedding": embedding if embedding is not None else self._vec(hash(rid) & 0x7FFFFFFF),
            "salience": salience,
            "source_session": None,
            "source_message_id": None,
            "created_at": None,
        }

    def test_bulk_add_new_rows(self) -> None:
        records = [self._record(f"m{i}") for i in range(50)]
        written = self.store.add_memories_bulk(records)
        self.assertEqual(written, 50)
        self.assertEqual(self.store.counts()["memories"], 50)
        # Round-trip: the embedding for "m7" should still match itself
        # closely enough to come back at the top of search.
        target = self._vec(hash("m7") & 0x7FFFFFFF)
        hits = self.store.search_memories(target, top_k=3, min_score=0.0)
        self.assertTrue(any(h.record.id == "m7" for h in hits))

    def test_bulk_upserts_existing_rows(self) -> None:
        v = self._vec(1)
        self.store.add_memory(
            record_id="m1", content="old", kind="fact", embedding=v,
        )
        # Same id, new content via the bulk path. Upsert semantics
        # (one row remaining, with the new content) must hold.
        self.store.add_memories_bulk(
            [self._record("m1", content="fresh", embedding=v)],
        )
        self.assertEqual(self.store.counts()["memories"], 1)
        hits = self.store.search_memories(v, top_k=5, min_score=0.0)
        m1_hits = [h for h in hits if h.record.id == "m1"]
        self.assertEqual(len(m1_hits), 1)
        self.assertEqual(m1_hits[0].record.content, "fresh")

    def test_mixed_new_and_existing_in_one_call(self) -> None:
        # Seed three rows individually, then bulk-write a batch of
        # ten that includes those three (with new content) plus seven
        # new ids. The end state should be ten rows with the three
        # existing rows refreshed.
        for i in range(3):
            self.store.add_memory(
                record_id=f"m{i}", content="old", kind="fact",
                embedding=self._vec(i),
            )
        records = [
            # Refresh the seeded three: same vector, new content.
            self._record(f"m{i}", content="refreshed", embedding=self._vec(i))
            for i in range(3)
        ] + [self._record(f"m{i}") for i in range(3, 10)]
        written = self.store.add_memories_bulk(records)
        self.assertEqual(written, 10)
        self.assertEqual(self.store.counts()["memories"], 10)
        # Refreshed content for the seeded ids.
        hits = self.store.search_memories(
            self._vec(0), top_k=10, min_score=0.0,
        )
        m0 = next(h for h in hits if h.record.id == "m0")
        self.assertEqual(m0.record.content, "refreshed")

    def test_chunk_boundary_matches_single_chunk(self) -> None:
        # Same input, different chunk size. The end state must be
        # identical: count, ids, and content per id.
        records = [self._record(f"m{i}") for i in range(10)]
        self.store.add_memories_bulk(records, chunk_size=4)
        self.assertEqual(self.store.counts()["memories"], 10)
        # Compare against a fresh store with chunk_size big enough
        # to land everything in one chunk.
        other_dir = self.tmp / "other"
        other_dir.mkdir()
        other = RagStore(other_dir, embedding_model="x", vector_dim=4)
        other.add_memories_bulk(records, chunk_size=1000)
        self.assertEqual(other.counts()["memories"], 10)

    def test_empty_content_rows_skipped(self) -> None:
        records = [
            self._record("m1", content="real content"),
            self._record("m2", content=""),
            self._record("m3", content="   "),
            self._record("m4", content="another real one"),
        ]
        written = self.store.add_memories_bulk(records)
        self.assertEqual(written, 2)
        self.assertEqual(self.store.counts()["memories"], 2)

    def test_missing_embedding_skipped(self) -> None:
        records = [
            self._record("m1"),
            {**self._record("m2"), "embedding": None},
            self._record("m3"),
        ]
        written = self.store.add_memories_bulk(records)
        self.assertEqual(written, 2)
        self.assertEqual(self.store.counts()["memories"], 2)

    def test_id_with_apostrophe_is_quoted_safely(self) -> None:
        # The bulk delete builds an ``id IN (...)`` SQL predicate, so
        # any apostrophe in a record id has to be escaped or the
        # delete blows up. Belt-and-braces in case future ids embed
        # one.
        records = [
            self._record("m'1"),
            self._record("m2"),
        ]
        written = self.store.add_memories_bulk(records)
        self.assertEqual(written, 2)
        self.assertEqual(self.store.counts()["memories"], 2)


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
        block = retriever.block_for(
            "Jacob lives in Krakow", user_display_name="Jacob",
        )
        # The "Jacob" header is required when any non-self memory is hit.
        self.assertIn("What you know about Jacob", block)


if __name__ == "__main__":
    unittest.main()

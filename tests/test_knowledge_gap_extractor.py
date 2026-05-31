"""F2 personality backlog tests for the knowledge-gap journal.

Covers:
- ``[[gap:topic:question]]`` regex extraction (positive + negative cases).
- ``KnowledgeGapStore.add_gap`` persists with the documented metadata.
- ``prune_overflow`` drops the oldest unpinned unresolved gap above cap.
- ``pick_relevant`` returns the top-1 above threshold and ``None`` below.
- ``mark_resolved`` stamps ``metadata.resolved_at`` (and excludes from open list).
- ``prune_expired`` deletes 90-day-old unresolved unpinned rows only.
- Response-text strip rule removes ``[[gap:...]]`` from visible text.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.knowledge_gap_extractor import (
    GapCandidate,
    KnowledgeGapStore,
    extract_inline_tags,
)
from app.core.memory.memory_store import MemoryStore
from app.core.services.response_text_service import strip_all_meta_tags


class _DeterministicEmbedder:
    """Returns embeddings stable enough that similar text clusters."""

    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        # Encode each token into a slot so related tokens overlap, but
        # the function stays simple: every word picks its slot via hash.
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            slot = hash(token) % self.DIM
            vec[slot] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


def _store_factory(
    *,
    max_open: int = 20,
    ttl_days: int = 90,
) -> tuple[Path, MemoryStore, KnowledgeGapStore]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    memory_store = MemoryStore(path)
    gap_store = KnowledgeGapStore(
        memory_store=memory_store,
        embedder=_DeterministicEmbedder(),
        max_open=max_open,
        ttl_days=ttl_days,
    )
    return path, memory_store, gap_store


class TestInlineRegex(unittest.TestCase):
    def test_extracts_single_well_formed_tag(self) -> None:
        text = (
            "Wait, you said you used to play violin?"
            " [[gap:music:how long has Jacob been playing violin]]"
        )
        cands = extract_inline_tags(text)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].topic, "music")
        self.assertIn("violin", cands[0].question)

    def test_extracts_multiple_distinct_tags(self) -> None:
        text = (
            "[[gap:work:where does Jacob work]] "
            "[[gap:music:does Jacob play piano too]]"
        )
        cands = extract_inline_tags(text)
        self.assertEqual(len(cands), 2)
        topics = {c.topic for c in cands}
        self.assertEqual(topics, {"work", "music"})

    def test_dedupes_repeated_tag_in_same_text(self) -> None:
        text = (
            "[[gap:music:do they play violin]] "
            "[[gap:music:do they play violin]]"
        )
        cands = extract_inline_tags(text)
        self.assertEqual(len(cands), 1)

    def test_rejects_missing_topic(self) -> None:
        text = "[[gap::what about it]]"
        self.assertEqual(extract_inline_tags(text), [])

    def test_rejects_too_short_question(self) -> None:
        text = "[[gap:music:hi]]"
        self.assertEqual(extract_inline_tags(text), [])

    def test_rejects_bracket_in_question(self) -> None:
        text = "[[gap:music:something [bad] here]]"
        self.assertEqual(extract_inline_tags(text), [])

    def test_rejects_newline_in_question(self) -> None:
        text = "[[gap:music:line one\nline two]]"
        self.assertEqual(extract_inline_tags(text), [])


class TestResponseTextStripsGap(unittest.TestCase):
    def test_strip_removes_gap_tag(self) -> None:
        text = "Oh, fun! [[gap:music:do they play piano]] anyway, how's work?"
        stripped = strip_all_meta_tags(text)
        self.assertNotIn("gap:", stripped)
        self.assertIn("Oh, fun!", stripped)
        self.assertIn("how's work?", stripped)

    def test_strip_handles_unclosed_gap_at_eos(self) -> None:
        text = "I wonder [[gap:music:do they"
        stripped = strip_all_meta_tags(text)
        self.assertNotIn("[[gap", stripped)


class TestAddGap(unittest.TestCase):
    def test_add_gap_persists_with_metadata(self) -> None:
        _, mem_store, gaps = _store_factory()
        mem = gaps.add_gap(topic="music", question="do they play piano")
        self.assertIsNotNone(mem)
        assert mem is not None
        self.assertEqual(mem.kind, "knowledge_gap")
        self.assertAlmostEqual(mem.confidence, 0.0, places=5)
        self.assertEqual(mem.metadata.get("topic"), "music")
        self.assertEqual(mem.metadata.get("question"), "do they play piano")
        self.assertIsNone(mem.metadata.get("resolved_at"))

    def test_add_gap_rejects_short_question(self) -> None:
        _, _mem_store, gaps = _store_factory()
        self.assertIsNone(gaps.add_gap(topic="music", question="hi"))


class TestCapAndPruneOverflow(unittest.TestCase):
    def test_overflow_drops_oldest_unpinned_unresolved(self) -> None:
        _, mem_store, gaps = _store_factory(max_open=3)
        ids = []
        for i in range(5):
            mem = gaps.add_gap(
                topic=f"topic{i}",
                question=f"question number {i} for testing the cap",
            )
            assert mem is not None
            ids.append(mem.id)
        open_rows = gaps.list_open()
        self.assertLessEqual(len(open_rows), 3)
        # The two oldest (ids[0], ids[1]) should have been pruned.
        remaining = {m.id for m in open_rows}
        self.assertNotIn(ids[0], remaining)
        self.assertNotIn(ids[1], remaining)

    def test_overflow_keeps_pinned_rows(self) -> None:
        _, mem_store, gaps = _store_factory(max_open=2)
        first = gaps.add_gap(topic="t1", question="first one to keep around")
        assert first is not None
        mem_store.set_pinned(first.id, True)
        # Now flood the cap.
        for i in range(4):
            gaps.add_gap(
                topic=f"flood{i}",
                question=f"flood question {i} to trigger pruning",
            )
        open_rows = gaps.list_open()
        ids = {m.id for m in open_rows}
        # Pinned row must survive.
        self.assertIn(first.id, ids)


class TestPickRelevant(unittest.TestCase):
    def test_picks_top_match_above_threshold(self) -> None:
        _, _mem_store, gaps = _store_factory()
        gaps.add_gap(topic="music", question="violin practice schedule details")
        gaps.add_gap(topic="cooking", question="favourite dinner recipe choice")
        hit = gaps.pick_relevant(
            "I've been practicing violin again", threshold=0.0,
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.metadata.get("topic"), "music")

    def test_returns_none_below_threshold(self) -> None:
        _, _mem_store, gaps = _store_factory()
        gaps.add_gap(topic="music", question="violin practice schedule details")
        hit = gaps.pick_relevant("hello there", threshold=0.99)
        self.assertIsNone(hit)

    def test_excludes_resolved_gaps(self) -> None:
        _, _mem_store, gaps = _store_factory()
        mem = gaps.add_gap(
            topic="music", question="violin practice schedule details",
        )
        assert mem is not None
        gaps.mark_resolved(mem.id, answer_memory_id=None)
        hit = gaps.pick_relevant("violin practice", threshold=0.0)
        self.assertIsNone(hit)


class TestMarkResolved(unittest.TestCase):
    def test_resolved_at_is_stamped(self) -> None:
        _, mem_store, gaps = _store_factory()
        mem = gaps.add_gap(topic="music", question="something to be resolved")
        assert mem is not None
        ok = gaps.mark_resolved(mem.id, answer_memory_id=42)
        self.assertTrue(ok)
        updated = mem_store.get(mem.id)
        assert updated is not None
        self.assertTrue(updated.metadata.get("resolved_at"))
        self.assertEqual(updated.metadata.get("resolved_by_memory_id"), 42)

    def test_returns_false_for_missing_or_wrong_kind(self) -> None:
        _, mem_store, gaps = _store_factory()
        # Create a regular fact and try to mark it resolved as a gap.
        emb = _DeterministicEmbedder().embed("hi there")
        regular = mem_store.add("plain fact entry", "fact", emb)
        assert regular is not None
        self.assertFalse(gaps.mark_resolved(regular.id, answer_memory_id=None))


class TestPruneExpired(unittest.TestCase):
    def test_expired_unresolved_unpinned_gap_is_pruned(self) -> None:
        _, mem_store, gaps = _store_factory(ttl_days=90)
        mem = gaps.add_gap(topic="old", question="something from long ago")
        assert mem is not None
        # Manually rewind the created_at to 100 days ago.
        ancient = (
            datetime.now(timezone.utc) - timedelta(days=100)
        ).isoformat()
        conn = mem_store._get_conn()
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (ancient, mem.id),
        )
        conn.commit()
        # Reload the in-memory mirror so the gap store sees the old date.
        mem_store._reload_mirror()
        pruned = gaps.prune_expired()
        self.assertEqual(pruned, 1)
        self.assertIsNone(mem_store.get(mem.id))

    def test_resolved_gap_is_kept_even_when_expired(self) -> None:
        _, mem_store, gaps = _store_factory(ttl_days=30)
        mem = gaps.add_gap(topic="old", question="resolved long ago")
        assert mem is not None
        gaps.mark_resolved(mem.id, answer_memory_id=None)
        ancient = (
            datetime.now(timezone.utc) - timedelta(days=100)
        ).isoformat()
        conn = mem_store._get_conn()
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (ancient, mem.id),
        )
        conn.commit()
        mem_store._reload_mirror()
        pruned = gaps.prune_expired()
        self.assertEqual(pruned, 0)
        self.assertIsNotNone(mem_store.get(mem.id))

    def test_pinned_gap_is_kept_even_when_expired(self) -> None:
        _, mem_store, gaps = _store_factory(ttl_days=30)
        mem = gaps.add_gap(topic="old", question="pinned and ancient question")
        assert mem is not None
        mem_store.set_pinned(mem.id, True)
        ancient = (
            datetime.now(timezone.utc) - timedelta(days=100)
        ).isoformat()
        conn = mem_store._get_conn()
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (ancient, mem.id),
        )
        conn.commit()
        mem_store._reload_mirror()
        pruned = gaps.prune_expired()
        self.assertEqual(pruned, 0)
        self.assertIsNotNone(mem_store.get(mem.id))


if __name__ == "__main__":
    unittest.main()

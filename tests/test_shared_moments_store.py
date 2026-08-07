"""Tests for :class:`SharedMomentsStore`, focused on what gets embedded.

The store renders each moment as ``"Shared moment (<vibe>): <summary>"`` for
the prompt, but must embed the **bare summary**. Embedding the rendered form
put an identical prefix on all 145 vectors of a real corpus, and the topic
graph duly clustered moments by *vibe word* rather than by what happened --
76 of 77 members of one cluster were ``tender``. That starved every topical
consumer: L7 rituals had minted a single concept from those 145 moments, and
L29(a) shared arcs could not be sourced at all.

So the content format and the embedding basis are two separate contracts,
and both are pinned here.
"""
from __future__ import annotations

import unittest

import numpy as np

from app.core.relationship.shared_moments import SharedMomentsStore


class _RecordingEmbedder:
    """Captures the exact text handed to ``embed``."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, text: str) -> np.ndarray:
        self.texts.append(text)
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float32)


class _MemStub:
    def __init__(self, mid: int, content: str, metadata: dict) -> None:
        self.id = mid
        self.content = content
        self.kind = "shared_moment"
        self.metadata = metadata
        self.salience = 0.7
        self.pinned = False
        self.created_at = "2026-01-05T20:00:00+00:00"
        self.embedding = None


class _MemoryStoreStub:
    def __init__(self) -> None:
        self.rows: dict[int, _MemStub] = {}
        self.added: list[dict] = []
        self.updated: list[dict] = []
        self._next_id = 500

    def add(self, *, content, kind, embedding, metadata, **kwargs):
        self.added.append(
            {"content": content, "kind": kind, "metadata": metadata}
        )
        mid = self._next_id
        self._next_id += 1
        row = _MemStub(mid, content, dict(metadata))
        row.embedding = embedding
        self.rows[mid] = row
        return row

    def get(self, mid: int):
        return self.rows.get(int(mid))

    def update(self, mid: int, *, content=None, metadata=None, **kwargs):
        row = self.rows.get(int(mid))
        if row is None:
            return None
        self.updated.append({"id": mid, "kwargs": kwargs})
        if content is not None:
            row.content = content
        if metadata is not None:
            row.metadata = dict(metadata)
        embedding = kwargs.get("embedding")
        if embedding is not None:
            row.embedding = embedding
        return row

    def set_pinned(self, mid: int, pinned: bool) -> None:
        row = self.rows.get(int(mid))
        if row is not None:
            row.pinned = bool(pinned)


class EmbeddingBasisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.embedder = _RecordingEmbedder()
        self.memories = _MemoryStoreStub()
        self.store = SharedMomentsStore(
            memory_store=self.memories, embedder=self.embedder
        )

    def test_add_embeds_the_summary_not_the_prefixed_content(self) -> None:
        row = self.store.add(
            summary="we finally shipped voice mode", vibe="milestone"
        )
        self.assertIsNotNone(row)
        self.assertEqual(
            self.embedder.texts, ["we finally shipped voice mode"]
        )
        # The rendered content is unchanged -- it is what the prompt shows.
        self.assertEqual(
            self.memories.added[0]["content"],
            "Shared moment (milestone): we finally shipped voice mode",
        )

    def test_vibe_never_reaches_the_vector(self) -> None:
        # Two moments about different things that happen to share a vibe must
        # not be handed identical-prefixed text; the vibe travels as a field.
        self.store.add(summary="we rebuilt the memory system", vibe="tender")
        self.store.add(summary="we argued about interruptions", vibe="tender")
        for text in self.embedder.texts:
            self.assertNotIn("Shared moment", text)
            self.assertNotIn("tender", text)
        self.assertEqual(
            [row["metadata"]["vibe"] for row in self.memories.added],
            ["tender", "tender"],
        )

    def test_update_re_embeds_the_new_summary(self) -> None:
        row = self.store.add(summary="an early rough draft", vibe="warm")
        self.embedder.texts.clear()
        updated = self.store.update(row.id, summary="the polished retelling")
        self.assertIsNotNone(updated)
        self.assertEqual(self.embedder.texts, ["the polished retelling"])
        self.assertEqual(
            self.memories.rows[row.id].content,
            "Shared moment (warm): the polished retelling",
        )

    def test_update_without_a_new_summary_does_not_re_embed(self) -> None:
        row = self.store.add(summary="something that happened", vibe="warm")
        self.embedder.texts.clear()
        self.store.update(row.id, vibe="playful")
        self.assertEqual(self.embedder.texts, [])


if __name__ == "__main__":
    unittest.main()

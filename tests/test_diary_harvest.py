"""Tests for the H9 ``[[diary:...]]`` harvest in :class:`TurnRunner`.

Exercises ``TurnRunner._extract_diary_memories`` directly with a
lightweight fake ``self`` (memory store + embedder), avoiding the full
turn machinery. Confirms diary tags become ``kind="diary"`` rows on the
durable tier with dedupe disabled, and that the short-body / duplicate
gates hold.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.session.turn_runner import TurnRunner


class _FakeEmbedder:
    def embed(self, text: str) -> np.ndarray:
        return np.ones(4, dtype=np.float32)


class _FakeStore:
    def __init__(self) -> None:
        self.adds: list[dict[str, Any]] = []
        self._next = 1

    def add(self, **kwargs: Any) -> Any:
        self.adds.append(kwargs)
        mem = SimpleNamespace(id=self._next, kind=kwargs.get("kind"), content=kwargs.get("content"))
        self._next += 1
        return mem


def _host(store: Any, embedder: Any) -> SimpleNamespace:
    added: list[Any] = []
    return SimpleNamespace(
        _memory_store=store,
        _embedder=embedder,
        _self_tagged_salience=0.7,
        _on_memory_added=added.append,
        _added=added,
    )


def _run(host: SimpleNamespace, raw: str) -> None:
    TurnRunner._extract_diary_memories(
        host, raw, session_key="user:s1", assistant_message_id=42,
    )


class DiaryHarvestTests(unittest.TestCase):
    def test_writes_a_diary_memory(self) -> None:
        store = _FakeStore()
        host = _host(store, _FakeEmbedder())
        _run(host, "[[diary:Today felt warm and a little quiet.]]")
        self.assertEqual(len(store.adds), 1)
        call = store.adds[0]
        self.assertEqual(call["kind"], "diary")
        self.assertEqual(call["content"], "Today felt warm and a little quiet.")
        self.assertEqual(call["tier"], "long_term")
        self.assertTrue(call["skip_dedupe"])
        self.assertEqual(call["source_message_id"], 42)
        # The memory_added listener fired so the UI updates live.
        self.assertEqual(len(host._added), 1)

    def test_multiple_distinct_entries(self) -> None:
        store = _FakeStore()
        host = _host(store, _FakeEmbedder())
        _run(host, "[[diary:first real entry here]] and [[diary:second real entry here]]")
        self.assertEqual(len(store.adds), 2)

    def test_duplicate_entries_collapse_within_turn(self) -> None:
        store = _FakeStore()
        host = _host(store, _FakeEmbedder())
        _run(host, "[[diary:same exact words]] ... [[diary:same exact words]]")
        self.assertEqual(len(store.adds), 1)

    def test_short_body_is_skipped(self) -> None:
        store = _FakeStore()
        host = _host(store, _FakeEmbedder())
        _run(host, "[[diary:hi]]")
        self.assertEqual(store.adds, [])

    def test_no_store_is_noop(self) -> None:
        host = _host(None, _FakeEmbedder())
        # Must not raise.
        _run(host, "[[diary:a perfectly fine entry]]")

    def test_no_tags_is_noop(self) -> None:
        store = _FakeStore()
        host = _host(store, _FakeEmbedder())
        _run(host, "just a normal reply with no diary tag")
        self.assertEqual(store.adds, [])


if __name__ == "__main__":
    unittest.main()

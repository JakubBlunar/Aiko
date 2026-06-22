"""Controller-level tests for the K61 knowledge-grounding provider.

Exercises ``InnerLifeProvidersMixin._render_knowledge_grounding_block``
via a minimal stub host that simulates the controller surface the
provider reads (settings + memory store + embedder). The block fires
only on informational ("question") turns when Aiko has learned facts
(``knowledge`` / ``curiosity_finding`` rows) topically close to the
question, and nudges her to commit to specifics instead of hedging.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)


# ── fixtures ────────────────────────────────────────────────────────


@dataclass
class _FakeMemory:
    content: str
    kind: str
    embedding: np.ndarray


class _FakeMemoryStore:
    def __init__(self, rows: dict[str, list[_FakeMemory]] | None = None) -> None:
        self._rows = rows or {}
        self.raise_on_iter = False

    def iter_by_kind(self, kind: str) -> list[_FakeMemory]:
        if self.raise_on_iter:
            raise RuntimeError("boom")
        return list(self._rows.get(kind, []))


class _DictEmbedder:
    """Maps known text to explicit vectors; unknown text → zero vector."""

    def __init__(self, vecs: dict[str, np.ndarray]) -> None:
        self._vecs = vecs
        self.raise_on_embed = False

    def embed(self, text: str) -> np.ndarray:
        if self.raise_on_embed:
            raise RuntimeError("embed failed")
        return self._vecs.get(text, np.zeros(3, dtype=np.float32))


def _agent(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(knowledge_grounding_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory_settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        knowledge_grounding_min_similarity=0.45,
        knowledge_grounding_max_items=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _FakeSettings:
    agent: SimpleNamespace


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        memory_store: _FakeMemoryStore | None,
        embedder: _DictEmbedder | None,
        agent_settings: SimpleNamespace | None = None,
        mem_settings: SimpleNamespace | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._settings = _FakeSettings(agent=agent_settings or _agent())
        self._memory_settings = mem_settings or _memory_settings()


# A unit vector and an orthogonal one so cosine is exactly controllable.
_NEAR = np.array([1.0, 0.0, 0.0], dtype=np.float32)
_FAR = np.array([0.0, 1.0, 0.0], dtype=np.float32)

_QUESTION = "what is a good dark roast coffee?"
_STATEMENT = "I had a great coffee today."


def _store_with_near_knowledge() -> _FakeMemoryStore:
    return _FakeMemoryStore(
        {
            "knowledge": [
                _FakeMemory(
                    "Italian roast is one of the darkest roast levels.",
                    "knowledge",
                    _NEAR,
                ),
            ],
            "curiosity_finding": [],
        }
    )


def _embedder() -> _DictEmbedder:
    return _DictEmbedder({_QUESTION: _NEAR, _STATEMENT: _NEAR})


# ── tests ───────────────────────────────────────────────────────────


class KnowledgeGroundingProviderTests(unittest.TestCase):
    def test_fires_on_question_with_close_knowledge(self) -> None:
        host = _Host(
            memory_store=_store_with_near_knowledge(),
            embedder=_embedder(),
        )
        block = host._render_knowledge_grounding_block(_QUESTION)
        self.assertNotEqual(block, "")
        self.assertIn("Italian roast", block)
        self.assertIn("commit", block.lower())

    def test_master_switch_off(self) -> None:
        host = _Host(
            memory_store=_store_with_near_knowledge(),
            embedder=_embedder(),
            agent_settings=_agent(knowledge_grounding_enabled=False),
        )
        self.assertEqual(host._render_knowledge_grounding_block(_QUESTION), "")

    def test_silent_on_non_question(self) -> None:
        host = _Host(
            memory_store=_store_with_near_knowledge(),
            embedder=_embedder(),
        )
        self.assertEqual(
            host._render_knowledge_grounding_block(_STATEMENT), ""
        )

    def test_silent_when_no_learned_rows(self) -> None:
        host = _Host(
            memory_store=_FakeMemoryStore({}),
            embedder=_embedder(),
        )
        self.assertEqual(
            host._render_knowledge_grounding_block(_QUESTION), ""
        )

    def test_silent_when_below_threshold(self) -> None:
        store = _FakeMemoryStore(
            {
                "knowledge": [
                    _FakeMemory("Unrelated fact.", "knowledge", _FAR),
                ],
            }
        )
        host = _Host(memory_store=store, embedder=_embedder())
        self.assertEqual(
            host._render_knowledge_grounding_block(_QUESTION), ""
        )

    def test_respects_max_items(self) -> None:
        store = _FakeMemoryStore(
            {
                "knowledge": [
                    _FakeMemory("Fact one about roast.", "knowledge", _NEAR),
                    _FakeMemory("Fact two about roast.", "knowledge", _NEAR),
                    _FakeMemory("Fact three about roast.", "knowledge", _NEAR),
                ],
            }
        )
        host = _Host(
            memory_store=store,
            embedder=_embedder(),
            mem_settings=_memory_settings(knowledge_grounding_max_items=2),
        )
        block = host._render_knowledge_grounding_block(_QUESTION)
        self.assertEqual(block.count("\n- "), 2)

    def test_dedupes_identical_content(self) -> None:
        store = _FakeMemoryStore(
            {
                "knowledge": [
                    _FakeMemory("Same fact.", "knowledge", _NEAR),
                ],
                "curiosity_finding": [
                    _FakeMemory("Same fact.", "curiosity_finding", _NEAR),
                ],
            }
        )
        host = _Host(memory_store=store, embedder=_embedder())
        block = host._render_knowledge_grounding_block(_QUESTION)
        self.assertEqual(block.count("- Same fact."), 1)

    def test_includes_curiosity_findings(self) -> None:
        store = _FakeMemoryStore(
            {
                "curiosity_finding": [
                    _FakeMemory(
                        "The violin originated in northern Italy.",
                        "curiosity_finding",
                        _NEAR,
                    ),
                ],
            }
        )
        host = _Host(memory_store=store, embedder=_embedder())
        block = host._render_knowledge_grounding_block(_QUESTION)
        self.assertIn("violin", block)

    def test_embed_failure_swallowed(self) -> None:
        embedder = _embedder()
        embedder.raise_on_embed = True
        host = _Host(
            memory_store=_store_with_near_knowledge(), embedder=embedder,
        )
        self.assertEqual(
            host._render_knowledge_grounding_block(_QUESTION), ""
        )

    def test_missing_store_or_embedder(self) -> None:
        self.assertEqual(
            _Host(
                memory_store=None, embedder=_embedder(),
            )._render_knowledge_grounding_block(_QUESTION),
            "",
        )
        self.assertEqual(
            _Host(
                memory_store=_store_with_near_knowledge(), embedder=None,
            )._render_knowledge_grounding_block(_QUESTION),
            "",
        )

    def test_iter_failure_swallowed(self) -> None:
        store = _store_with_near_knowledge()
        store.raise_on_iter = True
        host = _Host(memory_store=store, embedder=_embedder())
        self.assertEqual(
            host._render_knowledge_grounding_block(_QUESTION), ""
        )

    def test_short_text_ignored(self) -> None:
        host = _Host(
            memory_store=_store_with_near_knowledge(),
            embedder=_embedder(),
        )
        self.assertEqual(host._render_knowledge_grounding_block("hi?"), "")


if __name__ == "__main__":
    unittest.main()

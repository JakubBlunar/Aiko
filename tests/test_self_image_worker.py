"""Tests for the daily self-image pulse worker (Phase 2d)."""
from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.persona.self_image_worker import SelfImageWorker, _clean_paragraph


class _FakeMemory:
    def __init__(self, content: str, kind: str, salience: float = 0.5, use_count: int = 0) -> None:
        self.content = content
        self.kind = kind
        self.salience = salience
        self.use_count = use_count
        self.embedding = np.zeros(8, dtype=np.float32)


class _FakeMemoryStore:
    def __init__(self, memories: list[_FakeMemory]) -> None:
        self._memories = memories

    def list_top(self, limit: int = 50):
        ordered = sorted(
            self._memories,
            key=lambda m: (m.salience, m.use_count),
            reverse=True,
        )
        return ordered[: max(1, int(limit))]


class _FakeOllama:
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[dict] = []
        self.fail = False

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options, "model": model})
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return self.response


class CleanParagraphTests(unittest.TestCase):
    def test_strips_code_fence(self) -> None:
        out = _clean_paragraph("```\nhello world\n```")
        self.assertEqual(out, "hello world")

    def test_strips_language_fence(self) -> None:
        out = _clean_paragraph("```text\nhello world\n```")
        self.assertEqual(out, "hello world")

    def test_collapses_blank_lines(self) -> None:
        out = _clean_paragraph("a\n\n\n\nb")
        self.assertEqual(out, "a\n\nb")

    def test_returns_empty_for_blank(self) -> None:
        self.assertEqual(_clean_paragraph(""), "")
        self.assertEqual(_clean_paragraph("   \n  "), "")


class SelfImageWorkerTests(unittest.TestCase):
    def _make(self, response: str, memories: list[_FakeMemory] | None = None, **overrides):
        ollama = _FakeOllama(response=response)
        store = _FakeMemoryStore(memories or [])
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "self_image.txt"
            kwargs = {
                "ollama": ollama,
                "memory_store": store,
                "target_path": target,
                "model": "m",
                "min_hours_between": 24.0,
            }
            kwargs.update(overrides)
            yield ollama, store, target, SelfImageWorker(**kwargs)

    def test_should_run_when_missing(self) -> None:
        for ollama, store, target, worker in self._make("ok"):
            self.assertTrue(worker.should_run())

    def test_should_not_run_when_recent(self) -> None:
        for ollama, store, target, worker in self._make("ok"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("recent text")
            # Ensure mtime is "now" (some FS clocks are coarse).
            now = time.time()
            os.utime(target, (now, now))
            self.assertFalse(worker.should_run())

    def test_should_run_when_stale(self) -> None:
        for ollama, store, target, worker in self._make("ok", min_hours_between=1.0):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old text")
            old = time.time() - 3600 * 5
            os.utime(target, (old, old))
            self.assertTrue(worker.should_run())

    def test_pulse_writes_paragraph(self) -> None:
        memories = [
            _FakeMemory("I prefer warm conversations", "self", salience=0.9),
            _FakeMemory("I'm trying to be more direct", "self", salience=0.7),
            _FakeMemory("I noticed I'm patient with retries", "reflection", salience=0.6),
        ]
        for ollama, store, target, worker in self._make(
            "I see myself as warm and trying to be more direct lately.",
            memories=memories,
            min_hours_between=24.0,
        ):
            text = worker.pulse()
            self.assertIsNotNone(text)
            assert text is not None
            self.assertIn("warm", text)
            self.assertTrue(target.exists())
            self.assertTrue(target.read_text(encoding="utf-8").startswith(text[:20]))
            self.assertEqual(worker.stats()["completed"], 1)
            # The user-content should contain bullets from BOTH self + reflection.
            user_msg = next(
                m for m in ollama.calls[0]["messages"] if m["role"] == "user"
            )
            self.assertIn("warm conversations", user_msg["content"])
            self.assertIn("patient with retries", user_msg["content"])

    def test_pulse_skips_when_recent(self) -> None:
        memories = [_FakeMemory("self note", "self", salience=0.8)]
        for ollama, store, target, worker in self._make(
            "ok",
            memories=memories,
            min_hours_between=24.0,
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("recent paragraph")
            now = time.time()
            os.utime(target, (now, now))
            text = worker.pulse()
            self.assertIsNone(text)
            self.assertEqual(worker.stats()["skipped_recent"], 1)
            self.assertEqual(ollama.calls, [])

    def test_pulse_skips_with_no_memories(self) -> None:
        for ollama, store, target, worker in self._make("ok", memories=[]):
            text = worker.pulse()
            self.assertIsNone(text)
            self.assertEqual(worker.stats()["skipped_no_input"], 1)
            self.assertEqual(ollama.calls, [])
            self.assertFalse(target.exists())

    def test_failure_does_not_crash(self) -> None:
        memories = [_FakeMemory("self note one liner", "self", salience=0.8)]
        for ollama, store, target, worker in self._make(
            "anything",
            memories=memories,
            min_hours_between=24.0,
        ):
            ollama.fail = True
            text = worker.pulse()
            self.assertIsNone(text)
            self.assertEqual(worker.stats()["failed"], 1)
            self.assertFalse(target.exists())

    def test_pulse_filters_kinds(self) -> None:
        memories = [
            _FakeMemory("Jacob loves coffee", "fact", salience=0.95),  # not self
            _FakeMemory("I take pride in my work", "self", salience=0.7),
            _FakeMemory("Last week's reflection content here", "reflection", salience=0.6),
        ]
        for ollama, store, target, worker in self._make(
            "ok paragraph",
            memories=memories,
            min_hours_between=24.0,
        ):
            worker.pulse()
            user_msg = next(
                m for m in ollama.calls[0]["messages"] if m["role"] == "user"
            )
            self.assertIn("pride in my work", user_msg["content"])
            self.assertIn("reflection content", user_msg["content"])
            self.assertNotIn("loves coffee", user_msg["content"])


class _FakeInterest:
    """Mimics topic_graph.InterestEntry (has .label / .size)."""

    def __init__(self, label: str, size: int = 5) -> None:
        self.label = label
        self.size = size


class InterestSeedTests(unittest.TestCase):
    """K65d: seed the self-image pulse from the K9 interest map."""

    def _make(self, response, memories, **overrides):
        ollama = _FakeOllama(response=response)
        store = _FakeMemoryStore(memories or [])
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "self_image.txt"
            kwargs = {
                "ollama": ollama,
                "memory_store": store,
                "target_path": target,
                "model": "m",
                "min_hours_between": 24.0,
            }
            kwargs.update(overrides)
            yield ollama, target, SelfImageWorker(**kwargs)

    @staticmethod
    def _user(ollama: _FakeOllama) -> str:
        return next(
            m for m in ollama.calls[0]["messages"] if m["role"] == "user"
        )["content"]

    @staticmethod
    def _system(ollama: _FakeOllama) -> str:
        return next(
            m for m in ollama.calls[0]["messages"] if m["role"] == "system"
        )["content"]

    def test_interest_line_lands_in_prompt(self) -> None:
        memories = [_FakeMemory("I value warmth", "self", salience=0.9)]
        for ollama, _t, worker in self._make(
            "I'm warm and lately drawn to rust.",
            memories,
            interest_provider=lambda: [
                _FakeInterest("rust programming", 9),
                _FakeInterest("rock climbing", 5),
            ],
        ):
            worker.pulse()
            user = self._user(ollama)
            self.assertIn("Lately you've been spending time on:", user)
            self.assertIn("rust programming", user)
            self.assertIn("rock climbing", user)
            # System prompt grew the interest rule.
            self.assertIn("Lately you've been spending time on", self._system(ollama))

    def test_no_provider_no_interest_line(self) -> None:
        memories = [_FakeMemory("I value warmth", "self", salience=0.9)]
        for ollama, _t, worker in self._make("ok", memories):
            worker.pulse()
            self.assertNotIn(
                "Lately you've been spending time on:", self._user(ollama)
            )

    def test_seed_disabled_suppresses_interest_line(self) -> None:
        memories = [_FakeMemory("I value warmth", "self", salience=0.9)]
        for ollama, _t, worker in self._make(
            "ok",
            memories,
            interest_provider=lambda: [_FakeInterest("rust programming", 9)],
            interest_seed_enabled=False,
        ):
            worker.pulse()
            self.assertNotIn(
                "Lately you've been spending time on:", self._user(ollama)
            )

    def test_interest_alone_does_not_trigger_without_memories(self) -> None:
        # No self/reflection memories -> still skipped, interest map is a
        # flavour not an input source.
        for ollama, target, worker in self._make(
            "ok",
            [],
            interest_provider=lambda: [_FakeInterest("rust programming", 9)],
        ):
            text = worker.pulse()
            self.assertIsNone(text)
            self.assertEqual(worker.stats()["skipped_no_input"], 1)
            self.assertEqual(ollama.calls, [])


class _FakeConcept:
    def __init__(self, label: str, confidence: float = 0.8) -> None:
        self.label = label
        self.confidence = confidence
        self.subject = "aiko"
        self.kind = "identity"


class _FakeConceptView:
    """Stand-in for ConceptView.core used by the self-image worker."""

    def __init__(self, concepts: list[_FakeConcept]) -> None:
        self._concepts = concepts
        self.calls: list[dict] = []

    def core(self, *, subject=None, kind=None, min_confidence=0.0, limit=None):
        self.calls.append(
            {"subject": subject, "min_confidence": min_confidence, "limit": limit}
        )
        out = [
            c for c in self._concepts
            if c.subject == (subject or c.subject)
            and c.confidence >= float(min_confidence)
        ]
        out.sort(key=lambda c: c.confidence, reverse=True)
        return out[: limit] if limit is not None else out


class ConceptSourcedTests(unittest.TestCase):
    """L24: the pulse narrates subject=aiko concepts when the layer is
    mature enough, falling back to raw memories otherwise."""

    def _make(self, response, memories, **overrides):
        ollama = _FakeOllama(response=response)
        store = _FakeMemoryStore(memories or [])
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "self_image.txt"
            kwargs = {
                "ollama": ollama,
                "memory_store": store,
                "target_path": target,
                "model": "m",
                "min_hours_between": 24.0,
            }
            kwargs.update(overrides)
            yield ollama, SelfImageWorker(**kwargs)

    @staticmethod
    def _user(ollama: _FakeOllama) -> str:
        return next(
            m for m in ollama.calls[0]["messages"] if m["role"] == "user"
        )["content"]

    def test_concept_path_used_when_mature(self) -> None:
        concepts = [
            _FakeConcept("I lead with curiosity", 0.9),
            _FakeConcept("I tease to soften hard truths", 0.85),
            _FakeConcept("I hold my ground on honesty", 0.8),
            _FakeConcept("I run warm but wary", 0.75),
        ]
        view = _FakeConceptView(concepts)
        mems = [_FakeMemory("raw self memory should be ignored", "self", 0.9)]
        for ollama, worker in self._make(
            "I'm curious and warm.",
            mems,
            concept_view_provider=lambda: view,
            min_concepts=4,
            min_concept_confidence=0.6,
        ):
            text = worker.pulse()
            self.assertIsNotNone(text)
            user = self._user(ollama)
            self.assertIn("I lead with curiosity", user)
            self.assertNotIn("raw self memory should be ignored", user)
            self.assertEqual(worker.stats()["source_concepts"], 1)
            self.assertEqual(worker.stats()["source_memories"], 0)
            # Asked the facade for aiko concepts under the confidence gate.
            self.assertEqual(view.calls[0]["subject"], "aiko")
            self.assertEqual(view.calls[0]["min_confidence"], 0.6)

    def test_falls_back_to_memories_when_too_few_concepts(self) -> None:
        view = _FakeConceptView([_FakeConcept("only one concept", 0.9)])
        mems = [
            _FakeMemory("I value slow mornings", "self", 0.9),
            _FakeMemory("reflection about patience", "reflection", 0.7),
        ]
        for ollama, worker in self._make(
            "warm paragraph",
            mems,
            concept_view_provider=lambda: view,
            min_concepts=4,
        ):
            text = worker.pulse()
            self.assertIsNotNone(text)
            user = self._user(ollama)
            self.assertIn("slow mornings", user)
            self.assertNotIn("only one concept", user)
            self.assertEqual(worker.stats()["source_memories"], 1)
            self.assertEqual(worker.stats()["source_concepts"], 0)

    def test_flag_off_forces_memory_path(self) -> None:
        concepts = [_FakeConcept(f"concept {i}", 0.9) for i in range(6)]
        view = _FakeConceptView(concepts)
        mems = [_FakeMemory("I value warmth", "self", 0.9)]
        for ollama, worker in self._make(
            "ok",
            mems,
            concept_view_provider=lambda: view,
            concept_sourced_enabled=False,
            min_concepts=4,
        ):
            worker.pulse()
            user = self._user(ollama)
            self.assertIn("I value warmth", user)
            self.assertNotIn("concept 0", user)
            self.assertEqual(view.calls, [])

    def test_interest_seed_preserved_on_concept_path(self) -> None:
        concepts = [_FakeConcept(f"self concept {i}", 0.9) for i in range(4)]
        view = _FakeConceptView(concepts)
        for ollama, worker in self._make(
            "I'm drawn to rust lately.",
            [],
            concept_view_provider=lambda: view,
            min_concepts=4,
            interest_provider=lambda: [_FakeInterest("rust programming", 9)],
        ):
            text = worker.pulse()
            self.assertIsNotNone(text)
            user = self._user(ollama)
            self.assertIn("self concept 0", user)
            self.assertIn("Lately you've been spending time on:", user)
            self.assertIn("rust programming", user)


if __name__ == "__main__":
    unittest.main()

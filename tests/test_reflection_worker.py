"""Tests for the post-turn ReflectionWorker (Phase 2c)."""
from __future__ import annotations

import time
import unittest
from dataclasses import dataclass

import numpy as np

from app.core.reflection_worker import (
    Reflection,
    ReflectionWorker,
    _parse_reflection_payload,
)


@dataclass(slots=True)
class _FakeAffect:
    valence: float = 0.0
    arousal: float = 0.4
    mood_label: str = "content"


class _FakeOllama:
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.fail = False

    def chat(self, messages, options=None, model=None):  # pragma: no cover - thin
        self.calls.append({
            "messages": messages,
            "options": options,
            "model": model,
        })
        if self.fail:
            raise RuntimeError("simulated LLM failure")
        return self.response


@dataclass(slots=True)
class _FakeMemory:
    id: int
    content: str
    kind: str


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._next_id = 1
        self.fail_kind: str | None = None

    def add(
        self,
        *,
        content: str,
        kind: str,
        embedding,
        salience: float,
        source_session=None,
        source_message_id=None,
    ):
        self.calls.append({
            "content": content,
            "kind": kind,
            "salience": salience,
            "source_session": source_session,
        })
        if self.fail_kind == kind:
            return None
        mem = _FakeMemory(id=self._next_id, content=content, kind=kind)
        self._next_id += 1
        return mem


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str):
        self.calls.append(text)
        return np.zeros(8, dtype=np.float32)


class ParseReflectionTests(unittest.TestCase):
    def test_parses_clean_json(self) -> None:
        raw = (
            '{"observation":"Jacob seems excited about the trip.",'
            '"open_questions":["Where would he like to go first?"],'
            '"callbacks":["Ask about packing tomorrow"]}'
        )
        out = _parse_reflection_payload(raw)
        self.assertIn("excited", out.observation)
        self.assertEqual(out.open_questions, ["Where would he like to go first?"])
        self.assertEqual(out.callbacks, ["Ask about packing tomorrow"])

    def test_parses_fenced_json(self) -> None:
        raw = "```json\n{\"observation\":\"hello there\",\"open_questions\":[],\"callbacks\":[]}\n```"
        out = _parse_reflection_payload(raw)
        self.assertEqual(out.observation, "hello there")
        self.assertEqual(out.open_questions, [])

    def test_handles_prose_around_json(self) -> None:
        raw = "Sure thing!\n{\"observation\":\"hi\",\"open_questions\":[\"why?\"]}\nThanks."
        out = _parse_reflection_payload(raw)
        # "hi" is below 4 chars min for items, but observation has no min.
        self.assertEqual(out.observation, "hi")

    def test_caps_arrays(self) -> None:
        raw = (
            '{"observation":"x","open_questions":["one item","two item","three item","four item","five item"]}'
        )
        out = _parse_reflection_payload(raw)
        self.assertEqual(len(out.open_questions), 3)

    def test_drops_too_short_items(self) -> None:
        raw = '{"observation":"ok","open_questions":["x","this one is fine"]}'
        out = _parse_reflection_payload(raw)
        self.assertEqual(out.open_questions, ["this one is fine"])

    def test_returns_empty_on_garbage(self) -> None:
        out = _parse_reflection_payload("totally not JSON")
        self.assertTrue(out.is_empty())

    def test_returns_empty_on_non_object(self) -> None:
        out = _parse_reflection_payload("[1,2,3]")
        self.assertTrue(out.is_empty())


class ReflectionWorkerTests(unittest.TestCase):
    def _make_worker(self, response: str, **overrides):
        ollama = _FakeOllama(response=response)
        store = _FakeMemoryStore()
        embedder = _FakeEmbedder()
        kwargs = {
            "ollama": ollama,
            "memory_store": store,
            "embedder": embedder,
            "model": "m",
            "min_seconds_between": 0.0,
            "emotional_delta_threshold": 0.0,
        }
        kwargs.update(overrides)
        worker = ReflectionWorker(**kwargs)
        return worker, ollama, store, embedder

    def test_runs_and_persists_memories(self) -> None:
        worker, ollama, store, embedder = self._make_worker(
            response='{"observation":"Stood out: a trip plan.","open_questions":["When does he leave?"],"callbacks":["Check on packing"]}',
        )
        result = worker.maybe_run(
            session_key="s",
            user_text="I'm going to Paris next week",
            assistant_text="That's exciting!",
            reaction="excited",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.open_questions, ["When does he leave?"])
        kinds = [c["kind"] for c in store.calls]
        self.assertEqual(kinds, ["reflection", "open_question", "callback"])
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(len(result.persisted_memory_ids), 3)

    def test_throttled_within_window(self) -> None:
        worker, ollama, _store, _emb = self._make_worker(
            response='{"observation":"x","open_questions":[],"callbacks":[]}',
            min_seconds_between=10.0,
        )
        # First call goes through.
        first = worker.maybe_run(
            session_key="s", user_text="u", assistant_text="a", reaction="warm",
        )
        self.assertIsNotNone(first)
        # Second call within the throttle window is skipped.
        second = worker.maybe_run(
            session_key="s", user_text="u2", assistant_text="a2", reaction="warm",
        )
        self.assertIsNone(second)
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(worker.stats()["skipped_recent"], 1)

    def test_skips_on_flat_affect(self) -> None:
        worker, ollama, _store, _emb = self._make_worker(
            response='{"observation":"x"}',
            emotional_delta_threshold=0.5,
        )
        before = _FakeAffect(valence=0.0, arousal=0.4)
        after = _FakeAffect(valence=0.01, arousal=0.41)
        result = worker.maybe_run(
            session_key="s",
            user_text="u",
            assistant_text="a",
            reaction="neutral",
            affect_before=before,
            affect_after=after,
        )
        self.assertIsNone(result)
        self.assertEqual(len(ollama.calls), 0)
        self.assertEqual(worker.stats()["skipped_flat"], 1)

    def test_failure_does_not_raise(self) -> None:
        worker, ollama, _store, _emb = self._make_worker(response="{}")
        ollama.fail = True
        result = worker.maybe_run(
            session_key="s", user_text="u", assistant_text="a", reaction="neutral",
        )
        self.assertIsNone(result)
        self.assertEqual(worker.stats()["failed"], 1)

    def test_no_memory_store_skips_persistence(self) -> None:
        ollama = _FakeOllama(response='{"observation":"x","open_questions":["q1q1q1"]}')
        worker = ReflectionWorker(
            ollama=ollama,
            memory_store=None,
            embedder=None,
            model="m",
            min_seconds_between=0.0,
            emotional_delta_threshold=0.0,
        )
        result = worker.maybe_run(
            session_key="s", user_text="u", assistant_text="a", reaction="warm",
        )
        self.assertIsNotNone(result)
        assert result is not None
        # Returned the parsed reflection but persisted nothing.
        self.assertEqual(result.open_questions, ["q1q1q1"])
        self.assertEqual(result.persisted_memory_ids, [])

    def test_dedupe_returns_none_does_not_break(self) -> None:
        worker, _ollama, store, _emb = self._make_worker(
            response='{"observation":"observe","open_questions":["question one"]}',
        )
        store.fail_kind = "open_question"  # add() returns None for that kind
        result = worker.maybe_run(
            session_key="s", user_text="u", assistant_text="a", reaction="neutral",
        )
        self.assertIsNotNone(result)
        assert result is not None
        # The reflection memory still went through; the open_question got
        # deduped (None) and was not counted.
        self.assertEqual(len(result.persisted_memory_ids), 1)


if __name__ == "__main__":
    unittest.main()

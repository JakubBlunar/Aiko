"""Tests for the G3 idle curiosity worker.

The worker resolves Aiko's existing ``open_question`` memories by
scrubbing them, web-searching for snippets, distilling a short
answer with the LLM, and writing the result as a high-confidence
``curiosity_finding`` memory linked back to the question.

We exercise the full flow against a real :class:`MemoryStore`
backed by SQLite so the metadata stamps land in actual rows; only
the LLM and the web-search tool are stubbed.

Coverage targets:

* Privacy gate redirects: a question that won't scrub stops being
  picked up (cooldown stamp) instead of looping forever.
* Inconclusive path: low-confidence distil leaves no
  ``curiosity_finding`` memory but stamps the source.
* Success path: high-confidence distil writes the finding, links it
  via metadata, and stamps the source as resolved.
* Question selection: oldest-first ordering, cooldowns honored,
  resolved questions skipped.
* Rate limiter integration: the worker consumes one token per run.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.proactive.idle_curiosity_worker import (
    IdleCuriosityWorker,
    CuriosityAnswer,
)
from app.core.memory.memory_store import MemoryStore


# ── tiny stubs ─────────────────────────────────────────────────────────


class _DeterministicEmbedder:
    """Token-slot embedder. Uses md5 instead of ``hash()`` so the same
    token always maps to the same slot regardless of ``PYTHONHASHSEED``.
    """

    DIM = 16

    @staticmethod
    def _slot(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % _DeterministicEmbedder.DIM

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            vec[self._slot(token)] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


@dataclass
class _StubWebSearch:
    payload: dict[str, Any] = field(
        default_factory=lambda: {
            "results": [
                {
                    "title": "Violin history",
                    "url": "https://en.example.org/violin",
                    "snippet": (
                        "The violin originated in northern Italy in the "
                        "early 16th century and quickly spread across "
                        "Europe."
                    ),
                },
            ],
        }
    )
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_call: bool = False

    def run(self, args: dict[str, Any]) -> str:
        self.calls.append(dict(args))
        if self.raise_on_call:
            raise RuntimeError("simulated search outage")
        return json.dumps(self.payload)


@dataclass
class _StubOllamaClient:
    answer_json: dict[str, Any] = field(
        default_factory=lambda: {
            "answer": (
                "The violin originated in northern Italy in the early "
                "16th century."
            ),
            "confidence": 0.85,
        }
    )
    raise_on_call: bool = False
    chat_calls: list[dict[str, Any]] = field(default_factory=list)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        *,
        model: str | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        format_json: bool = False,
        think: bool = False,
        **kwargs: Any,
    ) -> Iterable[str]:
        self.chat_calls.append(
            {
                "messages": messages,
                "model": model,
                "stop_event_set": bool(
                    stop_event and stop_event.is_set()
                ),
                "format_json": format_json,
            }
        )
        if self.raise_on_call:
            raise RuntimeError("simulated ollama outage")
        if stop_event is not None and stop_event.is_set():
            return
        yield json.dumps(self.answer_json)


@dataclass
class _StubAgent:
    idle_curiosity_enabled: bool = True
    idle_curiosity_per_hour_cap: int = 5
    idle_curiosity_per_day_cap: int = 20


@dataclass
class _StubMemorySettings:
    idle_curiosity_interval_seconds: int = 1800


# ── shared fixture ─────────────────────────────────────────────────────


def _build_world(
    *,
    enabled: bool = True,
    answer_json: dict[str, Any] | None = None,
    user_names: list[str] | None = None,
    raise_search: bool = False,
) -> dict[str, Any]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    rate_limiter = FactCheckRateLimiter(
        chat_db,
        per_hour_cap=5,
        per_day_cap=20,
        state_key="idle_curiosity.rate_state",
    )
    web_search = _StubWebSearch(raise_on_call=raise_search)
    ollama = _StubOllamaClient(
        answer_json=answer_json
        or {
            "answer": (
                "The violin originated in northern Italy in the early "
                "16th century."
            ),
            "confidence": 0.85,
        },
    )
    cancel_event = threading.Event()

    added_calls: list[dict[str, Any]] = []
    updated_calls: list[dict[str, Any]] = []

    worker = IdleCuriosityWorker(
        memory_store=memory_store,
        embedder=embedder,
        ollama=ollama,
        chat_model="stub-model",
        web_search_tool=web_search,
        rate_limiter=rate_limiter,
        cancel_event=cancel_event,
        agent_settings=_StubAgent(idle_curiosity_enabled=enabled),
        memory_settings=_StubMemorySettings(),
        user_names_provider=(lambda: user_names or []),
        assistant_name_provider=(lambda: None),
        notify_memory_added=lambda d: added_calls.append(d),
        notify_memory_updated=lambda d: updated_calls.append(d),
    )
    return {
        "chat_db": chat_db,
        "memory_store": memory_store,
        "embedder": embedder,
        "rate_limiter": rate_limiter,
        "web_search": web_search,
        "ollama": ollama,
        "cancel_event": cancel_event,
        "worker": worker,
        "added_calls": added_calls,
        "updated_calls": updated_calls,
    }


def _seed_open_question(
    memory_store: MemoryStore,
    embedder: _DeterministicEmbedder,
    text: str,
) -> int:
    """Insert a plain ``open_question`` row and return its id."""
    mem = memory_store.add(
        text,
        "open_question",
        embedder.embed(text),
        salience=0.55,
    )
    assert mem is not None
    return int(mem.id)


# ── _parse_answer ──────────────────────────────────────────────────────


class TestParseAnswer(unittest.TestCase):
    def test_parses_clean_json(self) -> None:
        parsed = IdleCuriosityWorker._parse_answer(
            json.dumps({"answer": "Some fact.", "confidence": 0.7}),
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.answer, "Some fact.")
        self.assertAlmostEqual(parsed.confidence, 0.7)

    def test_parses_json_in_prose(self) -> None:
        raw = (
            "Sure! Here's the answer:\n"
            '{"answer": "X", "confidence": 0.4}\n'
            "Hope that helps."
        )
        parsed = IdleCuriosityWorker._parse_answer(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.answer, "X")

    def test_truncates_long_answer(self) -> None:
        long_answer = "word " * 100  # ~500 chars
        parsed = IdleCuriosityWorker._parse_answer(
            json.dumps({"answer": long_answer, "confidence": 0.9}),
        )
        assert parsed is not None
        self.assertLessEqual(len(parsed.answer), 240)
        self.assertTrue(parsed.answer.endswith("…"))

    def test_clamps_confidence(self) -> None:
        parsed = IdleCuriosityWorker._parse_answer(
            json.dumps({"answer": "x", "confidence": 5.0}),
        )
        assert parsed is not None
        self.assertEqual(parsed.confidence, 1.0)

    def test_returns_none_on_garbage(self) -> None:
        self.assertIsNone(IdleCuriosityWorker._parse_answer("nope"))


# ── question selection ────────────────────────────────────────────────


class TestQuestionSelection(unittest.TestCase):
    def test_picks_oldest_unresolved(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        first = _seed_open_question(
            store, embedder, "How does sourdough work?",
        )
        second = _seed_open_question(
            store, embedder, "When did the violin originate?",
        )
        # Resolve the first one so the second becomes the candidate.
        store.update(
            first,
            metadata={"curiosity_resolved_at": "2026-05-20T00:00:00+00:00"},
            metadata_merge=True,
        )
        chosen = world["worker"]._pick_next_question(
            now=datetime.now(timezone.utc),
        )
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(int(chosen.id), second)

    def test_skips_recently_skipped(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        only = _seed_open_question(
            store, embedder, "How does sourdough work?",
        )
        now = datetime.now(timezone.utc)
        store.update(
            only,
            metadata={
                "curiosity_skipped": "privacy",
                "curiosity_skipped_at": (
                    now - timedelta(days=1)
                ).isoformat(),
            },
            metadata_merge=True,
        )
        chosen = world["worker"]._pick_next_question(now=now)
        self.assertIsNone(chosen)

    def test_picks_after_cooldown_expires(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        only = _seed_open_question(
            store, embedder, "How does sourdough work?",
        )
        now = datetime.now(timezone.utc)
        store.update(
            only,
            metadata={
                "curiosity_inconclusive_at": (
                    now - timedelta(days=10)
                ).isoformat(),
            },
            metadata_merge=True,
        )
        chosen = world["worker"]._pick_next_question(now=now)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(int(chosen.id), only)


# ── end-to-end ────────────────────────────────────────────────────────


class TestRunSuccessPath(unittest.TestCase):
    def test_writes_finding_and_stamps_source(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        question_id = _seed_open_question(
            store, embedder, "When did the violin originate?",
        )

        result = world["worker"].run()
        self.assertEqual(result.get("outcome"), "resolved")
        self.assertEqual(int(result["memory_id"]), question_id)
        answer_id = int(result["answer_memory_id"])

        # Source question stamped with the resolved metadata.
        source = store.get(question_id)
        self.assertIsNotNone(source)
        assert source is not None
        self.assertIn("curiosity_resolved_at", source.metadata)
        self.assertEqual(
            int(source.metadata["curiosity_answer_memory_id"]),
            answer_id,
        )

        # Answer memory exists with the right kind + metadata link.
        answer = store.get(answer_id)
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertEqual(answer.kind, "curiosity_finding")
        self.assertEqual(
            int(answer.metadata.get("source_open_question_id", -1)),
            question_id,
        )
        self.assertIn("violin", answer.content.lower())
        self.assertLessEqual(answer.confidence, 0.9)
        # Web search query was the (unchanged) question text — no
        # PII stripping was needed for this short, generic question.
        self.assertEqual(len(world["web_search"].calls), 1)
        # Listener fired for both the new finding and the source update.
        self.assertEqual(len(world["added_calls"]), 1)
        self.assertGreaterEqual(len(world["updated_calls"]), 1)

    def test_consumes_one_rate_token(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        _seed_open_question(
            store, embedder, "When did the violin originate?",
        )
        before = world["rate_limiter"].snapshot()
        self.assertEqual(before["hour_used"], 0)
        world["worker"].run()
        after = world["rate_limiter"].snapshot()
        self.assertEqual(after["hour_used"], 1)


class TestRunInconclusive(unittest.TestCase):
    def test_low_confidence_writes_no_finding(self) -> None:
        world = _build_world(
            answer_json={"answer": "Maybe.", "confidence": 0.3},
        )
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        question_id = _seed_open_question(
            store, embedder, "How does sourdough work?",
        )

        result = world["worker"].run()
        self.assertEqual(result.get("outcome"), "inconclusive")
        self.assertEqual(int(result["memory_id"]), question_id)

        # Source stamped with the inconclusive marker; no
        # curiosity_finding row was added.
        source = store.get(question_id)
        assert source is not None
        self.assertIn("curiosity_inconclusive_at", source.metadata)
        self.assertEqual(
            source.metadata.get("curiosity_inconclusive_reason"),
            "low_confidence",
        )
        findings = store.iter_by_kind("curiosity_finding")
        self.assertEqual(findings, [])

    def test_no_search_results_marks_no_results(self) -> None:
        world = _build_world()
        # Empty search payload.
        world["web_search"].payload = {"results": []}
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        _seed_open_question(
            store, embedder, "How does sourdough work?",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("outcome"), "inconclusive")
        self.assertEqual(result.get("reason"), "no_results")
        # Distil should never have been called.
        self.assertEqual(len(world["ollama"].chat_calls), 0)


class TestRunPrivacy(unittest.TestCase):
    def test_email_in_question_marked_skipped(self) -> None:
        # The privacy scrubber hard-rejects emails (no safe redaction
        # exists for "what was X about"-style questions where the
        # only handle is an address). The worker is expected to
        # stamp a cooldown so subsequent ticks skip the row.
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        question_id = _seed_open_question(
            store,
            embedder,
            "what was the message from jacob@example.com about",
        )

        result = world["worker"].run()
        self.assertEqual(result.get("reason"), "privacy_gate")

        source = store.get(question_id)
        assert source is not None
        self.assertEqual(
            source.metadata.get("curiosity_skipped"), "privacy",
        )
        self.assertIn("curiosity_skipped_at", source.metadata)

        # Web search must not have been called.
        self.assertEqual(world["web_search"].calls, [])

    def test_skipped_question_not_picked_again(self) -> None:
        # End-to-end: after a privacy skip the same question must not
        # be retried on the next tick (this is what stops the worker
        # burning rate-limit tokens on a question it can never resolve).
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        _seed_open_question(
            store,
            embedder,
            "what was the message from jacob@example.com about",
        )
        first = world["worker"].run()
        self.assertEqual(first.get("reason"), "privacy_gate")
        second = world["worker"].run()
        # Without anything else queueable, the worker reports the
        # generic "no_unresolved_question" reason — proving the
        # skipped row dropped out of the candidate pool.
        self.assertEqual(second.get("reason"), "no_unresolved_question")


class TestRunGuards(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        world = _build_world(enabled=False)
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        _seed_open_question(
            store, embedder, "When did the violin originate?",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("reason"), "disabled")
        self.assertEqual(world["web_search"].calls, [])

    def test_no_question_returns_skipped(self) -> None:
        world = _build_world()
        result = world["worker"].run()
        self.assertEqual(result.get("reason"), "no_unresolved_question")

    def test_pre_set_cancel_aborts_before_pop(self) -> None:
        world = _build_world()
        world["cancel_event"].set()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        _seed_open_question(
            store, embedder, "When did the violin originate?",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("reason"), "cancelled_before_start")


class TestIsReady(unittest.TestCase):
    def test_disabled_not_ready(self) -> None:
        world = _build_world(enabled=False)
        ready = world["worker"].is_ready(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertFalse(ready)

    def test_no_question_not_ready(self) -> None:
        world = _build_world()
        ready = world["worker"].is_ready(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertFalse(ready)

    def test_ready_when_question_pending(self) -> None:
        world = _build_world()
        store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        _seed_open_question(
            store, embedder, "When did the violin originate?",
        )
        ready = world["worker"].is_ready(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()

"""Tests for :mod:`app.core.memory.promise_worker`."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.memory.promise_extractor import Promise
from app.core.memory.promise_worker import (
    PromiseExtractionWorker,
    _is_low_quality,
)


class _StubEmbedder:
    def embed(self, text: str) -> np.ndarray:
        return np.zeros(8, dtype=np.float32)


@dataclass
class _StubOllama:
    """Yields one pre-canned JSON-array response per chat_stream call."""

    responses: list[str] = field(default_factory=list)
    chat_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_call: bool = False

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
        self.chat_calls.append({
            "messages": messages,
            "format_json": format_json,
        })
        if self.raise_on_call:
            raise RuntimeError("simulated ollama outage")
        if not self.responses:
            yield "[]"
            return
        yield self.responses.pop(0)


class _FakeMemory:
    def __init__(self, mid: int, content: str, metadata: dict[str, Any]) -> None:
        self.id = mid
        self.content = content
        self.kind = "promise"
        self.metadata = metadata


class _FakeMemoryStore:
    """Records ``add`` calls and serves seeded promise rows for dedupe."""

    def __init__(self, existing: list[_FakeMemory] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._existing = list(existing or [])
        self._next_id = 100

    def add(
        self,
        *,
        content,
        kind,
        embedding,
        salience=0.5,
        source_session=None,
        source_message_id=None,
        metadata=None,
        pinned=False,
        skip_dedupe=False,
        tier=None,
        confidence=None,
        event_time=None,
        temporal_type=None,
        relevance_until=None,
    ):
        self.calls.append({
            "content": content,
            "kind": kind,
            "salience": salience,
            "tier": tier,
            "confidence": confidence,
            "metadata": metadata,
        })
        mem = _FakeMemory(self._next_id, content, metadata or {})
        self._next_id += 1
        self._existing.append(mem)
        return mem

    def iter_by_kind(self, kind: str) -> list[_FakeMemory]:
        if (kind or "").lower() != "promise":
            return []
        return list(self._existing)


@dataclass
class _StubAgent:
    promise_worker_enabled: bool = True
    promise_worker_per_hour_cap: int = 10
    promise_worker_per_day_cap: int = 50


@dataclass
class _StubMemorySettings:
    promise_worker_interval_seconds: int = 600
    promise_worker_lookback_turns: int = 12
    promise_worker_max_per_run: int = 5
    promise_worker_max_msg_chars: int = 2000
    promise_worker_max_transcript_chars: int = 8000


def _build_world(
    *,
    responses: list[str] | None = None,
    cap_hour: int = 10,
    cap_day: int = 50,
    session_id: str = "session-1",
    messages: list[tuple[str, str]] | None = None,
    existing: list[_FakeMemory] | None = None,
    agent: _StubAgent | None = None,
    memory_settings: _StubMemorySettings | None = None,
) -> tuple[PromiseExtractionWorker, _FakeMemoryStore, _StubOllama, FactCheckRateLimiter]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    store = _FakeMemoryStore(existing=existing)
    ollama = _StubOllama(responses=list(responses or []))
    rate_limiter = FactCheckRateLimiter(
        db,
        per_hour_cap=cap_hour,
        per_day_cap=cap_day,
        state_key="promise_worker.test",
    )
    if messages is None:
        messages = [
            ("user", "I keep meaning to call the dentist about my filling."),
            ("assistant", "I'll dig into the LanceDB indexing docs for you tonight."),
        ]
    for role, content in messages:
        db.add_message(session_id=session_id, role=role, content=content)
    worker = PromiseExtractionWorker(
        memory_store=store,
        chat_db=db,
        embedder=_StubEmbedder(),
        ollama=ollama,
        chat_model="llama3:latest",
        rate_limiter=rate_limiter,
        cancel_event=threading.Event(),
        agent_settings=agent or _StubAgent(),
        memory_settings=memory_settings or _StubMemorySettings(),
        session_id_provider=lambda: session_id,
        user_display_name_provider=lambda: "Jacob",
        user_names_provider=lambda: ["Jacob"],
        assistant_name_provider=lambda: "Aiko",
    )
    return worker, store, ollama, rate_limiter


def _seed_promise(content: str, who: str = "user") -> _FakeMemory:
    return _FakeMemory(
        1,
        content,
        {"promise_who": who, "promise_status": "open"},
    )


class QualityGateTests(unittest.TestCase):
    def test_idiom_first_token_rejected(self) -> None:
        self.assertTrue(_is_low_quality("never know"))

    def test_short_body_rejected(self) -> None:
        self.assertTrue(_is_low_quality("do"))

    def test_single_content_word_rejected(self) -> None:
        self.assertTrue(_is_low_quality("resolve them"))

    def test_idiom_whole_phrase_rejected(self) -> None:
        self.assertTrue(_is_low_quality("we will see"))

    def test_real_promise_accepted(self) -> None:
        self.assertFalse(_is_low_quality("fix the deploy script"))
        self.assertFalse(_is_low_quality("bring Jacob some tea"))


class ParseTests(unittest.TestCase):
    def test_basic_object_with_deadline(self) -> None:
        raw = json.dumps([
            {"who": "user", "what": "start running", "deadline": "this weekend"},
        ])
        out = PromiseExtractionWorker._parse_promises(raw)
        assert out is not None
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].who, "user")
        self.assertIn("start running", out[0].text)
        self.assertIn("by this weekend", out[0].text)

    def test_assistant_mapping(self) -> None:
        raw = json.dumps([
            {"who": "aiko", "what": "send the recap", "deadline": None},
        ])
        out = PromiseExtractionWorker._parse_promises(raw)
        assert out is not None
        self.assertEqual(out[0].who, "assistant")

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(PromiseExtractionWorker._parse_promises("not json"))

    def test_empty_array_returns_empty_list(self) -> None:
        self.assertEqual(PromiseExtractionWorker._parse_promises("[]"), [])


class RunTests(unittest.TestCase):
    def test_run_persists_clean_promise(self) -> None:
        payload = json.dumps([
            {"who": "user", "what": "call the dentist about the filling", "deadline": None},
        ])
        worker, store, ollama, _ = _build_world(responses=[payload])
        result = worker.run()
        self.assertEqual(result["persisted"], 1)
        self.assertEqual(len(ollama.chat_calls), 1)
        self.assertTrue(ollama.chat_calls[0]["format_json"])
        call = store.calls[-1]
        self.assertEqual(call["kind"], "promise")
        self.assertEqual(call["tier"], "long_term")
        self.assertEqual(call["confidence"], 0.85)
        self.assertEqual(
            call["metadata"],
            {"promise_who": "user", "promise_status": "open"},
        )
        self.assertIn("Jacob promised", call["content"])

    def test_run_drops_low_quality(self) -> None:
        payload = json.dumps([
            {"who": "user", "what": "never know", "deadline": None},
        ])
        worker, store, _, _ = _build_world(responses=[payload])
        result = worker.run()
        self.assertEqual(result["persisted"], 0)
        self.assertEqual(result["dropped_low_quality"], 1)
        self.assertEqual(store.calls, [])

    def test_run_dedupes_against_existing(self) -> None:
        existing = [_seed_promise("Jacob promised: fix the deploy script")]
        payload = json.dumps([
            {"who": "user", "what": "fix the deploy script", "deadline": None},
        ])
        worker, store, _, _ = _build_world(
            responses=[payload], existing=existing,
        )
        result = worker.run()
        self.assertEqual(result["persisted"], 0)
        self.assertEqual(result["dropped_duplicate"], 1)

    def test_run_caps_per_run(self) -> None:
        # Genuinely distinct promises (near-duplicates would be deduped).
        distinct = [
            "call the dentist about the filling",
            "email the landlord about the leak",
            "buy groceries for the dinner party",
            "review the pull request from Sam",
            "book the flight to Tokyo",
            "renew the gym membership",
            "fix the broken stair railing",
            "water the office plants",
        ]
        payload = json.dumps([
            {"who": "user", "what": what, "deadline": None}
            for what in distinct
        ])
        worker, store, _, _ = _build_world(
            responses=[payload],
            memory_settings=_StubMemorySettings(promise_worker_max_per_run=3),
        )
        result = worker.run()
        self.assertEqual(result["persisted"], 3)

    def test_empty_array_no_writes(self) -> None:
        worker, store, _, _ = _build_world(responses=["[]"])
        result = worker.run()
        self.assertEqual(result["persisted"], 0)
        self.assertEqual(store.calls, [])

    def test_unparseable_skipped(self) -> None:
        worker, _, _, _ = _build_world(responses=["nonsense"])
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "llm_unparseable")

    def test_disabled_skips(self) -> None:
        worker, _, ollama, _ = _build_world(
            responses=["[]"], agent=_StubAgent(promise_worker_enabled=False),
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "disabled")
        self.assertEqual(len(ollama.chat_calls), 0)

    def test_includes_both_roles_in_transcript(self) -> None:
        worker, _, ollama, _ = _build_world(responses=["[]"])
        worker.run()
        prompt = ollama.chat_calls[0]["messages"][-1]["content"]
        self.assertIn("Jacob:", prompt)
        self.assertIn("Aiko:", prompt)


class RateLimitTests(unittest.TestCase):
    def test_rate_limited_skip(self) -> None:
        worker, _, ollama, limiter = _build_world(
            cap_hour=1, cap_day=1, responses=["[]"],
        )
        self.assertTrue(limiter.allow(datetime.now(timezone.utc)))
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "rate_limited")
        self.assertEqual(len(ollama.chat_calls), 0)

    def test_is_ready_false_when_exhausted(self) -> None:
        worker, _, _, limiter = _build_world(cap_hour=1, cap_day=1)
        now = datetime.now(timezone.utc)
        self.assertTrue(limiter.allow(now))
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))


class PrivacyScrubTests(unittest.TestCase):
    def test_url_only_message_blocks_extraction(self) -> None:
        worker, _, ollama, _ = _build_world(
            messages=[("user", "https://example.com/dashboard?token=abcdef123")],
            responses=["[]"],
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "privacy_blocked")
        self.assertEqual(len(ollama.chat_calls), 0)


class PromiseContentTests(unittest.TestCase):
    def test_to_memory_content(self) -> None:
        p = Promise(who="user", text="call my mom tomorrow")
        self.assertIn("Jacob promised", p.to_memory_content())
        p2 = Promise(who="assistant", text="check on the deploy")
        self.assertIn("Aiko promised", p2.to_memory_content())


if __name__ == "__main__":
    unittest.main()

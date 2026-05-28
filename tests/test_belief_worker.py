"""Tests for :mod:`app.core.belief_worker`."""
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

from app.core.belief_store import BeliefStore, KIND_MOOD, KIND_OPINION
from app.core.belief_worker import BeliefInferenceWorker
from app.core.chat_database import ChatDatabase
from app.core.fact_check_rate_limiter import FactCheckRateLimiter


class _StubEmbedder:
    """Deterministic 4-dim embedder so tests can assert on the cosine path."""

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(4, dtype=np.float32)
        for token in (text or "").lower().split():
            vec[hash(token) % 4] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


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


@dataclass
class _StubAgent:
    belief_tracking_enabled: bool = True
    belief_worker_enabled: bool = True
    belief_worker_per_hour_cap: int = 10
    belief_worker_per_day_cap: int = 50


@dataclass
class _StubBeliefSettings:
    belief_worker_interval_seconds: int = 3600
    belief_worker_lookback_turns: int = 12
    belief_max_active_per_user: int = 200


def _build_world(
    *,
    responses: list[str] | None = None,
    cap_hour: int = 10,
    cap_day: int = 50,
    session_id: str = "session-1",
    user_messages: list[str] | None = None,
) -> tuple[BeliefInferenceWorker, BeliefStore, _StubOllama, FactCheckRateLimiter]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    store = BeliefStore(db)
    ollama = _StubOllama(responses=list(responses or []))
    rate_limiter = FactCheckRateLimiter(
        db,
        per_hour_cap=cap_hour,
        per_day_cap=cap_day,
        state_key="belief_worker.test",
    )
    # Seed the message store with some user turns.
    if user_messages is None:
        user_messages = [
            "I'm so excited about the Tokyo trip next month!",
            "Rust language really feels overhyped to me lately.",
        ]
    for content in user_messages:
        db.add_message(
            session_id=session_id, role="user", content=content,
        )
        db.add_message(
            session_id=session_id, role="assistant",
            content="ack",
        )
    worker = BeliefInferenceWorker(
        belief_store=store,
        chat_db=db,
        embedder=_StubEmbedder(),
        ollama=ollama,
        chat_model="llama3:latest",
        rate_limiter=rate_limiter,
        cancel_event=threading.Event(),
        agent_settings=_StubAgent(),
        belief_settings=_StubBeliefSettings(),
        session_id_provider=lambda: session_id,
        user_id_provider=lambda: "u1",
        user_names_provider=lambda: ["Jacob"],
        assistant_name_provider=lambda: "Aiko",
    )
    return worker, store, ollama, rate_limiter


class ExtractionTests(unittest.TestCase):
    def test_run_upserts_beliefs_from_llm(self) -> None:
        payload = json.dumps([
            {
                "kind": "mood",
                "topic": "tokyo trip",
                "predicted_state": "excited",
                "confidence": 0.8,
            },
            {
                "kind": "opinion",
                "topic": "rust language",
                "predicted_state": "overhyped",
                "confidence": 0.6,
            },
        ])
        worker, store, ollama, _ = _build_world(responses=[payload])
        result = worker.run()
        self.assertEqual(result["upserted"], 2)
        self.assertEqual(len(ollama.chat_calls), 1)
        beliefs = store.list_active(user_id="u1")
        topics = {b.topic for b in beliefs}
        self.assertIn("tokyo trip", topics)
        self.assertIn("rust language", topics)

    def test_invalid_kind_dropped(self) -> None:
        payload = json.dumps([
            {
                "kind": "bogus",
                "topic": "x",
                "predicted_state": "y",
                "confidence": 0.5,
            },
            {
                "kind": "mood",
                "topic": "ok",
                "predicted_state": "y",
                "confidence": 0.5,
            },
        ])
        worker, store, _, _ = _build_world(responses=[payload])
        result = worker.run()
        # Only the valid one lands.
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(len(store.list_active(user_id="u1")), 1)

    def test_empty_array_no_upserts(self) -> None:
        worker, store, _, _ = _build_world(responses=["[]"])
        result = worker.run()
        self.assertEqual(result["upserted"], 0)
        self.assertEqual(len(store.list_active(user_id="u1")), 0)

    def test_unparseable_response_returns_skipped(self) -> None:
        worker, _, _, _ = _build_world(responses=["not json at all"])
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "llm_unparseable")


class RateLimitTests(unittest.TestCase):
    def test_rate_limited_skip(self) -> None:
        worker, store, ollama, limiter = _build_world(
            cap_hour=1, cap_day=1,
            responses=["[]"],
        )
        # Burn the only token so the second call defers.
        self.assertTrue(limiter.allow(datetime.now(timezone.utc)))
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "rate_limited")
        # Worker shouldn't have called the LLM.
        self.assertEqual(len(ollama.chat_calls), 0)


class PrivacyScrubTests(unittest.TestCase):
    def test_url_only_message_blocks_extraction(self) -> None:
        # Single user message that's basically just a URL/email -> the
        # privacy scrubber should bail and the worker should never
        # call the LLM.
        worker, _, ollama, _ = _build_world(
            user_messages=["https://example.com/dashboard?token=abcdef123"],
            responses=["[]"],
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "privacy_blocked")
        self.assertEqual(len(ollama.chat_calls), 0)


class LookbackTests(unittest.TestCase):
    def test_lookback_window_caps_user_turns(self) -> None:
        # 30 short user turns; lookback_turns=12 means only the last
        # 12 hit the prompt.
        msgs = [f"belief about topic_{i}" for i in range(30)]
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            user_messages=msgs,
        )
        worker.run()
        self.assertEqual(len(ollama.chat_calls), 1)
        prompt = ollama.chat_calls[0]["messages"][-1]["content"]
        # Last 12 topics should be present; earlier ones absent.
        self.assertIn("topic_29", prompt)
        self.assertIn("topic_18", prompt)
        self.assertNotIn("topic_5", prompt)


class SelfTagGuardTests(unittest.TestCase):
    def test_self_tag_wins_over_lower_confidence_worker(self) -> None:
        # Seed a high-confidence self-tag belief; worker returns the
        # same topic at lower confidence -> should be skipped.
        worker, store, _, _ = _build_world(
            responses=[json.dumps([
                {
                    "kind": "mood",
                    "topic": "tokyo trip",
                    "predicted_state": "nervous",
                    "confidence": 0.4,
                },
            ])],
        )
        existing = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.85,
            source="self_tag",
        )
        assert existing is not None
        result = worker.run()
        self.assertEqual(result["skipped_self_tag"], 1)
        self.assertEqual(result["upserted"], 0)
        # The high-confidence self-tag belief is preserved.
        latest = store.get(existing.id)
        self.assertEqual(latest.predicted_state, "excited")
        self.assertEqual(latest.confidence, 0.85)


if __name__ == "__main__":
    unittest.main()

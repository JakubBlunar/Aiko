"""K1 personality backlog tests for :class:`GoalWorker`.

Covers:
- Bootstrap path: cold ring + LLM JSON -> goals persisted.
- Reflection path: existing goal -> progress note + mirror update.
- Rate-limiter integration: when limiter says "no", no LLM call fires.
- ``is_ready`` honours ``agent.goals_enabled``.
- Cancel-event mid-run short-circuits the write.
- Empty / malformed LLM responses degrade silently to "wrote=0".
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.goals.goal_store import GoalStore
from app.core.goals.goal_worker import GoalWorker
from app.core.memory.memory_store import MemoryStore


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


class _FakeOllama:
    """Returns a pre-set JSON string streamed as a single chunk."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.last_surface: str | None = None
        self.last_messages: list[dict] | None = None

    def chat_stream(self, messages, **kwargs):
        self.calls += 1
        self.last_surface = kwargs.get("surface")
        self.last_messages = list(messages)
        # Simulate the chat_stream contract: a generator of chunks.
        return iter([self.payload])


class _FakeAgentSettings:
    goals_enabled: bool = True
    goal_worker_bootstrap_enabled: bool = True


class _FakeMemorySettings:
    goal_reflection_interval_seconds: float = 60.0


def _harness(
    *,
    payload: str,
    per_hour_cap: int = 10,
    per_day_cap: int = 50,
    agent_settings: _FakeAgentSettings | None = None,
    memory_settings: _FakeMemorySettings | None = None,
) -> tuple[GoalStore, GoalWorker, _FakeOllama, FactCheckRateLimiter, list[dict]]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    goal_store = GoalStore(
        memory_store=memory_store,
        embedder=embedder,
        max_active=5,
        max_progress_per_goal=4,
    )
    rate_limiter = FactCheckRateLimiter(
        chat_db,
        per_hour_cap=per_hour_cap,
        per_day_cap=per_day_cap,
        state_key="goal_worker.rate_state",
    )
    ollama = _FakeOllama(payload)
    notified: list[dict] = []
    worker = GoalWorker(
        goal_store=goal_store,
        ollama=ollama,
        chat_model="test-model",
        cancel_event=threading.Event(),
        agent_settings=agent_settings or _FakeAgentSettings(),
        memory_settings=memory_settings or _FakeMemorySettings(),
        rate_limiter=rate_limiter,
        persona_provider=lambda: (
            "Self-image:\nplayful, curious, lives in a small bright room.\n\n"
            "Curiosity:\nlikes small handmade things and slow rituals."
        ),
        rolling_summary_provider=lambda: "",
        user_display_name_provider=lambda: "Jacob",
        assistant_display_name_provider=lambda: "Aiko",
        notify_memory_added=lambda payload: notified.append(payload),
    )
    return goal_store, worker, ollama, rate_limiter, notified


class TestBootstrap(unittest.TestCase):
    def test_bootstrap_writes_goals_when_ring_is_cold(self) -> None:
        payload = json.dumps({
            "goals": [
                {"summary": "keep a slow saturday tea ritual alive each weekend"},
                {"summary": "get fluent at sketching small everyday objects"},
                {"summary": "practice listening for harmonic seventh and ninth chords"},
            ]
        })
        goal_store, worker, ollama, _, notified = _harness(payload=payload)
        result = worker.run()
        self.assertEqual(result["branch"], "bootstrap")
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["wrote"], 3)
        self.assertEqual(len(goal_store.list_active()), 3)
        self.assertEqual(ollama.calls, 1)
        self.assertEqual(ollama.last_surface, "goal_worker_bootstrap")
        self.assertEqual(len(notified), 3)
        for mem in goal_store.list_active():
            self.assertEqual(
                (mem.metadata or {}).get("source"), "worker_bootstrap"
            )

    def test_bootstrap_caps_at_max_active(self) -> None:
        payload = json.dumps({
            "goals": [
                {"summary": f"keep doing the slow thing number {label} every week"}
                for label in (
                    "alpha",
                    "bravo",
                    "charlie",
                    "delta",
                    "echo",
                    "foxtrot",
                    "golf",
                )
            ]
        })
        goal_store, worker, _, _, _ = _harness(payload=payload)
        result = worker.run()
        self.assertLessEqual(result["wrote"], goal_store.max_active)

    def test_bootstrap_empty_payload_returns_no_candidates(self) -> None:
        payload = "{}"
        goal_store, worker, _, _, _ = _harness(payload=payload)
        result = worker.run()
        self.assertEqual(result["branch"], "bootstrap")
        self.assertEqual(result["reason"], "no_candidates")
        self.assertEqual(len(goal_store.list_active()), 0)

    def test_bootstrap_disabled_setting(self) -> None:
        settings = _FakeAgentSettings()
        settings.goal_worker_bootstrap_enabled = False
        payload = "{}"
        goal_store, worker, ollama, _, _ = _harness(
            payload=payload,
            agent_settings=settings,
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "bootstrap_disabled")
        self.assertEqual(ollama.calls, 0)


class TestReflection(unittest.TestCase):
    def test_reflection_writes_progress_and_mirrors_on_goal(self) -> None:
        payload = json.dumps({
            "note": "I noticed I keep reaching for major seventh shapes; next "
                    "week I want to compare them with a real jazz standard.",
        })
        goal_store, worker, ollama, _, _ = _harness(payload=payload)
        goal = goal_store.add_goal(
            summary="practice jazz piano sevenths and ninths daily",
            source="user",
        )
        self.assertIsNotNone(goal)
        assert goal is not None
        result = worker.run()
        self.assertEqual(result["branch"], "reflection")
        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["goal_id"], int(goal.id))
        self.assertEqual(ollama.last_surface, "goal_worker_reflection")
        history = goal_store.list_progress(int(goal.id))
        self.assertEqual(len(history), 1)
        progress = history[0]
        self.assertEqual(
            (progress.metadata or {}).get("source"), "worker"
        )
        # Mirror landed on the parent goal.
        refreshed = goal_store.list_active()[0]
        gmeta = refreshed.metadata or {}
        self.assertEqual(gmeta.get("reflection_count"), 1)
        self.assertIsNotNone(gmeta.get("last_reflected_at"))
        self.assertIn("seventh", gmeta.get("last_progress_note") or "")

    def test_reflection_empty_note_returns_zero_writes(self) -> None:
        payload = json.dumps({"note": ""})
        goal_store, worker, _, _, _ = _harness(payload=payload)
        goal = goal_store.add_goal(
            summary="learn russian alphabet slowly each evening this season",
        )
        assert goal is not None
        result = worker.run()
        self.assertEqual(result["branch"], "reflection")
        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["reason"], "empty_note")

    def test_reflection_picks_oldest_touched_goal(self) -> None:
        payload = json.dumps({"note": "a fresh observation on this goal today"})
        goal_store, worker, _, _, _ = _harness(payload=payload)
        first = goal_store.add_goal(
            summary="explore woodworking hand chisels and dovetail joints"
        )
        second = goal_store.add_goal(
            summary="learn russian alphabet slowly each evening this season"
        )
        third = goal_store.add_goal(
            summary="practice jazz piano sevenths and ninths daily"
        )
        assert first is not None and second is not None and third is not None
        # Touch second + third so first is oldest-untouched.
        goal_store.add_progress(
            goal_id=int(second.id),
            note="russian alphabet practice on the second day went smoothly",
        )
        goal_store.add_progress(
            goal_id=int(third.id),
            note="jazz piano sevenths exercise tonight felt rewarding",
        )
        result = worker.run()
        self.assertEqual(result["goal_id"], int(first.id))


class TestGating(unittest.TestCase):
    def test_is_ready_respects_goals_disabled(self) -> None:
        settings = _FakeAgentSettings()
        settings.goals_enabled = False
        payload = "{}"
        _, worker, _, _, _ = _harness(
            payload=payload,
            agent_settings=settings,
        )
        now = datetime.now(timezone.utc)
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))

    def test_is_ready_default_interval(self) -> None:
        payload = "{}"
        _, worker, _, _, _ = _harness(payload=payload)
        now = datetime.now(timezone.utc)
        # Never ran -> always ready.
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        # Last ran just now -> not ready until interval elapses.
        self.assertFalse(worker.is_ready(now=now, last_run_at=now))

    def test_rate_limit_skips_llm_call(self) -> None:
        payload = json.dumps({"goals": [{"summary": "any goal"}]})
        _, worker, ollama, rate_limiter, _ = _harness(
            payload=payload, per_hour_cap=0,
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "rate_limited")
        self.assertEqual(ollama.calls, 0)

    def test_disabled_short_circuits(self) -> None:
        settings = _FakeAgentSettings()
        settings.goals_enabled = False
        payload = "{}"
        _, worker, ollama, _, _ = _harness(
            payload=payload,
            agent_settings=settings,
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "disabled")
        self.assertEqual(ollama.calls, 0)


class TestCancellation(unittest.TestCase):
    def test_cancel_before_start(self) -> None:
        payload = "{}"
        _, worker, _, _, _ = _harness(payload=payload)
        worker._cancel_event.set()
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "cancelled_before_start")


if __name__ == "__main__":
    unittest.main()

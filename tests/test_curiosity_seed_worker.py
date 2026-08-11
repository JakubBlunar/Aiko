"""Tests for :mod:`app.core.proactive.curiosity_seed_worker` (K9).

Since schema v29 the worker writes into the cue pool rather than the
memories table, so these run against a real :class:`CueStore` on a
throwaway database -- the pool's state machine is the thing the worker's
pacing now depends on, and stubbing it would only test the stub.

The LLM, topic graph and embedder stay mocked: the interesting behaviour
is the filter chain between them and the write.
"""
from __future__ import annotations

import hashlib
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.proactive.curiosity_seed_worker import CuriositySeedWorker


# ── stubs ────────────────────────────────────────────────────────────


class _StubEmbedder:
    """Deterministic embedder: hash text into a 4-D unit vector.

    Uses md5 rather than ``hash()``: Python randomizes string hashing per
    process, so the previous version drew a different vector for the same
    text on every run. With only four dimensions the two seeds in
    ``_MIXED_PAYLOAD`` occasionally landed close enough for the novelty
    filter to drop one, which made ``SubjectQuotaTests`` fail on roughly
    one process in forty (``PYTHONHASHSEED=14`` is a reproducer). A stub
    that is only *usually* deterministic is worse than a random one,
    because the failure looks like a real regression.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> np.ndarray:
        self.calls.append(text)
        digest = hashlib.md5(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:4], "little")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(4).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v


class _StubTopicGraph:
    """Always returns the configured (sim, id) tuple. Tests vary it."""

    def __init__(self, *, best_sim: float = 0.0, best_id: int | None = 1) -> None:
        self.best_sim = best_sim
        self.best_id = best_id
        self.calls = 0

    def best_match(self, vec: np.ndarray) -> tuple[float, int | None]:
        self.calls += 1
        return self.best_sim, self.best_id

    def topic_clusters(self) -> list[Any]:
        return []

    def is_close_to_any_cluster(self, vec, threshold=None) -> bool:  # noqa: D401
        thr = threshold if threshold is not None else 0.65
        return self.best_sim >= thr


class _StubOllama:
    """Yields the configured chunks for ``chat_stream``."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls = 0

    def chat_stream(self, *args: Any, **kwargs: Any):
        self.calls += 1
        yield self._payload


# ── helpers ──────────────────────────────────────────────────────────


def _agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        curiosity_seed_enabled=True,
        curiosity_seed_max_active=6,
        curiosity_seed_max_per_run=2,
        curiosity_seed_min_novelty=0.85,
        topic_graph_filter_threshold=0.65,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory_settings() -> SimpleNamespace:
    return SimpleNamespace(curiosity_seed_interval_seconds=3600)


_GOOD_PAYLOAD = (
    '{"seeds": ['
    '{"topic": "your favourite tea ritual", '
    ' "prompt_text": "I have been wondering what your perfect tea moment looks like.", '
    ' "why": "small, sensory, easy to share."}, '
    '{"topic": "morning lighting habits", '
    ' "prompt_text": "Off-topic, but do you ever notice how morning light hits the room?", '
    ' "why": "ambient curiosity"}'
    ']}'
)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _build(
        self,
        *,
        payload: str = _GOOD_PAYLOAD,
        best_sim: float = 0.0,
        **overrides: Any,
    ) -> CuriositySeedWorker:
        self.graph = _StubTopicGraph(best_sim=best_sim)
        self.ollama = _StubOllama(payload)
        self.embedder = _StubEmbedder()
        return CuriositySeedWorker(
            cue_store_provider=lambda: self.store,
            topic_graph=self.graph,
            embedder=self.embedder,
            ollama=self.ollama,
            chat_model="test-model",
            cancel_event=threading.Event(),
            agent_settings=_agent_settings(**overrides),
            memory_settings=_memory_settings(),
            persona_provider=lambda: "Curiosity:\n- loves rituals",
            rolling_summary_provider=lambda: "Recent chat: about coffee",
            user_display_name_provider=lambda: "Jacob",
            assistant_display_name_provider=lambda: "Aiko",
        )

    def _pending(self) -> list:
        return self.store.pending("curiosity_seed", limit=20)


# ── tests ────────────────────────────────────────────────────────────


class WriteShapeTests(_Fixture):
    def test_seeds_land_in_the_pool_as_pending_cues(self) -> None:
        worker = self._build()
        result = worker.run()
        self.assertGreaterEqual(result.get("wrote", 0), 1)
        rows = self._pending()
        self.assertEqual(len(rows), result["wrote"])
        for row in rows:
            self.assertEqual(row.cue_type, "curiosity_seed")
            self.assertTrue(row.subject)
            # Two readers, two fields: the seeds block lists the bare
            # subject, the narrative weaver speaks the prompt sentence.
            self.assertTrue(row.payload.get("prompt_text"))
            self.assertEqual(row.payload.get("source"), "llm")
            self.assertIn("candidate_score", row.payload)

    def test_the_subject_is_embedded_for_later_matching(self) -> None:
        """Consumption cosines against this vector, so it has to be stored."""
        worker = self._build()
        worker.run()
        rows = self.store.pending(
            "curiosity_seed", limit=20, with_embedding=True,
        )
        self.assertTrue(rows)
        self.assertIsNotNone(rows[0].embedding)


class GraphFilterTests(_Fixture):
    def test_high_graph_sim_rejects_all_candidates(self) -> None:
        # Every candidate's best_match returns 0.99 -> above the
        # 0.65 default filter threshold -> all rejected.
        worker = self._build(best_sim=0.99)
        result = worker.run()
        self.assertEqual(result.get("wrote", 0), 0)
        self.assertGreaterEqual(result.get("rejected_graph", 0), 1)
        self.assertEqual(self._pending(), [])


class NoveltyFilterTests(_Fixture):
    def test_an_existing_pending_seed_blocks_a_near_duplicate(self) -> None:
        worker = self._build()
        # Collapse every candidate onto one vector, then queue a seed
        # already carrying it: the novelty filter reads the pool, so a
        # duplicate of pending stock is what it must catch.
        fixed = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        worker._embedder.embed = lambda text: fixed  # type: ignore[assignment]
        self.store.add(
            "curiosity_seed", "placeholder", "placeholder", embedding=fixed,
        )

        result = worker.run()
        self.assertEqual(result.get("wrote", 0), 0)
        self.assertGreaterEqual(result.get("rejected_novelty", 0), 1)


class DemandTests(_Fixture):
    def test_disabled_is_a_hard_veto(self) -> None:
        worker = self._build(curiosity_seed_enabled=False)
        self.assertFalse(
            worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None),
        )
        signal = worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)

    def test_an_empty_shelf_is_maximum_pressure(self) -> None:
        worker = self._build()
        signal = worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 1.0)
        self.assertTrue(signal.needs_llm)

    def test_a_full_shelf_reports_none(self) -> None:
        """max_active stopped being a cap and became the target stock."""
        worker = self._build(curiosity_seed_max_active=2)
        self.store.add("curiosity_seed", "one", "one")
        self.store.add("curiosity_seed", "two", "two")
        signal = worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)

    def test_spending_a_seed_reopens_demand(self) -> None:
        worker = self._build(curiosity_seed_max_active=2)
        first = self.store.add("curiosity_seed", "one", "one")
        self.store.add("curiosity_seed", "two", "two")
        self.store.mark_used(first, evidence="test")
        signal = worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertGreater(signal.pressure, 0.0)


class ParseTests(_Fixture):
    def test_returns_empty_on_invalid_json(self) -> None:
        worker = self._build(payload="not json at all")
        result = worker.run()
        self.assertEqual(result.get("wrote", 0), 0)
        self.assertEqual(result.get("checked", 0), 0)


# ── K87: seeds that aren't about him ────────────────────────────────


_MIXED_PAYLOAD = (
    '{"seeds": ['
    '{"topic": "your favourite tea ritual", '
    ' "prompt_text": "I have been wondering what your perfect tea moment '
    'looks like.", "why": "small and sensory", "about": "user"}, '
    '{"topic": "second-day cold brew", '
    ' "prompt_text": "Cold brew seems to taste rounder the next morning.", '
    ' "why": "worth testing", "about": "subject"}'
    ']}'
)


class SubjectQuotaTests(_Fixture):
    def _abouts(self) -> list[str]:
        return [r.payload.get("about") for r in self._pending()]

    def test_the_label_is_recorded_on_the_cue(self) -> None:
        worker = self._build(payload=_MIXED_PAYLOAD)
        worker.run()
        self.assertEqual(sorted(self._abouts()), ["person", "subject"])

    def test_a_starved_pool_writes_the_subject_seed_first(self) -> None:
        # One write per run, and the model listed the bond-scoped seed
        # first. Without the reorder the subject seed never lands.
        worker = self._build(
            payload=_MIXED_PAYLOAD, curiosity_seed_max_per_run=1,
        )
        result = worker.run()
        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["wrote_subject"], 1)
        self.assertEqual(self._abouts(), ["subject"])

    def test_a_pool_already_at_quota_keeps_the_model_order(self) -> None:
        for i in range(4):
            self.store.add(
                "curiosity_seed", f"own {i}", f"own {i}",
                payload={"about": "subject"},
            )
        worker = self._build(
            payload=_MIXED_PAYLOAD,
            curiosity_seed_max_per_run=1,
            curiosity_seed_max_active=10,
        )
        result = worker.run()
        self.assertEqual(result["wrote_subject"], 0)

    def test_a_zero_quota_keeps_the_model_order(self) -> None:
        worker = self._build(
            payload=_MIXED_PAYLOAD,
            curiosity_seed_max_per_run=1,
            curiosity_subject_quota=0.0,
        )
        result = worker.run()
        self.assertEqual(result["wrote_subject"], 0)

    def test_a_subject_label_on_a_question_about_him_is_overruled(self) -> None:
        payload = (
            '{"seeds": [{"topic": "his commute", "prompt_text": "I wonder '
            'how his commute has been treating him.", "why": "care", '
            '"about": "subject"}]}'
        )
        worker = self._build(payload=payload)
        result = worker.run()
        self.assertEqual(result["wrote_subject"], 0)
        self.assertEqual(self._abouts(), ["person"])

    def test_an_unlabelled_seed_is_read_from_its_text(self) -> None:
        payload = (
            '{"seeds": [{"topic": "second-day cold brew", "prompt_text": '
            '"Cold brew seems rounder the next morning.", "why": "hmm"}]}'
        )
        worker = self._build(payload=payload)
        result = worker.run()
        self.assertEqual(result["wrote_subject"], 1)

    def test_the_prompt_asks_for_a_floor_of_subject_seeds(self) -> None:
        worker = self._build(payload=_MIXED_PAYLOAD)
        captured: dict[str, Any] = {}

        def _capture(messages, *args: Any, **kwargs: Any):
            captured["system"] = messages[0]["content"]
            yield _MIXED_PAYLOAD

        worker._ollama.chat_stream = _capture  # type: ignore[assignment]
        worker.run()
        self.assertIn("must be \"subject\" seeds", captured["system"])


if __name__ == "__main__":
    unittest.main()

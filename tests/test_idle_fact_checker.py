"""F1 personality backlog tests for the background fact-checker.

Covers (per F1.10 of the plan):

- Claim extractor matches year / measurement / date / proper-noun spans.
- :class:`FactCheckQueue` round-trips through ``kv_meta`` (so the queue
  survives a restart).
- :meth:`IdleFactChecker.is_ready` honours the master switch, empty
  queue, the rate-limit caps, and the idle-scheduler debounce.
- A ``support`` verdict bumps confidence and stamps ``last_verified_at``.
- A ``contradict`` verdict drops confidence and sets
  ``metadata.flags.conflict`` (and rewrites the content when the model
  is confident enough).
- An inconclusive verdict touches ``last_checked_at`` but leaves the
  confidence column alone.
- A pre-set ``cancel_event`` during distil makes the worker requeue the
  claim at the front of the queue and *not* mutate the memory.
- Gap source + support verdict stamps ``resolved_at`` on the gap and
  writes an answer memory with confidence ~0.85.

The :class:`OllamaClient` and :class:`WebSearchTool` are both stubbed —
the worker only depends on small surface areas of each (``chat_stream``
producing chunks; ``run`` returning a JSON string).
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.claim_extractor import find_claims
from app.core.fact_check_queue import ClaimItem, FactCheckQueue
from app.core.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.idle_fact_checker import IdleFactChecker, Verdict
from app.core.knowledge_gap_extractor import KnowledgeGapStore
from app.core.memory_store import MemoryStore


# ── tiny stubs ─────────────────────────────────────────────────────────


class _DeterministicEmbedder:
    """Mirror of the helper used by the F2 tests — keeps related text
    in nearby slots so the gap-resolution path can write an answer.
    """

    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            slot = hash(token) % self.DIM
            vec[slot] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


@dataclass
class _StubWebSearch:
    """Stand-in for :class:`WebSearchTool`. Returns the same canned
    JSON payload every call — the worker only cares that the JSON has
    a ``results`` array with ``snippet``-bearing entries.
    """

    payload: dict[str, Any] = field(
        default_factory=lambda: {
            "results": [
                {
                    "title": "Wiki article",
                    "url": "https://en.example.org/article",
                    "snippet": (
                        "Python 3.12 was released on October 2, 2023 by the "
                        "core development team."
                    ),
                },
            ],
        }
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, args: dict[str, Any]) -> str:
        self.calls.append(dict(args))
        return json.dumps(self.payload)


@dataclass
class _StubOllamaClient:
    """Stand-in for :class:`OllamaClient`. The worker only uses
    ``chat_stream``; everything else (model warmup, options) is
    irrelevant.

    The ``verdict`` field controls what JSON the stream yields. Set
    ``raise_on_call`` to simulate an Ollama outage.
    """

    verdict_json: dict[str, Any] = field(
        default_factory=lambda: {
            "verdict": "support",
            "delta": 0.1,
            "rewrite": None,
        }
    )
    raise_on_call: bool = False
    chunked: bool = True
    cancel_during_stream: threading.Event | None = None
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
                "stop_event_set": bool(stop_event and stop_event.is_set()),
                "format_json": format_json,
            }
        )
        if self.raise_on_call:
            raise RuntimeError("simulated ollama outage")
        # Honour cancellation: when the stream is asked to be aborted
        # mid-flight (either pre-set or via the test-only helper) we
        # yield nothing so the worker treats it as a cancellation.
        if stop_event is not None and stop_event.is_set():
            return
        if self.cancel_during_stream is not None:
            self.cancel_during_stream.set()
            if stop_event is not None and stop_event.is_set():
                return
        raw = json.dumps(self.verdict_json)
        if not self.chunked:
            yield raw
            return
        # Emit a handful of chunks so the worker's accumulator path is
        # actually exercised.
        mid = len(raw) // 2
        yield raw[:mid]
        if stop_event is not None and stop_event.is_set():
            return
        yield raw[mid:]


@dataclass
class _StubAgentSettings:
    fact_checker_enabled: bool = True
    fact_checker_per_hour_cap: int = 10
    fact_checker_per_day_cap: int = 50


@dataclass
class _StubMemorySettings:
    fact_checker_interval_seconds: int = 300


# ── shared fixture ─────────────────────────────────────────────────────


def _build_world(
    *,
    fact_checker_enabled: bool = True,
    per_hour_cap: int = 10,
    per_day_cap: int = 50,
    verdict_json: dict[str, Any] | None = None,
    cancel_during_stream: threading.Event | None = None,
) -> dict[str, Any]:
    """Build a self-contained checker + queue + memory store + stubs.

    The dict makes test assertions terse — we pluck whichever piece
    each individual test cares about.
    """
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    queue = FactCheckQueue(chat_db)
    rate_limiter = FactCheckRateLimiter(
        chat_db, per_hour_cap=per_hour_cap, per_day_cap=per_day_cap,
    )
    web_search = _StubWebSearch()
    ollama = _StubOllamaClient(
        verdict_json=verdict_json
        or {"verdict": "support", "delta": 0.1, "rewrite": None},
        cancel_during_stream=cancel_during_stream,
    )
    cancel_event = threading.Event()
    knowledge_gap_store = KnowledgeGapStore(
        memory_store=memory_store,
        embedder=embedder,
    )
    update_calls: list[dict[str, Any]] = []

    def _notify_memory_updated(snapshot: dict[str, Any]) -> None:
        update_calls.append(snapshot)

    worker = IdleFactChecker(
        queue=queue,
        memory_store=memory_store,
        agent_settings=_StubAgentSettings(
            fact_checker_enabled=fact_checker_enabled,
            fact_checker_per_hour_cap=per_hour_cap,
            fact_checker_per_day_cap=per_day_cap,
        ),
        memory_settings=_StubMemorySettings(),
        ollama=ollama,
        chat_model="stub-model",
        web_search_tool=web_search,
        rate_limiter=rate_limiter,
        cancel_event=cancel_event,
        knowledge_gap_store=knowledge_gap_store,
        embedder=embedder,
        notify_memory_updated=_notify_memory_updated,
    )
    return {
        "path": path,
        "chat_db": chat_db,
        "memory_store": memory_store,
        "embedder": embedder,
        "queue": queue,
        "rate_limiter": rate_limiter,
        "web_search": web_search,
        "ollama": ollama,
        "cancel_event": cancel_event,
        "knowledge_gap_store": knowledge_gap_store,
        "worker": worker,
        "update_calls": update_calls,
    }


def _add_fact(memory_store: MemoryStore, embedder: Any, text: str) -> int:
    """Insert a vanilla fact memory and return its id."""
    emb = embedder.embed(text)
    mem = memory_store.add(text, "fact", emb, salience=0.6)
    assert mem is not None
    return int(mem.id)


# ── F1.1 — claim extractor ─────────────────────────────────────────────


class TestClaimExtractorPatterns(unittest.TestCase):
    def test_year_pattern(self) -> None:
        claims = find_claims("Python 3.12 was released in 2023.")
        kinds = {c.kind for c in claims}
        self.assertIn("year", kinds)

    def test_measurement_pattern(self) -> None:
        claims = find_claims("The hike is 12 km long with 500m climb.")
        # ``12 km`` matches the measurement whitelist.
        self.assertTrue(any(c.kind == "measurement" for c in claims))

    def test_date_pattern(self) -> None:
        claims = find_claims("The meeting on 03/14/2024 was rescheduled.")
        self.assertTrue(any(c.kind == "date" for c in claims))

    def test_proper_noun_pattern(self) -> None:
        claims = find_claims("We visited Yosemite National Park last summer.")
        self.assertTrue(any(c.kind == "proper_noun" for c in claims))

    def test_max_claims_cap(self) -> None:
        text = (
            "In 2021, in 2022, in 2023, in 2024 — lots of years stacked up."
        )
        claims = find_claims(text, max_claims=2)
        self.assertLessEqual(len(claims), 2)


# ── F1.2 — queue persistence round-trip via kv_meta ────────────────────


class TestQueuePersistence(unittest.TestCase):
    def test_enqueue_survives_restart(self) -> None:
        d = tempfile.mkdtemp()
        path = Path(d) / "mem.db"
        chat_db = ChatDatabase(path)
        q1 = FactCheckQueue(chat_db)
        q1.enqueue(memory_id=11, claim_text="2023", claim_kind="year")
        q1.enqueue(memory_id=12, claim_text="12 km", claim_kind="measurement")
        # Simulate restart: drop ``q1`` and re-read from the same DB.
        chat_db_2 = ChatDatabase(path)
        q2 = FactCheckQueue(chat_db_2)
        items = q2.peek_all()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].memory_id, 11)
        self.assertEqual(items[1].claim_text, "12 km")


# ── F1.4 + F1.8 — readiness gate ───────────────────────────────────────


class TestIsReady(unittest.TestCase):
    def test_disabled_flag_blocks_run(self) -> None:
        world = _build_world(fact_checker_enabled=False)
        world["queue"].enqueue(
            memory_id=1, claim_text="2023", claim_kind="year",
        )
        worker: IdleFactChecker = world["worker"]
        self.assertFalse(
            worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_empty_queue_blocks_run(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        self.assertFalse(
            worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_recent_run_blocks_run(self) -> None:
        world = _build_world()
        world["queue"].enqueue(
            memory_id=1, claim_text="2023", claim_kind="year",
        )
        worker: IdleFactChecker = world["worker"]
        now = datetime.now(timezone.utc)
        # Last run was 10s ago — interval is 300s so we're not ready.
        last_run = now - timedelta(seconds=10)
        self.assertFalse(worker.is_ready(now=now, last_run_at=last_run))

    def test_rate_limit_blocks_run(self) -> None:
        world = _build_world(per_hour_cap=1, per_day_cap=5)
        world["queue"].enqueue(
            memory_id=1, claim_text="2023", claim_kind="year",
        )
        # Burn the hourly budget so ``is_ready`` rejects the next tick.
        world["rate_limiter"].allow()
        worker: IdleFactChecker = world["worker"]
        self.assertFalse(
            worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_happy_path_allows_run(self) -> None:
        world = _build_world()
        world["queue"].enqueue(
            memory_id=1, claim_text="2023", claim_kind="year",
        )
        worker: IdleFactChecker = world["worker"]
        self.assertTrue(
            worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )


# ── F1.5 — verdict parser ──────────────────────────────────────────────


class TestVerdictParser(unittest.TestCase):
    def test_support_verdict(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        result = worker._parse_verdict(
            '{"verdict": "support", "delta": 0.2, "rewrite": null}'
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.kind, "support")
        self.assertAlmostEqual(result.delta, 0.2)

    def test_contradict_with_negative_delta(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        result = worker._parse_verdict(
            '{"verdict": "contradict", "delta": -0.25, "rewrite": "fixed text"}'
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.kind, "contradict")
        self.assertLess(result.delta, 0.0)
        self.assertEqual(result.rewrite, "fixed text")

    def test_flips_sign_for_mismatched_delta(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        # Contradict verdict with accidentally positive delta — the
        # parser should re-sign it so confidence never goes up on a
        # contradiction.
        result = worker._parse_verdict(
            '{"verdict": "contradict", "delta": 0.2, "rewrite": null}'
        )
        assert result is not None
        self.assertLess(result.delta, 0.0)

    def test_inconclusive_forces_zero_delta(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        result = worker._parse_verdict(
            '{"verdict": "inconclusive", "delta": 0.3, "rewrite": null}'
        )
        assert result is not None
        self.assertEqual(result.delta, 0.0)

    def test_clamps_oversized_delta(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        result = worker._parse_verdict(
            '{"verdict": "support", "delta": 999, "rewrite": null}'
        )
        assert result is not None
        self.assertLessEqual(result.delta, 0.3)

    def test_rejects_unknown_verdict(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        self.assertIsNone(
            worker._parse_verdict(
                '{"verdict": "maybe", "delta": 0.0, "rewrite": null}'
            )
        )

    def test_handles_stray_prose_around_json(self) -> None:
        world = _build_world()
        worker: IdleFactChecker = world["worker"]
        result = worker._parse_verdict(
            'Sure, here is the result:\n'
            '{"verdict": "support", "delta": 0.1, "rewrite": null}\n'
            'hope that helps!'
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.kind, "support")


# ── F1.7 — verdict application ─────────────────────────────────────────


class TestVerdictApplication(unittest.TestCase):
    def test_support_bumps_confidence_and_stamps_last_verified_at(self) -> None:
        world = _build_world(
            verdict_json={
                "verdict": "support",
                "delta": 0.15,
                "rewrite": None,
            },
        )
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        mem_id = _add_fact(
            memory_store, embedder, "Python 3.12 was released in 2023.",
        )
        before = memory_store.get(mem_id)
        assert before is not None
        before_conf = float(before.confidence)
        # Use a claim with both a year and a verifiable noun so the
        # privacy gate accepts it (a bare ``"2023"`` has no
        # fact-checkable surface and is correctly rejected).
        world["queue"].enqueue(
            memory_id=mem_id,
            claim_text="Python 3.12 released 2023",
            claim_kind="year",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("verdict"), "support")
        after = memory_store.get(mem_id)
        assert after is not None
        self.assertGreater(after.confidence, before_conf)
        self.assertIn("last_verified_at", after.metadata)

    def test_contradict_drops_confidence_and_sets_conflict_flag(self) -> None:
        world = _build_world(
            verdict_json={
                "verdict": "contradict",
                "delta": -0.25,
                "rewrite": "Python 3.12 shipped in October 2023",
            },
        )
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        # Seed at a high confidence so we can observe the drop.
        emb = embedder.embed("Python 3.12 was released in 2022.")
        seeded = memory_store.add(
            "Python 3.12 was released in 2022.",
            "fact",
            emb,
            salience=0.6,
            confidence=0.9,
        )
        assert seeded is not None
        mem_id = int(seeded.id)
        world["queue"].enqueue(
            memory_id=mem_id,
            claim_text="Python 3.12 released 2022",
            claim_kind="year",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("verdict"), "contradict")
        after = memory_store.get(mem_id)
        assert after is not None
        self.assertLess(after.confidence, 0.9)
        flags = after.metadata.get("flags") or {}
        self.assertTrue(flags.get("conflict"))
        self.assertIn("last_verified_at", after.metadata)
        # |delta| > 0.2 + rewrite present — content should be replaced.
        self.assertEqual(
            after.content, "Python 3.12 shipped in October 2023",
        )

    def test_contradict_with_small_delta_keeps_original_content(self) -> None:
        world = _build_world(
            verdict_json={
                "verdict": "contradict",
                "delta": -0.1,  # below the 0.2 rewrite threshold
                "rewrite": "Different text",
            },
        )
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        mem_id = _add_fact(memory_store, embedder, "Original claim text.")
        world["queue"].enqueue(
            memory_id=mem_id,
            claim_text="Original claim text",
            claim_kind="proper_noun",
        )
        world["worker"].run()
        after = memory_store.get(mem_id)
        assert after is not None
        self.assertEqual(after.content, "Original claim text.")

    def test_inconclusive_touches_last_checked_at_only(self) -> None:
        world = _build_world(
            verdict_json={
                "verdict": "inconclusive",
                "delta": 0.0,
                "rewrite": None,
            },
        )
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        mem_id = _add_fact(memory_store, embedder, "Some claim from 2020.")
        before = memory_store.get(mem_id)
        assert before is not None
        before_conf = float(before.confidence)
        world["queue"].enqueue(
            memory_id=mem_id,
            claim_text="Some claim from 2020",
            claim_kind="year",
        )
        world["worker"].run()
        after = memory_store.get(mem_id)
        assert after is not None
        self.assertAlmostEqual(after.confidence, before_conf, places=5)
        self.assertIn("last_checked_at", after.metadata)
        self.assertNotIn("last_verified_at", after.metadata)


# ── F1.6 — cancellation requeues the claim ─────────────────────────────


class TestCancellation(unittest.TestCase):
    def test_cancel_event_before_run_requeues_claim(self) -> None:
        world = _build_world()
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        mem_id = _add_fact(memory_store, embedder, "Something from 2023.")
        before_conf = float(memory_store.get(mem_id).confidence)
        world["queue"].enqueue(
            memory_id=mem_id,
            claim_text="Something from 2023",
            claim_kind="year",
        )
        world["cancel_event"].set()
        result = world["worker"].run()
        # The pre-start fast path returns "cancelled_before_start" and
        # leaves the queue untouched (the claim still has to be drained
        # later; the front-requeue behaviour kicks in for mid-distil
        # cancellation, covered below).
        self.assertEqual(result.get("reason"), "cancelled_before_start")
        items = world["queue"].peek_all()
        self.assertEqual(len(items), 1)
        # Confidence wasn't mutated.
        after = memory_store.get(mem_id)
        assert after is not None
        self.assertAlmostEqual(after.confidence, before_conf, places=5)

    def test_cancel_during_distil_requeues_at_front(self) -> None:
        cancel_event = threading.Event()
        world = _build_world(cancel_during_stream=cancel_event)
        # Reuse the same event so the stub trips ``cancel_event`` mid-
        # stream and the worker sees the cancellation.
        world["cancel_event"] = cancel_event
        world["worker"]._cancel_event = cancel_event  # noqa: SLF001
        world["ollama"].cancel_during_stream = cancel_event
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        mem_id_a = _add_fact(memory_store, embedder, "Alpha fact from 2021.")
        mem_id_b = _add_fact(memory_store, embedder, "Beta fact from 2022.")
        # Enqueue two claims. The first will be cancelled and put back
        # at the head; the second stays untouched.
        world["queue"].enqueue(
            memory_id=mem_id_a,
            claim_text="Alpha fact from 2021",
            claim_kind="year",
        )
        world["queue"].enqueue(
            memory_id=mem_id_b,
            claim_text="Beta fact from 2022",
            claim_kind="year",
        )
        before_conf = float(memory_store.get(mem_id_a).confidence)
        result = world["worker"].run()
        self.assertTrue(result.get("cancelled"))
        items = world["queue"].peek_all()
        self.assertEqual(len(items), 2)
        # The cancelled claim went back to the head.
        self.assertEqual(items[0].memory_id, mem_id_a)
        # Confidence wasn't mutated.
        after = memory_store.get(mem_id_a)
        assert after is not None
        self.assertAlmostEqual(after.confidence, before_conf, places=5)


# ── F1.7 — gap-resolution path ─────────────────────────────────────────


class TestGapResolution(unittest.TestCase):
    def test_support_verdict_on_gap_writes_answer_and_resolves(self) -> None:
        world = _build_world(
            verdict_json={
                "verdict": "support",
                "delta": 0.2,
                "rewrite": "Jacob plays the violin for about ten years",
            },
        )
        memory_store: MemoryStore = world["memory_store"]
        gap_store: KnowledgeGapStore = world["knowledge_gap_store"]
        gap = gap_store.add_gap(
            topic="music", question="how long has Jacob played violin",
        )
        assert gap is not None
        gap_id = int(gap.id)
        world["queue"].enqueue(
            memory_id=gap_id,
            claim_text="how long has Jacob played violin",
            claim_kind="knowledge_gap",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("verdict"), "support")
        self.assertTrue(result.get("resolved_gap"))
        answer_id = result.get("answer_memory_id")
        self.assertIsNotNone(answer_id)
        # The gap row should now carry resolved_at + resolved_by_memory_id.
        resolved = memory_store.get(gap_id)
        assert resolved is not None
        self.assertTrue(resolved.metadata.get("resolved_at"))
        self.assertEqual(
            resolved.metadata.get("resolved_by_memory_id"), answer_id,
        )
        # The answer memory landed at confidence ~0.85 (F1.7).
        answer = memory_store.get(int(answer_id))
        assert answer is not None
        self.assertAlmostEqual(answer.confidence, 0.85, places=2)
        self.assertEqual(answer.kind, "fact")


# ── safety: missing memory just no-ops ─────────────────────────────────


class TestMissingMemoryNoOps(unittest.TestCase):
    def test_deleted_memory_is_skipped_cleanly(self) -> None:
        world = _build_world()
        world["queue"].enqueue(
            memory_id=9999,
            claim_text="Some lost fact from 2023",
            claim_kind="year",
        )
        result = world["worker"].run()
        # The worker reports the missing-memory case rather than raising.
        self.assertTrue(result.get("memory_missing"))


if __name__ == "__main__":
    unittest.main()

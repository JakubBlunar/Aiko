"""Tests for :mod:`app.core.memory_conflict_worker`."""
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

from app.core.chat_database import ChatDatabase
from app.core.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.memory_conflict_store import (
    MemoryConflictStore,
    STATUS_AUTO_RESOLVED,
    STATUS_OPEN,
)
from app.core.memory_conflict_worker import MemoryConflictWorker
from app.core.memory_store import MemoryStore


# ── tiny stubs ─────────────────────────────────────────────────────────


class _TokenEmbedder:
    """Token-frequency bag-of-words embedder with a wide bucket count.

    With ``DIM=64`` the hash collision rate on 5-8 distinct content
    tokens is small enough that two sentences sharing N of K tokens
    yield cosine ~= N / K. That lets the tests construct pairs that
    land squarely in the worker's 0.80-0.92 band.
    """

    DIM = 64

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
class _StubOllama:
    verdicts: list[dict[str, Any]] = field(default_factory=list)
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
            "model": model,
            "format_json": format_json,
        })
        if self.raise_on_call:
            raise RuntimeError("simulated ollama outage")
        if not self.verdicts:
            # Default: YES so heuristic-borderline cases resolve.
            yield json.dumps({"verdict": "YES", "reason": "test default"})
            return
        payload = self.verdicts.pop(0)
        yield json.dumps(payload)


@dataclass
class _StubAgent:
    conflict_detector_enabled: bool = True
    conflict_detector_per_hour_cap: int = 10
    conflict_detector_per_day_cap: int = 50


@dataclass
class _StubMemorySettings:
    conflict_detector_interval_seconds: int = 3600
    conflict_detector_similarity_min: float = 0.80
    conflict_detector_similarity_max: float = 0.92
    conflict_detector_auto_resolve_delta: float = 0.30
    conflict_detector_max_corpus: int = 1000
    conflict_detector_max_pairs_per_run: int = 50


def _build_world(
    *,
    enabled: bool = True,
    verdicts: list[dict[str, Any]] | None = None,
    per_hour_cap: int = 10,
    per_day_cap: int = 50,
    auto_resolve_delta: float = 0.30,
) -> dict[str, Any]:
    """Spin up a fresh world with a real DB, memory store, and worker."""
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    conflict_store = MemoryConflictStore(chat_db)
    embedder = _TokenEmbedder()
    rate_limiter = FactCheckRateLimiter(
        chat_db,
        per_hour_cap=per_hour_cap,
        per_day_cap=per_day_cap,
        state_key="conflict_detector.rate_state",
    )
    ollama = _StubOllama(verdicts=list(verdicts or []))
    cancel_event = threading.Event()

    updated_calls: list[dict[str, Any]] = []
    worker = MemoryConflictWorker(
        memory_store=memory_store,
        conflict_store=conflict_store,
        ollama=ollama,
        chat_model="stub",
        rate_limiter=rate_limiter,
        cancel_event=cancel_event,
        agent_settings=_StubAgent(
            conflict_detector_enabled=enabled,
            conflict_detector_per_hour_cap=per_hour_cap,
            conflict_detector_per_day_cap=per_day_cap,
        ),
        memory_settings=_StubMemorySettings(
            conflict_detector_auto_resolve_delta=auto_resolve_delta,
        ),
        notify_memory_updated=lambda d: updated_calls.append(d),
    )

    return {
        "chat_db": chat_db,
        "memory_store": memory_store,
        "conflict_store": conflict_store,
        "embedder": embedder,
        "rate_limiter": rate_limiter,
        "ollama": ollama,
        "cancel_event": cancel_event,
        "worker": worker,
        "updated_calls": updated_calls,
    }


def _add_fact(
    memory_store: MemoryStore,
    embedder: _TokenEmbedder,
    content: str,
    *,
    confidence: float = 0.7,
    kind: str = "fact",
) -> int:
    emb = embedder.embed(content)
    mem = memory_store.add(
        content=content,
        kind=kind,
        embedding=emb,
        salience=0.6,
        confidence=confidence,
        skip_dedupe=True,
    )
    assert mem is not None, f"memory insert failed for {content!r}"
    return int(mem.id)


# ── tests ──────────────────────────────────────────────────────────────


class HeuristicDefinitePathTests(unittest.TestCase):
    def test_definite_skips_llm_and_resolves(self) -> None:
        w = _build_world()
        # Two contradicting facts with enough shared tokens to fall in
        # the 0.80-0.92 cosine band (6 of 7 tokens shared).
        id_a = _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
            confidence=0.9,
        )
        id_b = _add_fact(
            w["memory_store"], w["embedder"],
            "Bea hates spicy food deeply and often",
            confidence=0.5,
        )

        result = w["worker"].run()

        # No LLM call — definite heuristic short-circuited.
        self.assertEqual(len(w["ollama"].chat_calls), 0)
        self.assertEqual(result.get("definite"), 1)
        self.assertEqual(result.get("auto_resolved"), 1)
        # Loser demoted; winner untouched.
        loser = w["memory_store"].get(id_b)
        winner = w["memory_store"].get(id_a)
        self.assertEqual(loser.tier, "archive")
        self.assertAlmostEqual(loser.confidence, 0.20, places=3)
        self.assertEqual(loser.metadata.get("superseded_by"), id_a)
        self.assertEqual(winner.confidence, 0.9)
        # Pair stored with auto_resolved status.
        opens = w["conflict_store"].list_recent(status=STATUS_AUTO_RESOLVED)
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0].winner_id, id_a)
        self.assertEqual(opens[0].loser_id, id_b)

    def test_definite_open_when_delta_below_auto_threshold(self) -> None:
        w = _build_world(auto_resolve_delta=0.30)
        id_a = _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
            confidence=0.7,
        )
        id_b = _add_fact(
            w["memory_store"], w["embedder"],
            "Bea hates spicy food deeply and often",
            confidence=0.7,  # delta = 0
        )

        result = w["worker"].run()
        self.assertEqual(result.get("opened"), 1)
        self.assertEqual(result.get("auto_resolved"), 0)
        opens = w["conflict_store"].list_open()
        self.assertEqual(len(opens), 1)
        # Loser tier untouched on open status.
        for mem_id in (id_a, id_b):
            mem = w["memory_store"].get(mem_id)
            self.assertEqual(mem.tier, "long_term")


class HeuristicBorderlinePathTests(unittest.TestCase):
    def test_borderline_with_llm_yes_resolves(self) -> None:
        w = _build_world(
            verdicts=[{"verdict": "YES", "reason": "ages disagree"}],
        )
        id_a = _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 35 years old and currently single",
            confidence=0.9,
        )
        id_b = _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 60 years old and currently single",
            confidence=0.5,
        )
        result = w["worker"].run()

        # One LLM verification call.
        self.assertEqual(len(w["ollama"].chat_calls), 1)
        self.assertEqual(result.get("borderline_consulted"), 1)
        self.assertEqual(result.get("auto_resolved"), 1)
        # Loser demoted because delta 0.4 >= 0.30.
        loser = w["memory_store"].get(id_b)
        self.assertEqual(loser.tier, "archive")
        self.assertAlmostEqual(loser.confidence, 0.20, places=3)

    def test_borderline_with_llm_no_drops(self) -> None:
        w = _build_world(
            verdicts=[{"verdict": "NO", "reason": "both true"}],
        )
        id_a = _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 35 years old and currently single",
            confidence=0.9,
        )
        id_b = _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 60 years old and currently single",
            confidence=0.5,
        )
        result = w["worker"].run()

        self.assertEqual(len(w["ollama"].chat_calls), 1)
        self.assertEqual(result.get("borderline_dropped_by_llm"), 1)
        self.assertEqual(result.get("opened"), 0)
        self.assertEqual(result.get("auto_resolved"), 0)
        # Nothing in the store.
        self.assertEqual(w["conflict_store"].count_open(), 0)
        # Loser untouched.
        loser = w["memory_store"].get(id_b)
        self.assertEqual(loser.tier, "long_term")
        self.assertAlmostEqual(loser.confidence, 0.5, places=3)

    def test_borderline_with_llm_unrelated_drops(self) -> None:
        w = _build_world(
            verdicts=[
                {"verdict": "UNRELATED", "reason": "different topic"},
            ],
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 35 years old and currently single",
            confidence=0.7,
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 60 years old and currently single",
            confidence=0.7,
        )
        result = w["worker"].run()
        self.assertEqual(result.get("borderline_dropped_by_llm"), 1)
        self.assertEqual(w["conflict_store"].count_open(), 0)


class RateLimitDeferTests(unittest.TestCase):
    def test_borderline_deferred_when_rate_limited(self) -> None:
        w = _build_world(per_hour_cap=0, per_day_cap=0)
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 35 years old and currently single",
            confidence=0.9,
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bob is 60 years old and currently single",
            confidence=0.5,
        )
        result = w["worker"].run()
        # No LLM call because the limiter rejected.
        self.assertEqual(len(w["ollama"].chat_calls), 0)
        self.assertEqual(result.get("borderline_skipped_rate_limit"), 1)
        # No row written so the worker re-tries next tick.
        self.assertEqual(w["conflict_store"].count_open(), 0)


class SkipExistingPairTests(unittest.TestCase):
    def test_already_recorded_pair_is_not_redetected(self) -> None:
        w = _build_world()
        id_a = _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
            confidence=0.9,
        )
        id_b = _add_fact(
            w["memory_store"], w["embedder"],
            "Bea hates spicy food deeply and often",
            confidence=0.5,
        )
        # Pre-seed a "dismissed" row -- worker should NOT touch it again.
        w["conflict_store"].record(
            memory_a_id=id_a,
            memory_b_id=id_b,
            similarity=0.85,
            confidence_delta=0.4,
            heuristic_label="definite",
            status="dismissed",
        )
        result = w["worker"].run()
        self.assertEqual(result.get("pairs_skipped_existing"), 1)
        self.assertEqual(result.get("definite"), 0)
        self.assertEqual(result.get("auto_resolved"), 0)


class KindAllowListTests(unittest.TestCase):
    def test_knowledge_gap_pairs_are_excluded(self) -> None:
        """Process kinds (knowledge_gap / curiosity_finding / open_question
        / ...) are excluded from the candidate corpus."""
        w = _build_world()
        # Two ``knowledge_gap`` rows that would otherwise be a definite
        # contradiction on the heuristic. Since the worker filters them
        # out at the candidate-corpus step, they never reach the gate.
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
            kind="knowledge_gap",
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea hates spicy food deeply and often",
            kind="knowledge_gap",
        )
        result = w["worker"].run()
        self.assertEqual(result.get("pairs_scanned", 0), 0)
        self.assertEqual(w["conflict_store"].count_open(), 0)


class CorpusFilterTests(unittest.TestCase):
    def test_topically_distant_pairs_below_band_are_skipped(self) -> None:
        w = _build_world()
        _add_fact(
            w["memory_store"], w["embedder"],
            "User likes spicy food",
            confidence=0.7,
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "User lives in northern Spain",
            confidence=0.7,
        )
        result = w["worker"].run()
        # No overlap in the cosine band, nothing scanned in the heuristic step.
        self.assertEqual(result.get("definite", 0), 0)
        self.assertEqual(result.get("borderline_consulted", 0), 0)
        self.assertEqual(w["conflict_store"].count_open(), 0)


class GuardTests(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        w = _build_world(enabled=False)
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea hates spicy food deeply and often",
        )
        result = w["worker"].run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "disabled")

    def test_tiny_corpus_returns_skipped(self) -> None:
        w = _build_world()
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
        )
        result = w["worker"].run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "corpus_too_small")

    def test_pre_set_cancel_aborts(self) -> None:
        w = _build_world()
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea loves spicy food deeply and often",
        )
        _add_fact(
            w["memory_store"], w["embedder"],
            "Bea hates spicy food deeply and often",
        )
        w["cancel_event"].set()
        result = w["worker"].run()
        self.assertTrue(result.get("skipped"))


class IsReadyTests(unittest.TestCase):
    def test_disabled_not_ready(self) -> None:
        w = _build_world(enabled=False)
        now = datetime.now(timezone.utc)
        self.assertFalse(w["worker"].is_ready(now=now, last_run_at=None))

    def test_enabled_ready_after_interval(self) -> None:
        w = _build_world()
        now = datetime.now(timezone.utc)
        self.assertTrue(w["worker"].is_ready(now=now, last_run_at=None))


class ParseVerdictTests(unittest.TestCase):
    def test_parses_yes(self) -> None:
        result = MemoryConflictWorker._parse_verdict(
            '{"verdict": "YES", "reason": "ages clash"}',
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, "YES")
        self.assertEqual(result.reason, "ages clash")

    def test_parses_in_prose(self) -> None:
        result = MemoryConflictWorker._parse_verdict(
            'Sure! {"verdict": "NO", "reason": "compatible"} hope that helps.',
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, "NO")

    def test_normalises_case(self) -> None:
        result = MemoryConflictWorker._parse_verdict(
            '{"verdict": "yes", "reason": "ok"}',
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, "YES")

    def test_invalid_verdict_returns_none(self) -> None:
        result = MemoryConflictWorker._parse_verdict(
            '{"verdict": "MAYBE", "reason": "?"}',
        )
        self.assertIsNone(result)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(MemoryConflictWorker._parse_verdict("not json"))
        self.assertIsNone(MemoryConflictWorker._parse_verdict(""))


if __name__ == "__main__":
    unittest.main()

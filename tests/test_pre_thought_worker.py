"""K11 pre-thought worker — pure helpers + worker run with fakes.

The pure functions (``parse_questions``, ``clean_thought``,
``build_pre_thought_content``) are deterministic and covered directly.
The worker run is exercised with in-memory fakes (no real LLM / DB /
embedder) to pin: two-stage call flow, scratchpad write keyed on the
QUESTION embedding, novelty dedupe, rate-limit + max-active skips, and
prune-to-cap.
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.proactive import pre_thought_worker as ptw
from app.core.proactive.pre_thought_worker import PreThoughtWorker


# ── pure helpers ────────────────────────────────────────────────────


class ParseQuestionsTests(unittest.TestCase):
    def test_valid(self) -> None:
        raw = '{"questions": ["what do you think of jazz?", "how was your day?"]}'
        self.assertEqual(
            ptw.parse_questions(raw),
            ["what do you think of jazz?", "how was your day?"],
        )

    def test_pulls_object_out_of_noise(self) -> None:
        raw = 'sure!\n{"questions": ["q one"]}\nhope that helps'
        self.assertEqual(ptw.parse_questions(raw), ["q one"])

    def test_malformed_json(self) -> None:
        self.assertEqual(ptw.parse_questions("{not json"), [])

    def test_non_dict(self) -> None:
        self.assertEqual(ptw.parse_questions('["a", "b"]'), [])

    def test_missing_questions_key(self) -> None:
        self.assertEqual(ptw.parse_questions('{"foo": 1}'), [])

    def test_skips_blank_and_nonstring(self) -> None:
        raw = '{"questions": ["", "  ", 5, null, "real one"]}'
        self.assertEqual(ptw.parse_questions(raw), ["real one"])

    def test_dedupes_case_insensitive(self) -> None:
        raw = '{"questions": ["Same Q", "same q", "other"]}'
        self.assertEqual(ptw.parse_questions(raw), ["Same Q", "other"])

    def test_caps_at_max(self) -> None:
        raw = '{"questions": ["a", "b", "c", "d", "e"]}'
        self.assertEqual(len(ptw.parse_questions(raw, max_questions=2)), 2)


class CleanThoughtTests(unittest.TestCase):
    def test_strips_reaction_tag(self) -> None:
        out = ptw.clean_thought("[[reaction:warm]] jazz is the best, honestly")
        self.assertNotIn("[[reaction:", out)
        self.assertIn("jazz is the best", out)

    def test_empty(self) -> None:
        self.assertEqual(ptw.clean_thought(""), "")

    def test_trims_long(self) -> None:
        out = ptw.clean_thought("x" * 1000)
        self.assertLessEqual(len(out), 600)


class BuildContentTests(unittest.TestCase):
    def test_shape(self) -> None:
        out = ptw.build_pre_thought_content("do you like tea?", "love it", "Jacob")
        self.assertIn("Jacob", out)
        self.assertIn("do you like tea?", out)
        self.assertIn("love it", out)

    def test_fallback_name(self) -> None:
        out = ptw.build_pre_thought_content("q", "a", "")
        self.assertIn("they", out)


# ── fakes ───────────────────────────────────────────────────────────


def _unit(seed: float, dim: int = 4) -> np.ndarray:
    rng = np.random.default_rng(int(seed * 1000) & 0xFFFFFFFF)
    v = rng.standard_normal(dim).astype(np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


class _FakeMemory:
    def __init__(self, mid: int, kind: str, embedding, tier: str,
                 metadata: dict, salience: float, created_at: str) -> None:
        self.id = mid
        self.kind = kind
        self.embedding = embedding
        self.tier = tier
        self.metadata = metadata
        self.salience = salience
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind}


class _FakeMemoryStore:
    def __init__(self, existing: list[_FakeMemory] | None = None) -> None:
        self._rows: list[_FakeMemory] = list(existing or [])
        self._next_id = 100 + len(self._rows)
        self.add_calls: list[dict[str, Any]] = []
        self.deleted: list[int] = []

    def iter_by_kind(self, kind: str) -> list[_FakeMemory]:
        return [m for m in self._rows if m.kind == kind]

    def add(self, content, kind, embedding, *, salience=0.5, confidence=None,
            tier=None, metadata=None, **kwargs) -> _FakeMemory:
        self.add_calls.append({
            "content": content, "kind": kind, "embedding": embedding,
            "salience": salience, "confidence": confidence, "tier": tier,
            "metadata": metadata,
        })
        mem = _FakeMemory(
            self._next_id, kind, embedding, tier or "long_term",
            metadata or {}, salience, "2026-06-21T00:00:00+00:00",
        )
        self._next_id += 1
        self._rows.append(mem)
        return mem

    def delete(self, memory_id: int) -> bool:
        before = len(self._rows)
        self._rows = [m for m in self._rows if m.id != memory_id]
        if len(self._rows) < before:
            self.deleted.append(memory_id)
            return True
        return False


class _FakeEmbedder:
    def __init__(self, overrides: dict[str, np.ndarray] | None = None) -> None:
        self._overrides = overrides or {}

    def embed(self, text: str) -> np.ndarray:
        if text in self._overrides:
            return self._overrides[text]
        return _unit(float(abs(hash(text)) % 9973))


class _FakeClient:
    def __init__(self, questions_json: str, draft: str = "[[reaction:warm]] sure") -> None:
        self._questions_json = questions_json
        self._draft = draft
        self.surfaces: list[str] = []

    def chat(self, messages, options=None, model=None, surface="chat", **kwargs) -> str:
        self.surfaces.append(surface)
        if surface == "pre_thought_worker":
            return self._questions_json
        return self._draft


class _FakeRateLimiter:
    def __init__(self, *, allow: bool = True, hour_used: int = 0,
                 day_used: int = 0) -> None:
        self._allow = allow
        self._hour_used = hour_used
        self._day_used = day_used
        self.allow_calls = 0

    def snapshot(self, now) -> dict[str, int]:
        return {
            "hour_used": self._hour_used, "hour_cap": 6,
            "day_used": self._day_used, "day_cap": 40,
        }

    def allow(self, now) -> bool:
        self.allow_calls += 1
        return self._allow


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(
        pre_thought_enabled=True,
        pre_thought_max_active=12,
        pre_thought_candidates=4,
        pre_thought_max_per_run=2,
        pre_thought_min_novelty=0.85,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory_settings() -> SimpleNamespace:
    return SimpleNamespace(pre_thought_interval_seconds=3600)


def _make_worker(
    *,
    store: _FakeMemoryStore,
    client: _FakeClient,
    embedder: _FakeEmbedder | None = None,
    rate_limiter: _FakeRateLimiter | None = None,
    agent: SimpleNamespace | None = None,
    notify=None,
) -> PreThoughtWorker:
    return PreThoughtWorker(
        memory_store=store,
        embedder=embedder or _FakeEmbedder(),
        ollama=client,
        chat_model="fake-model",
        cancel_event=threading.Event(),
        agent_settings=agent or _agent(),
        memory_settings=_memory_settings(),
        rate_limiter=rate_limiter or _FakeRateLimiter(),
        persona_messages_builder=lambda q: [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": q},
        ],
        persona_provider=lambda: "Aiko is warm and curious.",
        rolling_summary_provider=lambda: "They talked about jazz.",
        user_display_name_provider=lambda: "Jacob",
        assistant_display_name_provider=lambda: "Aiko",
        notify_memory_added=notify,
        clock=lambda: datetime(2026, 6, 21, tzinfo=timezone.utc),
    )


# ── worker run ──────────────────────────────────────────────────────


class WorkerRunTests(unittest.TestCase):
    def test_writes_pre_thoughts(self) -> None:
        store = _FakeMemoryStore()
        client = _FakeClient('{"questions": ["do you like jazz?", "fav food?"]}')
        # Pin the two question embeddings to orthogonal vectors so the
        # novelty dedupe can't reject the second write under an
        # unlucky PYTHONHASHSEED (the default embedder hashes text).
        embedder = _FakeEmbedder({
            "do you like jazz?": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            "fav food?": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        })
        notified: list[dict] = []
        worker = _make_worker(store=store, client=client, embedder=embedder,
                              notify=notified.append)

        result = worker.run()

        self.assertEqual(result["wrote"], 2)
        self.assertEqual(len(store.add_calls), 2)
        for call in store.add_calls:
            self.assertEqual(call["kind"], "pre_thought")
            self.assertEqual(call["tier"], "scratchpad")
            self.assertIn("question", call["metadata"])
            self.assertIn("thought", call["metadata"])
        # two drafts + one question call
        self.assertEqual(client.surfaces.count("pre_thought_worker"), 1)
        self.assertEqual(client.surfaces.count("pre_thought_draft"), 2)
        self.assertEqual(len(notified), 2)

    def test_embedding_is_on_question(self) -> None:
        store = _FakeMemoryStore()
        q_vec = _unit(42.0)
        embedder = _FakeEmbedder({"do you like jazz?": q_vec})
        client = _FakeClient('{"questions": ["do you like jazz?"]}')
        worker = _make_worker(store=store, client=client, embedder=embedder,
                              agent=_agent(pre_thought_max_per_run=1))

        worker.run()

        self.assertEqual(len(store.add_calls), 1)
        np.testing.assert_array_equal(store.add_calls[0]["embedding"], q_vec)

    def test_respects_max_per_run(self) -> None:
        store = _FakeMemoryStore()
        client = _FakeClient('{"questions": ["a?", "b?", "c?", "d?"]}')
        worker = _make_worker(store=store, client=client,
                              agent=_agent(pre_thought_max_per_run=1))
        result = worker.run()
        self.assertEqual(result["wrote"], 1)

    def test_rate_limited_skips(self) -> None:
        store = _FakeMemoryStore()
        client = _FakeClient('{"questions": ["a?"]}')
        worker = _make_worker(store=store, client=client,
                              rate_limiter=_FakeRateLimiter(allow=False))
        result = worker.run()
        self.assertEqual(result.get("reason"), "rate_limited")
        self.assertEqual(len(store.add_calls), 0)
        self.assertEqual(client.surfaces, [])

    def test_disabled_skips(self) -> None:
        store = _FakeMemoryStore()
        client = _FakeClient('{"questions": ["a?"]}')
        worker = _make_worker(store=store, client=client,
                              agent=_agent(pre_thought_enabled=False))
        result = worker.run()
        self.assertEqual(result.get("reason"), "disabled")

    def test_no_questions(self) -> None:
        store = _FakeMemoryStore()
        client = _FakeClient("not json at all")
        worker = _make_worker(store=store, client=client)
        result = worker.run()
        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result.get("reason"), "no_questions")

    def test_empty_draft_rejected(self) -> None:
        store = _FakeMemoryStore()
        client = _FakeClient('{"questions": ["a?"]}', draft="   ")
        worker = _make_worker(store=store, client=client)
        result = worker.run()
        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_empty"], 1)

    def test_novelty_dedupe_within_run(self) -> None:
        store = _FakeMemoryStore()
        same = _unit(7.0)
        embedder = _FakeEmbedder({"q one?": same, "q two?": same})
        client = _FakeClient('{"questions": ["q one?", "q two?"]}')
        worker = _make_worker(store=store, client=client, embedder=embedder,
                              agent=_agent(pre_thought_max_per_run=5))
        result = worker.run()
        # first written, second rejected as a near-duplicate question
        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["rejected_novelty"], 1)

    def test_max_active_skips(self) -> None:
        existing = [
            _FakeMemory(i, "pre_thought", _unit(float(i)), "scratchpad",
                        {"question": f"q{i}"}, 0.4, "2026-06-20T00:00:00+00:00")
            for i in range(3)
        ]
        store = _FakeMemoryStore(existing)
        client = _FakeClient('{"questions": ["a?"]}')
        worker = _make_worker(store=store, client=client,
                              agent=_agent(pre_thought_max_active=3))
        result = worker.run()
        self.assertEqual(result.get("reason"), "max_active")

    def test_prune_to_cap(self) -> None:
        # 2 existing + write up to 2 → 4 active, cap 3 → prune 1 oldest.
        # Pin every embedding to an orthogonal basis vector so the
        # novelty dedupe (cosine >= 0.85) never rejects a write. The
        # default _FakeEmbedder derives vectors from hash(text), which
        # is PYTHONHASHSEED-randomised and occasionally collides — that
        # made this test fail intermittently across process runs.
        e0 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        e1 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        e2 = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        e3 = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        existing = [
            _FakeMemory(1, "pre_thought", e0, "scratchpad",
                        {"question": "old1"}, 0.4, "2026-06-19T00:00:00+00:00"),
            _FakeMemory(2, "pre_thought", e1, "scratchpad",
                        {"question": "old2"}, 0.4, "2026-06-20T00:00:00+00:00"),
        ]
        store = _FakeMemoryStore(existing)
        client = _FakeClient('{"questions": ["new a?", "new b?"]}')
        embedder = _FakeEmbedder({"new a?": e2, "new b?": e3})
        worker = _make_worker(store=store, client=client, embedder=embedder,
                              agent=_agent(pre_thought_max_active=3,
                                           pre_thought_max_per_run=2))
        result = worker.run()
        self.assertEqual(result["wrote"], 2)
        self.assertGreaterEqual(result["pruned"], 1)
        # the oldest (id=1) should be the prune victim
        self.assertIn(1, store.deleted)


class IsReadyTests(unittest.TestCase):
    def test_ready_when_fresh(self) -> None:
        store = _FakeMemoryStore()
        worker = _make_worker(store=store, client=_FakeClient("{}"))
        self.assertTrue(
            worker.is_ready(
                now=datetime(2026, 6, 21, tzinfo=timezone.utc),
                last_run_at=None,
            )
        )

    def test_not_ready_when_disabled(self) -> None:
        store = _FakeMemoryStore()
        worker = _make_worker(store=store, client=_FakeClient("{}"),
                              agent=_agent(pre_thought_enabled=False))
        self.assertFalse(
            worker.is_ready(
                now=datetime(2026, 6, 21, tzinfo=timezone.utc),
                last_run_at=None,
            )
        )

    def test_not_ready_when_rate_exhausted(self) -> None:
        store = _FakeMemoryStore()
        worker = _make_worker(
            store=store, client=_FakeClient("{}"),
            rate_limiter=_FakeRateLimiter(hour_used=6),
        )
        self.assertFalse(
            worker.is_ready(
                now=datetime(2026, 6, 21, tzinfo=timezone.utc),
                last_run_at=None,
            )
        )


class KindAndSurfacingTests(unittest.TestCase):
    def test_pre_thought_is_a_valid_kind(self) -> None:
        from app.core.memory.memory_store import VALID_KINDS

        self.assertIn("pre_thought", VALID_KINDS)

    def test_format_block_renders_pre_thought_suffix(self) -> None:
        from app.core.rag.rag_retriever import RagHit, RagRetriever
        from app.core.rag.rag_store import MemoryRecord

        rec = MemoryRecord(
            id="1",
            content="If Jacob asks: \u201cdo you like jazz?\u201d \u2014 I'd say: yeah, lots",
            kind="pre_thought",
            salience=0.4,
            source_session="s1",
            source_message_id=None,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_used_at=None,
            use_count=0,
        )
        hit = RagHit(source="memory", score=0.9, record=rec)
        block = RagRetriever.format_block([hit], user_display_name="Jacob")
        self.assertIn("(pre-thought)", block)


if __name__ == "__main__":
    unittest.main()

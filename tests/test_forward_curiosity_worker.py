"""Tests for :class:`app.core.proactive.forward_curiosity_worker.ForwardCuriosityWorker`.

Exercises candidate selection (from fake future_plan + callback
memories, biased by a fake routine profile), de-dup against the kv ring,
the kv journal ring trim, and the pacing gates (cooldown, daily cap,
enabled switch). All fakes — no real MemoryStore, LLM, or DB. Questions
compose via the deterministic fallback (``ollama=None``) so assertions
don't depend on a model.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.proactive.forward_curiosity_worker import (
    FORWARD_CURIOSITY_JOURNAL_KEY,
    ForwardCuriosityWorker,
    load_questions,
)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeMemory:
    def __init__(
        self,
        id_: int,
        content: str,
        *,
        kind: str = "fact",
        temporal_type: str = "durable",
    ) -> None:
        self.id = id_
        self.content = content
        self.kind = kind
        self.temporal_type = temporal_type


class _FakeMemoryStore:
    def __init__(
        self,
        *,
        future_plans: list[_FakeMemory] | None = None,
        callbacks: list[_FakeMemory] | None = None,
    ) -> None:
        self._future = future_plans or []
        self._callbacks = callbacks or []

    def list_by_temporal_type(self, temporal_type: str) -> list[_FakeMemory]:
        if temporal_type == "future_plan":
            return list(self._future)
        return []

    def iter_by_kind(self, kind: str) -> list[_FakeMemory]:
        if kind == "callback":
            return list(self._callbacks)
        return []


class _FakeProfileEntry:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeProfileStore:
    def __init__(self, fields: dict[str, str] | None = None) -> None:
        self._fields = {
            k: _FakeProfileEntry(v) for k, v in (fields or {}).items()
        }

    def fields(self, user_id: str) -> dict[str, _FakeProfileEntry]:
        return dict(self._fields)


def _make_worker(
    *,
    store: _FakeMemoryStore,
    kv: _FakeKV,
    profile: _FakeProfileStore | None = None,
    enabled: bool = True,
    cooldown: float = 3600.0,
    daily_cap: int = 4,
    seed: int = 0,
) -> ForwardCuriosityWorker:
    return ForwardCuriosityWorker(
        memory_store=store,
        kv_get=kv.get,
        kv_set=kv.set,
        user_id_provider=lambda: "jacob",
        user_display_name_provider=lambda: "Jacob",
        user_profile_store=profile,
        enabled_provider=lambda: enabled,
        ollama=None,  # deterministic fallback
        model=None,
        interval_seconds=1800.0,
        cooldown_seconds=cooldown,
        daily_cap=daily_cap,
        journal_max=8,
        rng=random.Random(seed),
    )


class DraftingTests(unittest.TestCase):
    def test_drafts_from_future_plan(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(
                    7, "espresso machine arriving Thursday",
                    temporal_type="future_plan",
                )
            ]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["source"], "future_plan")
        ring = load_questions(kv.get)
        self.assertEqual(len(ring), 1)
        self.assertIn("espresso", ring[0]["question"])
        self.assertEqual(ring[0]["source_id"], "7")

    def test_drafts_from_callback_when_no_future_plan(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            callbacks=[_FakeMemory(3, "the new job", kind="callback")]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["source"], "callback")

    def test_no_candidate_when_empty(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore()
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("no_candidate"))

    def test_force_source_picks_specific_memory(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(1, "trip to Japan", temporal_type="future_plan"),
                _FakeMemory(2, "dentist visit", temporal_type="future_plan"),
            ]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        worker.force_source("2")
        result = worker.run()
        self.assertEqual(result["source_id"], "2")
        self.assertIn("dentist", load_questions(kv.get)[0]["question"])

    def test_routine_profile_does_not_break_drafting(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(5, "marathon", temporal_type="future_plan")
            ]
        )
        profile = _FakeProfileStore(
            {"routines": "Monday-morning check-ins", "usual_hours": "evenings"}
        )
        worker = _make_worker(store=store, kv=kv, profile=profile, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["drafted"], 1)


class DedupTests(unittest.TestCase):
    def test_skips_already_drafted_source(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(9, "wedding", temporal_type="future_plan")
            ]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        first = worker.run()
        self.assertEqual(first["drafted"], 1)
        # Same single candidate is now in the ring -> no new candidate.
        second = worker.run()
        self.assertEqual(second["drafted"], 0)
        self.assertTrue(second.get("no_candidate"))


class JournalTests(unittest.TestCase):
    def test_ring_trims_to_max(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(i, f"plan {i}", temporal_type="future_plan")
                for i in range(20)
            ]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0, daily_cap=999)
        for _ in range(12):
            worker.run()
        ring = load_questions(kv.get)
        self.assertEqual(len(ring), 8)  # journal_max

    def test_load_questions_handles_garbage(self) -> None:
        kv = _FakeKV()
        kv.set(FORWARD_CURIOSITY_JOURNAL_KEY, "not json")
        self.assertEqual(load_questions(kv.get), [])


class GateTests(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[_FakeMemory(1, "x", temporal_type="future_plan")]
        )
        worker = _make_worker(store=store, kv=kv, enabled=False)
        result = worker.run()
        self.assertTrue(result.get("disabled"))
        self.assertEqual(load_questions(kv.get), [])

    def test_cooldown_blocks(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("forward_curiosity.last_fired_at", recent.isoformat())
        store = _FakeMemoryStore(
            future_plans=[_FakeMemory(1, "x", temporal_type="future_plan")]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=3600.0)
        result = worker.run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("skipped_cooldown"))

    def test_daily_cap_blocks(self) -> None:
        kv = _FakeKV()
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("forward_curiosity.day", today)
        kv.set("forward_curiosity.day_count", "4")
        store = _FakeMemoryStore(
            future_plans=[_FakeMemory(1, "x", temporal_type="future_plan")]
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0, daily_cap=4)
        result = worker.run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("skipped_daily_cap"))


if __name__ == "__main__":
    unittest.main()

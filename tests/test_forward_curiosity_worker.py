"""Tests for :class:`app.core.proactive.forward_curiosity_worker.ForwardCuriosityWorker`.

Exercises candidate selection (from fake future_plan + callback
memories, biased by a fake routine profile), de-dup against the kv ring
and the cue pool, the kv journal ring trim, the pacing gates (cooldown,
enabled switch), and the two-halves arming rule. Questions compose via
the deterministic fallback (``ollama=None``) so assertions don't depend
on a model; the pool tests use a real ``CueStore`` on a throwaway file,
since its state machine is the thing under test.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.proactive.forward_curiosity_worker import (
    FORWARD_CURIOSITY_JOURNAL_KEY,
    ForwardCuriosityWorker,
    load_questions,
    render_question_cue,
)


_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


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
    cues=None,
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
        cue_store_provider=(lambda: cues) if cues is not None else None,
        ollama=None,  # deterministic fallback
        model=None,
        interval_seconds=1800.0,
        cooldown_seconds=cooldown,
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
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        for _ in range(12):
            worker.run()
        ring = load_questions(kv.get)
        self.assertEqual(len(ring), 8)  # journal_max

    def test_load_questions_handles_garbage(self) -> None:
        kv = _FakeKV()
        kv.set(FORWARD_CURIOSITY_JOURNAL_KEY, "not json")
        self.assertEqual(load_questions(kv.get), [])


class QuestionCueRenderTests(unittest.TestCase):
    """K-time10 — the question is drafted from a memory's raw wording."""

    def test_a_deictic_resolves_against_the_source_note(self) -> None:
        written = (_NOW - timedelta(days=40)).isoformat()
        line = render_question_cue(
            {"question": "how the move went today", "source_at": written},
        )
        self.assertNotIn("today", line)
        self.assertIn("on Nov", line)

    def test_no_source_timestamp_leaves_the_question_alone(self) -> None:
        # Older journal entries predate ``source_at``; they must still
        # render rather than blow up or lose their text.
        line = render_question_cue({"question": "how the move went today"})
        self.assertEqual(line, "You've been wondering how the move went today.")

    def test_an_empty_question_renders_nothing(self) -> None:
        self.assertEqual(render_question_cue({"question": "  "}), "")


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


class PoolTests(unittest.TestCase):
    """Inventory, not a daily cap, is what stops this worker now."""

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        from app.core.infra.chat_database import ChatDatabase
        from app.core.proactive.cue_store import CueStore

        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.cues = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _worker(self, store, kv=None, **kw) -> ForwardCuriosityWorker:
        return _make_worker(
            store=store, kv=kv or _FakeKV(), cues=self.cues, **kw,
        )

    def test_run_queues_the_topic_as_the_subject(self) -> None:
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(
                    7, "espresso machine arriving Thursday",
                    temporal_type="future_plan",
                )
            ]
        )
        result = self._worker(store, cooldown=0.0).run()
        self.assertGreater(result["cue_id"], 0)
        rows = self.cues.pending("forward_curiosity")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject, "espresso machine arriving thursday")
        self.assertIn("espresso", rows[0].text)

    def test_an_empty_shelf_reports_full_pressure(self) -> None:
        worker = self._worker(_FakeMemoryStore())
        signal = worker.demand(now=_NOW, last_run_at=None)
        self.assertEqual(signal.pressure, 1.0)

    def test_a_stocked_worker_reports_no_pressure(self) -> None:
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(i, f"plan {i}", temporal_type="future_plan")
                for i in range(4)
            ]
        )
        worker = self._worker(store, cooldown=0.0)
        worker.run()
        worker.run()
        self.assertEqual(self.cues.count_pending("forward_curiosity"), 2)
        self.assertEqual(
            worker.demand(now=_NOW, last_run_at=None).pressure, 0.0,
        )

    def test_a_pooled_topic_is_not_re_drafted(self) -> None:
        """Even after the ring rotates the source id out of sight."""
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(9, "wedding", temporal_type="future_plan")
            ]
        )
        worker = self._worker(store, kv, cooldown=0.0)
        worker.run()
        kv.store.pop(FORWARD_CURIOSITY_JOURNAL_KEY, None)
        self.assertTrue(worker.run().get("no_candidate"))

    def test_demand_is_none_without_a_pool(self) -> None:
        worker = _make_worker(store=_FakeMemoryStore(), kv=_FakeKV())
        self.assertIsNone(worker.demand(now=_NOW, last_run_at=None))

    def test_disabled_worker_reports_zero_not_none(self) -> None:
        worker = self._worker(_FakeMemoryStore(), enabled=False)
        self.assertEqual(
            worker.demand(now=_NOW, last_run_at=None).pressure, 0.0,
        )

    def test_is_ready_still_vetoes_on_the_cooldown(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("forward_curiosity.last_fired_at", recent.isoformat())
        worker = self._worker(_FakeMemoryStore(), kv, cooldown=3600.0)
        self.assertFalse(worker.is_ready(now=_NOW, last_run_at=None))


class ArmingTests(unittest.TestCase):
    """``armed_cues`` needs both halves: the slot and the content."""

    class _Session:
        _chat_db = None
        _pending_forward_curiosity_seconds = None

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        from app.core.infra.chat_database import ChatDatabase
        from app.core.proactive.cue_store import CueStore

        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.session = self._Session()
        self.session._cue_store = CueStore(
            ChatDatabase(Path(tmp.name) / "chat.db")
        )

    def _armed(self) -> set[str]:
        from app.core.proactive.cue_accounting import armed_cues

        return armed_cues(self.session)

    def test_slot_without_stock_is_not_armed(self) -> None:
        self.session._pending_forward_curiosity_seconds = 9999.0
        self.assertNotIn("forward_curiosity", self._armed())

    def test_stock_without_a_slot_is_not_armed(self) -> None:
        self.session._cue_store.add(
            "forward_curiosity", "the wedding", "You've been wondering ...",
        )
        self.assertNotIn("forward_curiosity", self._armed())

    def test_both_halves_arm_it(self) -> None:
        self.session._pending_forward_curiosity_seconds = 9999.0
        self.session._cue_store.add(
            "forward_curiosity", "the wedding", "You've been wondering ...",
        )
        self.assertIn("forward_curiosity", self._armed())


if __name__ == "__main__":
    unittest.main()

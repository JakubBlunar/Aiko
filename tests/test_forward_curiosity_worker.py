"""Tests for :class:`app.core.proactive.forward_curiosity_worker.ForwardCuriosityWorker`.

Exercises candidate selection (from fake future_plan + callback
memories, her own subject notes, and L28's concept pool, with a fake
concept view and routine profile as phrasing context), de-dup against
the kv ring and the cue pool, the kv journal ring trim, the pacing gates
(cooldown, enabled switch), and the two-halves arming rule. Questions compose via
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
        metadata: dict | None = None,
    ) -> None:
        self.id = id_
        self.content = content
        self.kind = kind
        self.temporal_type = temporal_type
        self.metadata = metadata or {}


class _FakeMemoryStore:
    def __init__(
        self,
        *,
        future_plans: list[_FakeMemory] | None = None,
        callbacks: list[_FakeMemory] | None = None,
        open_questions: list[_FakeMemory] | None = None,
    ) -> None:
        self._future = future_plans or []
        self._callbacks = callbacks or []
        self._open_questions = open_questions or []

    def list_by_temporal_type(self, temporal_type: str) -> list[_FakeMemory]:
        if temporal_type == "future_plan":
            return list(self._future)
        return []

    def iter_by_kind(self, kind: str) -> list[_FakeMemory]:
        if kind == "callback":
            return list(self._callbacks)
        if kind == "open_question":
            return list(self._open_questions)
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


class _FakeConcept:
    def __init__(
        self,
        concept_id: int,
        label: str,
        *,
        kind: str = "taste",
        subject: str = "user",
        confidence: float = 0.8,
    ) -> None:
        self.concept_id = concept_id
        self.label = label
        self.kind = kind
        self.subject = subject
        self.confidence = confidence
        self.status = "active"


class _FakeView:
    """A ConceptView narrowed to the one read this worker performs."""

    def __init__(
        self,
        concepts: list[_FakeConcept] | None = None,
        *,
        enabled: bool = True,
        raises: bool = False,
    ) -> None:
        self._concepts = list(concepts or [])
        self.enabled = enabled
        self._raises = raises
        self.calls: list[str | None] = []

    def for_consumer(self, consumer, *, subject=None):
        self.calls.append(subject)
        if self._raises:
            raise RuntimeError("store is gone")
        return [
            c for c in self._concepts
            if subject is None or c.subject == subject
        ]


def _make_worker(
    *,
    store: _FakeMemoryStore,
    kv: _FakeKV,
    profile: _FakeProfileStore | None = None,
    view: _FakeView | None = None,
    enabled: bool = True,
    cooldown: float = 3600.0,
    cues=None,
    seed: int = 0,
    # K87 off by default so the existing cases stay about the two
    # user-centred pools they were written for.
    subject_quota: float = 0.0,
) -> ForwardCuriosityWorker:
    return ForwardCuriosityWorker(
        memory_store=store,
        kv_get=kv.get,
        kv_set=kv.set,
        user_id_provider=lambda: "jacob",
        user_display_name_provider=lambda: "Jacob",
        user_profile_store=profile,
        view_provider=(lambda: view) if view is not None else None,
        enabled_provider=lambda: enabled,
        cue_store_provider=(lambda: cues) if cues is not None else None,
        ollama=None,  # deterministic fallback
        model=None,
        interval_seconds=1800.0,
        cooldown_seconds=cooldown,
        journal_max=8,
        subject_quota_provider=lambda: subject_quota,
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


# ── K87: curiosity that isn't about him ─────────────────────────────


_SUBJECT_NOTE = (
    "Maybe bring up that cold brew tastes better on the second day."
)


class SubjectSourceTests(unittest.TestCase):
    def _store(self, *, depth: int = 8) -> _FakeMemoryStore:
        # Deep on both sides: the worker de-dups against the journal
        # ring, so a one-candidate pool would empty after a single draft
        # and every later run would look like a quota decision.
        return _FakeMemoryStore(
            future_plans=[
                _FakeMemory(
                    i, f"his package number {i} arriving Thursday",
                    temporal_type="future_plan",
                )
                for i in range(1, depth + 1)
            ],
            open_questions=[
                _FakeMemory(
                    100 + i,
                    f"Maybe bring up that cold brew number {i} tastes "
                    "better on the second day.",
                    kind="open_question",
                )
                for i in range(1, depth + 1)
            ],
        )

    def test_a_subject_note_becomes_a_statement(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            open_questions=[_FakeMemory(11, _SUBJECT_NOTE, kind="open_question")],
        )
        worker = _make_worker(
            store=store, kv=kv, cooldown=0.0, subject_quota=1.0,
        )
        result = worker.run()
        self.assertEqual(result["source"], "wondering")
        self.assertEqual(
            result["question"],
            "Cold brew tastes better on the second day.",
        )

    def test_a_note_that_asks_about_him_is_not_a_subject_source(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(7, "his move", temporal_type="future_plan"),
            ],
            open_questions=[
                _FakeMemory(
                    12,
                    "Maybe ask Jacob how his week went.",
                    kind="open_question",
                ),
            ],
        )
        worker = _make_worker(
            store=store, kv=kv, cooldown=0.0, subject_quota=1.0,
        )
        # A full quota with no subject candidate falls back to the
        # user-centred pool rather than treating the "ask" note as hers.
        self.assertEqual(worker.run()["source"], "future_plan")

    def test_a_zero_quota_never_draws_from_her_own_notes(self) -> None:
        kv = _FakeKV()
        for _ in range(5):
            worker = _make_worker(
                store=self._store(), kv=kv, cooldown=0.0, subject_quota=0.0,
            )
            self.assertEqual(worker.run()["source"], "future_plan")

    def test_the_quota_splits_the_two_pools(self) -> None:
        kv = _FakeKV()
        sources: list[str] = []
        for _ in range(8):
            worker = _make_worker(
                store=self._store(), kv=kv, cooldown=0.0, subject_quota=0.5,
            )
            sources.append(worker.run()["source"])
        self.assertEqual(sources.count("wondering"), 4)

    def test_only_one_pool_available_ignores_the_quota(self) -> None:
        # No user-centred candidates at all: a zero quota must not mean
        # "draft nothing".
        kv = _FakeKV()
        store = _FakeMemoryStore(
            open_questions=[_FakeMemory(11, _SUBJECT_NOTE, kind="open_question")],
        )
        worker = _make_worker(
            store=store, kv=kv, cooldown=0.0, subject_quota=0.0,
        )
        self.assertEqual(worker.run()["source"], "wondering")

    def test_the_cue_frames_it_as_hers_to_offer(self) -> None:
        line = render_question_cue({
            "question": "Cold brew tastes better on the second day.",
            "source": "wondering",
        })
        self.assertIn("yours to say", line)
        self.assertNotIn("You've been wondering", line)

    def test_a_user_centred_entry_keeps_the_question_frame(self) -> None:
        line = render_question_cue({
            "question": "how the move went",
            "source": "future_plan",
        })
        self.assertTrue(line.startswith("You've been wondering"))


class ConceptSourceTests(unittest.TestCase):
    """L28's fourth pool: standing things, not events.

    The three memory pools mean a plan, a callback or a note is the only
    thing she can be curious about -- she can ask how something went, but
    not whether a direction he is on still holds.
    """

    def test_a_concept_becomes_a_candidate(self) -> None:
        kv = _FakeKV()
        view = _FakeView([_FakeConcept(4, "he is drawn to slow crafts")])
        worker = _make_worker(
            store=_FakeMemoryStore(), kv=kv, view=view, cooldown=0.0,
        )
        result = worker.run()
        self.assertEqual(result["source"], "concept")
        self.assertEqual(result["source_id"], "concept:4")
        self.assertIn("slow crafts", result["question"])

    def test_the_question_asks_whether_it_still_holds(self) -> None:
        # The distinguishing shape: an event wants "how did it go?", a
        # standing read wants "is that still true?".
        kv = _FakeKV()
        view = _FakeView([_FakeConcept(4, "he is drawn to slow crafts")])
        worker = _make_worker(
            store=_FakeMemoryStore(), kv=kv, view=view, cooldown=0.0,
        )
        self.assertIn("still true", worker.run()["question"])

    def test_a_concept_of_hers_lands_on_the_subject_side(self) -> None:
        # The quota axis is whose it is, not which pool it came from.
        kv = _FakeKV()
        view = _FakeView([
            _FakeConcept(4, "she loves the quiet of early mornings",
                         subject="aiko"),
        ])
        worker = _make_worker(
            store=_FakeMemoryStore(), kv=kv, view=view, cooldown=0.0,
            subject_quota=1.0,
        )
        result = worker.run()
        self.assertEqual(result["source"], "concept")
        self.assertTrue(load_questions(kv.get)[0]["hers"])
        self.assertIn("yours to say", render_question_cue(load_questions(kv.get)[0]))

    def test_a_zero_quota_keeps_her_own_concepts_out(self) -> None:
        kv = _FakeKV()
        view = _FakeView([
            _FakeConcept(4, "she loves early mornings", subject="aiko"),
        ])
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(i, f"his package {i}", temporal_type="future_plan")
                for i in range(1, 6)
            ],
        )
        for _ in range(4):
            worker = _make_worker(
                store=store, kv=kv, view=view, cooldown=0.0, subject_quota=0.0,
            )
            self.assertEqual(worker.run()["source"], "future_plan")

    def test_the_ring_dedupes_a_concept_by_its_own_key(self) -> None:
        # ``concept:{id}`` rides the existing lineage paths rather than
        # introducing a fourth dedupe axis.
        kv = _FakeKV()
        view = _FakeView([_FakeConcept(4, "he is drawn to slow crafts")])
        store = _FakeMemoryStore()
        first = _make_worker(store=store, kv=kv, view=view, cooldown=0.0)
        self.assertEqual(first.run()["drafted"], 1)
        second = _make_worker(store=store, kv=kv, view=view, cooldown=0.0)
        self.assertEqual(second.run()["drafted"], 0)

    def test_the_pool_dedupes_a_concept_by_its_own_key(self) -> None:
        from tempfile import TemporaryDirectory

        from app.core.infra.chat_database import ChatDatabase
        from app.core.proactive.cue_store import CueStore

        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        cues = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))
        kv = _FakeKV()
        view = _FakeView([_FakeConcept(4, "he is drawn to slow crafts")])
        store = _FakeMemoryStore()
        self.assertEqual(
            _make_worker(
                store=store, kv=kv, view=view, cues=cues, cooldown=0.0,
            ).run()["drafted"],
            1,
        )
        # A fresh ring: the pool, not the journal, is what remembers now.
        kv.store.pop(FORWARD_CURIOSITY_JOURNAL_KEY, None)
        self.assertEqual(
            _make_worker(
                store=store, kv=kv, view=view, cues=cues, cooldown=0.0,
            ).run()["drafted"],
            0,
        )

    def test_no_view_leaves_the_three_memory_pools_alone(self) -> None:
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[_FakeMemory(7, "his move", temporal_type="future_plan")],
        )
        worker = _make_worker(store=store, kv=kv, cooldown=0.0)
        self.assertEqual(worker.run()["source"], "future_plan")

    def test_a_cold_or_broken_view_is_not_a_failed_tick(self) -> None:
        store = _FakeMemoryStore(
            future_plans=[_FakeMemory(7, "his move", temporal_type="future_plan")],
        )
        for view in (_FakeView(enabled=False), _FakeView(raises=True)):
            kv = _FakeKV()
            worker = _make_worker(
                store=store, kv=kv, view=view, cooldown=0.0,
            )
            self.assertEqual(worker.run()["source"], "future_plan")


class ConceptHintTests(unittest.TestCase):
    """The phrasing hint: concepts first, the K3 profile as the floor."""

    def _hint(self, **kwargs) -> str:
        worker = _make_worker(store=_FakeMemoryStore(), kv=_FakeKV(), **kwargs)
        return worker._context_hint()

    def test_concepts_lead_the_hint(self) -> None:
        hint = self._hint(
            view=_FakeView([_FakeConcept(1, "he is happiest mid-build")]),
            profile=_FakeProfileStore({"routines": "Monday check-ins"}),
        )
        self.assertLess(hint.index("mid-build"), hint.index("Monday"))

    def test_the_profile_stays_the_floor(self) -> None:
        # A cold concept layer must leave the hint no worse than the two
        # flat profile strings it used to be.
        hint = self._hint(
            view=_FakeView([]),
            profile=_FakeProfileStore(
                {"routines": "Monday check-ins", "usual_hours": "evenings"}
            ),
        )
        self.assertEqual(hint, "Monday check-ins; evenings")

    def test_only_his_concepts_are_asked_for(self) -> None:
        # A question about his life should not be phrased around her own
        # tastes.
        view = _FakeView([_FakeConcept(1, "he is happiest mid-build")])
        self._hint(view=view)
        self.assertIn("user", view.calls)

    def test_no_sources_at_all_is_an_empty_hint(self) -> None:
        self.assertEqual(self._hint(), "")


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

    def test_a_stocked_worker_declines_to_draft(self) -> None:
        """Zero demand is not enough; the scheduler admits it anyway.

        The observed failure: 14 pending against a target of 2, one more
        near-identical question every hour.
        """
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(i, f"plan {i}", temporal_type="future_plan")
                for i in range(6)
            ]
        )
        worker = self._worker(store, cooldown=0.0)
        worker.run()
        worker.run()
        result = worker.run()
        self.assertTrue(result.get("skipped_stocked"))
        self.assertEqual(result["stocked"], 2)
        self.assertEqual(self.cues.count_pending("forward_curiosity"), 2)


class LineageTests(unittest.TestCase):
    """One plan is one question, however many rows it left behind.

    The live failure this covers: three ``future_plan`` rows written ten
    minutes apart about the same cookie delivery produced three
    near-identical questions an hour apart, because the worker dedupes on
    the source row and each duplicate was a different row.
    """

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        from app.core.infra.chat_database import ChatDatabase
        from app.core.proactive.cue_store import CueStore

        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.cues = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _worker(self, store, kv=None, **kw) -> ForwardCuriosityWorker:
        return _make_worker(
            store=store, kv=kv or _FakeKV(), cues=self.cues, cooldown=0.0, **kw,
        )

    def test_the_same_source_is_not_redrafted_after_a_reword(self) -> None:
        """The pool remembers the source, not just the phrasing.

        The ring rotates and the subject text changes, so neither of the
        old gates caught this.
        """
        kv = _FakeKV()
        mem = _FakeMemory(
            2411, "cookies arrive in a few days", temporal_type="future_plan",
        )
        store = _FakeMemoryStore(future_plans=[mem])
        worker = self._worker(store, kv)
        self.assertEqual(worker.run()["drafted"], 1)
        kv.store.pop(FORWARD_CURIOSITY_JOURNAL_KEY, None)
        mem.content = "he expects his cookie order to arrive in a few days"
        self.assertTrue(worker.run().get("no_candidate"))

    def test_a_row_merged_into_another_is_not_a_plan_of_its_own(self) -> None:
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(
                    2218,
                    "surprise date in the near future",
                    temporal_type="future_plan",
                    metadata={"consolidated_into": 2241},
                )
            ]
        )
        self.assertTrue(self._worker(store).run().get("no_candidate"))

    def test_a_survivor_inherits_the_claims_of_what_it_absorbed(self) -> None:
        """A cue drafted before the merge still speaks for the group."""
        kv = _FakeKV()
        absorbed = _FakeMemory(
            2218, "surprise date soon", temporal_type="future_plan",
        )
        store = _FakeMemoryStore(future_plans=[absorbed])
        worker = self._worker(store, kv)
        self.assertEqual(worker.run()["drafted"], 1)

        # K35 folds 2218 into 2241; the primary now carries the content.
        kv.store.pop(FORWARD_CURIOSITY_JOURNAL_KEY, None)
        store._future = [
            _FakeMemory(
                2241,
                "he is actively planning a surprise date",
                temporal_type="future_plan",
                metadata={"source_ids": [2218, 2241]},
            )
        ]
        self.assertTrue(worker.run().get("no_candidate"))

    def test_an_unrelated_plan_still_gets_through(self) -> None:
        """The gate is lineage, not a blanket freeze on new questions."""
        kv = _FakeKV()
        store = _FakeMemoryStore(
            future_plans=[
                _FakeMemory(1, "the wedding", temporal_type="future_plan")
            ]
        )
        worker = self._worker(store, kv)
        self.assertEqual(worker.run()["drafted"], 1)
        store._future.append(
            _FakeMemory(2, "the interview", temporal_type="future_plan")
        )
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(self.cues.count_pending("forward_curiosity"), 2)


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

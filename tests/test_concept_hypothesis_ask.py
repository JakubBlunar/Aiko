"""L30b: from an unsettled belief to an actual question.

Two halves, matching the two sides of the cue pool:

* :class:`WorkerTests` -- what the
  :class:`ConceptHypothesisWorker` chooses to queue: the age exclusion
  that keeps it off beliefs an answer cannot move, ``importance x
  unsettledness`` ranking, and the "already asked" memory that stops a
  belief being raised twice across weeks.
* :class:`InventedPoolTests` -- the same worker's Phase B half, drawing
  from the ``hypotheses`` table instead of the concept graph: one ask per
  guess, nothing raised that a concept already speaks for.
* :class:`TopicPathTests` and friends -- when
  ``_render_concept_hypothesis_block`` turns a queued cue into a question:
  the two surfacing paths, the K47 question budget, the gap mutex, and
  the cross-lane guard against musing and asking about the same belief in
  one turn.

The provider runs against a real :class:`CueStore`, because the
surfaced / awaiting bookkeeping it drives is the store's state machine
and stubbing it would test nothing.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.concept_hypothesis_worker import (
    ConceptHypothesisWorker,
    render_hypothesis_cue,
)
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


# ── worker ───────────────────────────────────────────────────────────────


class _Concept(SimpleNamespace):
    pass


def _concept(
    cid: int,
    label: str,
    *,
    kind: str = "pattern",
    subject: str = "user",
) -> _Concept:
    return _Concept(
        concept_id=cid,
        label=label,
        kind=kind,
        subject=subject,
        embedding=np.asarray([1.0, 0.0], dtype=np.float32),
    )


class _FakeView:
    """A ConceptView narrowed to the one read the worker performs."""

    def __init__(self, rows: list[tuple[_Concept, float]]) -> None:
        self.enabled = True
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    def testable(self, **kwargs: Any) -> list[tuple[_Concept, float]]:
        self.calls.append(kwargs)
        return list(self._rows)


class _RaisingView(_FakeView):
    def testable(self, **kwargs: Any):
        raise RuntimeError("graph is unavailable")


class _WorkerFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _worker(
        self,
        view: _FakeView | None,
        *,
        importance: dict[int, float] | None = None,
        enabled: bool = True,
        max_per_run: int = 1,
    ) -> ConceptHypothesisWorker:
        ctx = None
        if importance is not None:
            ctx = SimpleNamespace(
                for_concept=lambda c: importance.get(c.concept_id, 0.5)
            )
        return ConceptHypothesisWorker(
            concept_view_provider=lambda: view,
            importance_context_provider=(
                (lambda _concepts: ctx) if ctx is not None else None
            ),
            enabled_provider=lambda: enabled,
            cue_store_provider=lambda: self.store,
            max_per_run=max_per_run,
        )

    def _pool(self) -> list:
        return [
            r
            for r in self.store.list_for_user()
            if r.cue_type == "concept_hypothesis"
        ]


class WorkerTests(_WorkerFixture):
    def test_it_queues_the_top_belief(self) -> None:
        view = _FakeView([(_concept(1, "Jacob walks to think"), 0.6)])
        result = self._worker(view).run()

        self.assertEqual(result["drafted"], 1)
        rows = self._pool()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload["target_type"], "concept")
        self.assertEqual(rows[0].payload["target_id"], 1)
        self.assertIn("Jacob walks to think", rows[0].text)

    def test_the_read_asks_for_testable_rows_only(self) -> None:
        # The age exclusion is the point of ``testable`` over
        # ``candidates``: an answer adds a source, so it cannot move a
        # belief that is merely waiting out its engaged-day floor.
        view = _FakeView([(_concept(1, "a belief"), 0.5)])
        self._worker(view).run()
        self.assertTrue(view.calls)

    def test_importance_breaks_the_unsettledness_ordering(self) -> None:
        # The less unsettled belief wins because it matters more:
        # 0.3 x 0.9 beats 0.8 x 0.2.
        view = _FakeView(
            [
                (_concept(1, "trivial but wide open"), 0.8),
                (_concept(2, "load-bearing hunch"), 0.3),
            ]
        )
        worker = self._worker(view, importance={1: 0.2, 2: 0.9})
        result = worker.run()

        self.assertEqual(result["questions"][0]["concept_id"], 2)

    def test_without_importance_it_ranks_on_unsettledness(self) -> None:
        # Importance is a lens, not data: when the affect join is missing
        # the lane keeps working on the neutral prior.
        view = _FakeView(
            [
                (_concept(1, "mild hunch"), 0.3),
                (_concept(2, "wide open hunch"), 0.9),
            ]
        )
        result = self._worker(view).run()
        self.assertEqual(result["questions"][0]["concept_id"], 2)

    def test_a_broken_importance_context_does_not_stop_the_run(self) -> None:
        def _boom(_concepts):
            raise RuntimeError("affect join is down")

        worker = ConceptHypothesisWorker(
            concept_view_provider=lambda: _FakeView(
                [(_concept(1, "a belief"), 0.5)]
            ),
            importance_context_provider=_boom,
            cue_store_provider=lambda: self.store,
        )
        self.assertEqual(worker.run()["drafted"], 1)

    def test_it_respects_max_per_run(self) -> None:
        view = _FakeView(
            [(_concept(i, f"belief {i}"), 0.5) for i in range(1, 6)]
        )
        self.assertEqual(self._worker(view, max_per_run=2).run()["drafted"], 2)

    def test_a_belief_is_never_asked_about_twice(self) -> None:
        # Broader than "still pending": one she asked about and got
        # nothing back from is the last one to raise again.
        view = _FakeView([(_concept(1, "Jacob walks to think"), 0.6)])
        worker = self._worker(view)
        self.assertEqual(worker.run()["drafted"], 1)

        for row in self._pool():
            self.store.mark_used(row.id)
        self.assertEqual(worker.run().get("all_asked"), True)
        self.assertEqual(len(self._pool()), 1)

    def test_force_next_overrides_the_asked_set(self) -> None:
        view = _FakeView([(_concept(1, "Jacob walks to think"), 0.6)])
        worker = self._worker(view)
        worker.run()
        for row in self._pool():
            self.store.mark_used(row.id)

        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)

    def test_force_next_is_one_shot(self) -> None:
        view = _FakeView([(_concept(1, "Jacob walks to think"), 0.6)])
        worker = self._worker(view)
        worker.force_next()
        worker.run()
        for row in self._pool():
            self.store.mark_used(row.id)
        self.assertEqual(worker.run().get("all_asked"), True)

    def test_disabled_writes_nothing(self) -> None:
        view = _FakeView([(_concept(1, "a belief"), 0.6)])
        result = self._worker(view, enabled=False).run()

        self.assertTrue(result["disabled"])
        self.assertEqual(self._pool(), [])

    def test_no_concept_graph_is_quiet(self) -> None:
        self.assertTrue(self._worker(None).run()["no_concepts"])

    def test_a_failing_read_is_quiet(self) -> None:
        result = self._worker(_RaisingView([])).run()
        self.assertTrue(result["no_candidate"])

    def test_an_empty_label_is_not_publishable(self) -> None:
        view = _FakeView([(_concept(1, "   "), 0.6)])
        self.assertEqual(self._worker(view).run()["drafted"], 0)

    def test_demand_reports_the_shortfall(self) -> None:
        signal = self._worker(_FakeView([])).demand(now=None, last_run_at=None)
        self.assertIsNotNone(signal)
        self.assertGreater(signal.pressure, 0.0)

    def test_disabled_reports_no_pressure(self) -> None:
        signal = self._worker(_FakeView([]), enabled=False).demand(
            now=None, last_run_at=None
        )
        self.assertEqual(signal.pressure, 0.0)


class InventedPoolTests(_WorkerFixture):
    """The Phase B half of the same worker.

    An invented guess is queued from the ``hypotheses`` table rather than
    the concept graph, and the ask is routed back to it by
    ``target_type`` in the payload. The filters below are the whole
    difference: a row that is finished, already spoken for by a concept,
    or already asked once must not be raised.
    """

    def setUp(self) -> None:
        from app.core.concepts.hypothesis_store import HypothesisStore

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.store = CueStore(db)
        self.hypotheses = HypothesisStore(db)

    def _invented(
        self,
        statement: str = "Jacob would take to sailing",
        *,
        kind: str = "taste",
        credence: float = 0.4,
        support: int = 0,
        status: str | None = None,
        linked: int | None = None,
        asked: int = 0,
    ):
        from app.core.concepts.hypothesis_store import Hypothesis

        row = Hypothesis(
            statement=statement,
            kind=kind,
            credence=credence,
            support_count=support,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        if status:
            row.status = status
        row.linked_concept_id = linked
        row.asked_count = asked
        self.hypotheses.add(row)
        return row

    def _worker(self, view=None, **kw) -> ConceptHypothesisWorker:  # type: ignore[override]
        worker = super()._worker(view or _FakeView([]), **kw)
        worker._hypothesis_store_provider = lambda: self.hypotheses
        return worker

    def test_an_invented_guess_gets_its_own_cue(self) -> None:
        row = self._invented()

        result = self._worker().run()

        self.assertEqual(result["invented"][0]["hypothesis_id"],
                         row.hypothesis_id)
        cue = self._pool()[0]
        self.assertEqual(cue.payload["target_type"], "hypothesis")
        self.assertEqual(cue.payload["target_id"], row.hypothesis_id)

    def test_the_cue_admits_she_made_it_up(self) -> None:
        self._invented()

        self._worker().run()

        self.assertIn("made it up", self._pool()[0].text)

    def test_both_pools_can_queue_in_one_run(self) -> None:
        """They are separate shelves, not competitors for one slot."""
        self._invented()
        view = _FakeView([(_concept(1, "Jacob walks to think"), 0.6)])

        result = self._worker(view).run()

        self.assertEqual(result["drafted"], 2)
        self.assertEqual(
            {r.payload["target_type"] for r in self._pool()},
            {"concept", "hypothesis"},
        )

    def test_a_finished_guess_is_never_raised(self) -> None:
        from app.core.concepts.hypothesis_store import STATUS_REFUTED

        self._invented(status=STATUS_REFUTED)

        self.assertEqual(self._worker().run()["drafted"], 0)

    def test_a_linked_guess_is_never_raised(self) -> None:
        """A concept speaks for it now; asking would re-open a settled thing."""
        self._invented(linked=7)

        self.assertEqual(self._worker().run()["drafted"], 0)

    def test_one_ask_per_guess(self) -> None:
        self._invented()
        worker = self._worker()
        worker.run()

        self.assertEqual(worker.run()["drafted"], 0)

    def test_publishing_is_what_spends_the_ask(self) -> None:
        row = self._invented()

        self._worker().run()

        self.assertEqual(
            self.hypotheses.get(row.hypothesis_id).asked_count, 1
        )

    def test_an_empty_statement_does_not_spend_an_ask(self) -> None:
        row = self._invented("   ")

        self._worker().run()

        self.assertEqual(
            self.hypotheses.get(row.hypothesis_id).asked_count, 0
        )

    def test_force_next_re_raises_a_spent_guess(self) -> None:
        self._invented()
        worker = self._worker()
        worker.run()

        worker.force_next()

        self.assertEqual(worker.run()["drafted"], 1)

    def test_the_least_settled_guess_goes_first(self) -> None:
        self._invented("nearly settled", credence=0.9, support=2)
        self._invented("wide open", credence=0.4)

        result = self._worker().run()

        self.assertEqual(result["invented"][0]["statement"], "wide open")

    def test_no_hypothesis_table_leaves_the_grounded_half_working(
        self,
    ) -> None:
        view = _FakeView([(_concept(1, "Jacob walks to think"), 0.6)])
        worker = super()._worker(view)

        self.assertEqual(worker.run()["drafted"], 1)

    def test_a_failing_read_is_quiet(self) -> None:
        class _Broken:
            def list_by(self, **_k):
                raise RuntimeError("db gone")

        worker = self._worker()
        worker._hypothesis_store_provider = lambda: _Broken()

        self.assertEqual(worker.run()["drafted"], 0)


class CueTextTests(unittest.TestCase):
    def test_the_cue_states_the_belief_and_its_uncertainty(self) -> None:
        text = render_hypothesis_cue("Jacob walks to think", "user")
        self.assertIn("Jacob walks to think", text)
        self.assertIn("about them", text)

    def test_the_invented_cue_never_claims_an_observation(self) -> None:
        from app.core.proactive.concept_hypothesis_worker import (
            render_invented_cue,
        )

        text = render_invented_cue("Jacob would take to sailing", "user")

        self.assertIn("Jacob would take to sailing", text)
        self.assertIn("nothing behind it", text)
        self.assertNotIn("noticed", text)

    def test_the_subject_changes_who_it_is_about(self) -> None:
        self.assertIn(
            "about yourself", render_hypothesis_cue("she rushes", "aiko"),
        )
        self.assertIn(
            "about the two of you",
            render_hypothesis_cue("they tease", "relationship"),
        )


# ── provider ─────────────────────────────────────────────────────────────


class _Overrides:
    def __init__(self) -> None:
        self._armed: dict[str, Any] = {}

    def arm(self, key: str, value: Any) -> None:
        self._armed[key] = value

    def take(self, key: str, default: Any = None) -> Any:
        return self._armed.pop(key, default)


class _Host(CuePoolMixin, InnerLifeProvidersMixin):
    """A SessionController stripped to what the provider touches."""

    def __init__(
        self,
        store: CueStore,
        *,
        enabled: bool = True,
        k47_armed: bool = False,
        pending_seconds: float | None = None,
        gap_cue_surfaced: bool = False,
        min_gap_hours: float = 4.0,
        gap_min_importance: float = 0.55,
        lane_claimed: set[int] | None = None,
    ) -> None:
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = None
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(concept_hypothesis_ask_enabled=enabled),
        )
        self._memory_settings = SimpleNamespace(
            concept_hypothesis_min_gap_hours=min_gap_hours,
            concept_hypothesis_gap_min_importance=gap_min_importance,
        )
        self._pending_concept_hypothesis_seconds = pending_seconds
        self._gap_cue_surfaced = gap_cue_surfaced
        self._last_hypothesis_lane_concept_ids = set(lane_claimed or ())
        self._debug_overrides = _Overrides()
        self.k47_armed = k47_armed
        self.user_display_name = "Jacob"

    def _question_balance_suppressed(self) -> bool:
        return self.k47_armed


class _ProviderFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _queue(
        self,
        label: str = "Jacob walks to think",
        *,
        concept_id: int = 1,
        importance: float = 0.8,
    ) -> int:
        return self.store.add(
            "concept_hypothesis",
            label,
            render_hypothesis_cue(label, "user"),
            payload={
                "target_type": "concept",
                "target_id": concept_id,
                "label": label,
                "kind": "pattern",
                "subject": "user",
                "importance": importance,
                "unsettled": 0.6,
            },
        )

    def _state(self, cue_id: int) -> str:
        return next(
            r.state for r in self.store.list_for_user() if r.id == cue_id
        )


class TopicPathTests(_ProviderFixture):
    def test_a_matching_message_raises_the_belief(self) -> None:
        cue_id = self._queue("Jacob walks to think")
        host = _Host(self.store)

        block = host._render_concept_hypothesis_block(
            "always walks better than I sit"
        )
        self.assertIn("Jacob walks to think", block)
        self.assertEqual(self._state(cue_id), "surfaced")

    def test_an_unrelated_message_leaves_it_queued(self) -> None:
        cue_id = self._queue("Jacob walks to think")
        host = _Host(self.store)

        self.assertEqual(
            host._render_concept_hypothesis_block("the build is broken again"),
            "",
        )
        self.assertEqual(self._state(cue_id), "pending")

    def test_the_topic_path_does_not_spend_the_gap_slot(self) -> None:
        # Matching ``knowledge_gap_notice``: riding a topic the user
        # raised is not the same as opening out of silence, so the other
        # gap cues on this turn must still be free to fire.
        self._queue("Jacob walks to think")
        host = _Host(self.store)
        block = host._render_concept_hypothesis_block("he walks everywhere")
        self.assertNotEqual(block, "")
        self.assertFalse(host._gap_cue_surfaced)

    def test_the_topic_path_ignores_a_gap_cue_already_surfaced(self) -> None:
        self._queue("Jacob walks to think")
        host = _Host(self.store, gap_cue_surfaced=True)
        self.assertIn(
            "Jacob walks to think",
            host._render_concept_hypothesis_block("she walks everywhere"),
        )

    def test_the_pending_slot_is_cleared_even_on_the_topic_path(self) -> None:
        # Otherwise the cue type is spent for the turn while the slot
        # stays armed, and a stale lull opens a second probe next turn.
        self._queue("Jacob walks to think")
        host = _Host(self.store, pending_seconds=9 * 3600.0)
        block = host._render_concept_hypothesis_block("he walks everywhere")
        self.assertNotEqual(block, "")
        self.assertIsNone(host._pending_concept_hypothesis_seconds)


class GapPathTests(_ProviderFixture):
    def test_a_long_gap_raises_a_weighty_belief(self) -> None:
        cue_id = self._queue(importance=0.8)
        host = _Host(self.store, pending_seconds=9 * 3600.0)

        self.assertIn(
            "Jacob walks to think", host._render_concept_hypothesis_block(""),
        )
        self.assertEqual(self._state(cue_id), "surfaced")

    def test_the_gap_path_spends_the_gap_slot(self) -> None:
        self._queue()
        host = _Host(self.store, pending_seconds=9 * 3600.0)
        host._render_concept_hypothesis_block("")
        self.assertTrue(host._gap_cue_surfaced)

    def test_it_defers_to_every_other_gap_cue(self) -> None:
        # Last in ``GAP_CUE_ORDER``: raising a belief about someone out of
        # silence is the heaviest thing she can open with.
        cue_id = self._queue()
        host = _Host(
            self.store, pending_seconds=9 * 3600.0, gap_cue_surfaced=True,
        )
        self.assertEqual(host._render_concept_hypothesis_block(""), "")
        self.assertEqual(self._state(cue_id), "pending")

    def test_an_unarmed_slot_stays_quiet(self) -> None:
        self._queue()
        host = _Host(self.store, pending_seconds=None)
        self.assertEqual(host._render_concept_hypothesis_block(""), "")

    def test_a_short_gap_stays_quiet(self) -> None:
        self._queue()
        host = _Host(self.store, pending_seconds=600.0)
        self.assertEqual(host._render_concept_hypothesis_block(""), "")

    def test_a_minor_hunch_is_not_worth_a_lull(self) -> None:
        # The extra bar the topic path does not have: out of silence,
        # only a belief that matters justifies the weight.
        cue_id = self._queue(importance=0.2)
        host = _Host(self.store, pending_seconds=9 * 3600.0)

        self.assertEqual(host._render_concept_hypothesis_block(""), "")
        self.assertEqual(self._state(cue_id), "pending")

    def test_a_minor_hunch_can_still_ride_a_topic(self) -> None:
        self._queue(importance=0.1)
        host = _Host(self.store)
        self.assertNotEqual(
            host._render_concept_hypothesis_block("he walks everywhere"), "",
        )

    def test_the_slot_is_one_shot(self) -> None:
        self._queue()
        self._queue("Jacob keeps odd hours", concept_id=2)
        host = _Host(self.store, pending_seconds=9 * 3600.0)

        self.assertNotEqual(host._render_concept_hypothesis_block(""), "")
        host._gap_cue_surfaced = False
        self.assertEqual(host._render_concept_hypothesis_block(""), "")


class QuestionBudgetTests(_ProviderFixture):
    def test_the_k47_gate_silences_both_paths(self) -> None:
        # This block exists to produce a question, so it belongs under the
        # question budget -- unlike the L30a musing lane, which costs the
        # user nothing.
        cue_id = self._queue()
        for user_text, seconds in (("out on a walk", None), ("", 9 * 3600.0)):
            with self.subTest(text=user_text):
                host = _Host(
                    self.store, k47_armed=True, pending_seconds=seconds,
                )
                self.assertEqual(
                    host._render_concept_hypothesis_block(user_text), "",
                )
                self.assertEqual(self._state(cue_id), "pending")

    def test_the_gate_does_not_consume_the_pending_slot(self) -> None:
        # A suppressed turn must not burn the lull: the opening is still
        # there once the budget recovers.
        self._queue()
        host = _Host(
            self.store, k47_armed=True, pending_seconds=9 * 3600.0,
        )
        host._render_concept_hypothesis_block("")
        self.assertEqual(host._pending_concept_hypothesis_seconds, 9 * 3600.0)

    def test_the_master_switch_silences_it(self) -> None:
        self._queue()
        host = _Host(
            self.store, enabled=False, pending_seconds=9 * 3600.0,
        )
        self.assertEqual(host._render_concept_hypothesis_block(""), "")


class CrossLaneTests(_ProviderFixture):
    def test_it_skips_a_belief_the_musing_lane_already_took(self) -> None:
        # One turn must never carry both "I half-wonder whether X" at T3
        # and "ask X" at T6.
        cue_id = self._queue(concept_id=7)
        host = _Host(self.store, lane_claimed={7})
        text = "he walks everywhere"
        # The same message with the guard clear does surface it, so this
        # is the guard talking and not a topic miss.
        self.assertEqual(host._render_concept_hypothesis_block(text), "")
        self.assertEqual(self._state(cue_id), "pending")
        self.assertNotEqual(
            _Host(self.store)._render_concept_hypothesis_block(text), "",
        )

    def test_the_guard_also_covers_the_gap_path(self) -> None:
        self._queue(concept_id=7)
        host = _Host(
            self.store, lane_claimed={7}, pending_seconds=9 * 3600.0,
        )
        self.assertEqual(host._render_concept_hypothesis_block(""), "")

    def test_a_different_belief_is_unaffected(self) -> None:
        self._queue("Jacob walks to think", concept_id=7)
        self._queue("Jacob prefers mornings", concept_id=8)
        host = _Host(self.store, lane_claimed={7})

        block = host._render_concept_hypothesis_block("mornings are best")
        self.assertIn("Jacob prefers mornings", block)


class ForceTests(_ProviderFixture):
    def test_force_bypasses_the_topic_slot_and_importance_gates(self) -> None:
        self._queue(importance=0.05)
        host = _Host(self.store)
        host._debug_overrides.arm("concept_hypothesis_force_next", True)

        self.assertNotEqual(host._render_concept_hypothesis_block(""), "")

    def test_force_cannot_invent_a_cue(self) -> None:
        host = _Host(self.store)
        host._debug_overrides.arm("concept_hypothesis_force_next", True)
        self.assertEqual(host._render_concept_hypothesis_block(""), "")

    def test_force_still_respects_the_question_budget(self) -> None:
        self._queue()
        host = _Host(self.store, k47_armed=True)
        host._debug_overrides.arm("concept_hypothesis_force_next", True)
        self.assertEqual(host._render_concept_hypothesis_block(""), "")


class EmptyPoolTests(_ProviderFixture):
    def test_nothing_queued_renders_nothing(self) -> None:
        host = _Host(self.store, pending_seconds=9 * 3600.0)
        self.assertEqual(host._render_concept_hypothesis_block("a walk"), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

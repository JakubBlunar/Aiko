"""L30 Phase B: how a guess stops being a guess.

Three exits and one race, against real stores on a throwaway database --
the whole subject here is what lands in the *concept* table, so a mock
concept store would test nothing.

The race is not an edge case and gets the most coverage. A confirmed
hypothesis stores the user's answer as an ordinary memory; L2 clusters
that memory and proposes a concept from it knowing nothing about the
hypothesis, and L2 needs one confirmation where graduation needs two. So
"the belief already exists by the time I graduate" is the *usual*
ending, and forking a near-twin instead of merging would be the layer
quietly corrupting the graph it feeds.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts import hypothesis_graduation as hg
from app.core.concepts.concept_dedupe import DEDUPE_COS
from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.hypothesis_store import (
    STATUS_GRADUATED,
    STATUS_MERGED,
    STATUS_OPEN,
    STATUS_REFUTED,
    SUBJECT_WORLD,
    Hypothesis,
    HypothesisStore,
)
from app.core.infra.chat_database import ChatDatabase


def _unit(*xs: float) -> np.ndarray:
    v = np.asarray(xs, dtype=np.float32)
    return v / float(np.linalg.norm(v))


#: A vector and a near-twin of it. The cosine between them sits above
#: ``DEDUPE_COS`` so the pair is what the duplicate lookup calls "the
#: same belief"; ``_FAR`` is orthogonal to both.
_NEAR_A = _unit(1.0, 0.0)
_NEAR_B = _unit(0.98, 0.2)
_FAR = _unit(0.0, 1.0)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "test.db")
        self.concepts = ConceptStore(self.db)
        self.hypotheses = HypothesisStore(self.db)
        self.events = ConceptEventStore(self.db)
        self.assertGreaterEqual(
            float(np.dot(_NEAR_A, _NEAR_B)),
            DEDUPE_COS,
            "fixture vectors must read as the same belief",
        )

    # ── builders ──────────────────────────────────────────────────────

    def _hypothesis(
        self,
        statement: str = "Jacob edits his code when he is avoiding a decision",
        *,
        subject: str = "user",
        kind: str = "pattern",
        embedding: np.ndarray | None = None,
        support: int = 2,
        credence: float = 0.8,
        answers: tuple[int, ...] = (501, 502),
        status: str = STATUS_OPEN,
    ) -> Hypothesis:
        row = Hypothesis(
            statement=statement,
            kind=kind,
            subject=subject,
            rationale="he reformats before every big call",
            credence=credence,
            support_count=support,
            status=status,
            embedding=_NEAR_A if embedding is None else embedding,
            answer_memory_ids=list(answers),
        )
        self.hypotheses.add(row)
        return row

    def _concept(
        self,
        label: str = "Jacob edits code to avoid deciding",
        *,
        subject: str = "user",
        kind: str = "identity",
        status: str = "active",
        embedding: np.ndarray | None = None,
    ) -> Concept:
        c = Concept(
            label=label,
            kind=kind,
            subject=subject,
            status=status,
            confidence=0.6,
            embedding=_NEAR_B if embedding is None else embedding,
        )
        c.concept_id = self.concepts.add(c)
        return c

    # ── reads ─────────────────────────────────────────────────────────

    def _graduate(self, row: Hypothesis, **kw):
        return hg.graduate(
            hypothesis_store=self.hypotheses,
            concept_store=self.concepts,
            row=row,
            event_store=self.events,
            **kw,
        )

    def _evidence_ids(self, concept_id: int) -> set[str]:
        return {
            e.src_id
            for e in self.concepts.evidence_of(concept_id)
            if e.src_type == "memory"
        }


# ── exit 1: a new candidate concept ───────────────────────────────────


class MintTests(_Fixture):
    def test_a_proven_guess_becomes_a_candidate_concept(self) -> None:
        row = self._hypothesis()

        result = self._graduate(row)

        self.assertEqual(result.exit, hg.EXIT_GRADUATED)
        concept = self.concepts.get(result.concept_id)
        self.assertEqual(concept.label, row.statement)
        self.assertEqual(concept.subject, "user")

    def test_it_enters_as_a_candidate_and_waits_for_l3(self) -> None:
        """Being guessed right twice earns entry, not promotion."""
        result = self._graduate(self._hypothesis())

        concept = self.concepts.get(result.concept_id)
        self.assertEqual(concept.status, "candidate")
        self.assertLess(concept.confidence, 0.9)

    def test_the_answers_arrive_as_evidence_edges(self) -> None:
        result = self._graduate(self._hypothesis(answers=(77, 78)))

        self.assertEqual(
            self._evidence_ids(result.concept_id), {"77", "78"}
        )

    def test_the_evidence_counters_are_recounted_not_asserted(self) -> None:
        result = self._graduate(self._hypothesis(answers=(77, 78, 78)))

        concept = self.concepts.get(result.concept_id)
        self.assertEqual(concept.evidence_count, 2)
        self.assertEqual(concept.distinct_source_count, 2)

    def test_the_row_closes_pointing_at_what_it_became(self) -> None:
        row = self._hypothesis()

        result = self._graduate(row)

        self.assertEqual(row.status, STATUS_GRADUATED)
        self.assertEqual(row.graduated_concept_id, result.concept_id)
        self.assertFalse(row.is_live)

    def test_the_provenance_survives_into_the_rationale(self) -> None:
        result = self._graduate(self._hypothesis())

        rationale = self.concepts.get(result.concept_id).rationale
        self.assertIn("hunch", rationale.lower())
        self.assertIn("he reformats", rationale)

    def test_a_meta_kind_degrades_rather_than_lying_about_its_edges(
        self,
    ) -> None:
        """A graduated concept's evidence is a set of answer memories.

        It has no base concepts and no chain, so claiming ``meta`` or
        ``sequence`` would store a shape its edges contradict.
        """
        result = self._graduate(self._hypothesis(kind="meta_pattern"))

        self.assertIn(
            self.concepts.get(result.concept_id).evidence_model,
            {"set", "recurring"},
        )

    def test_it_records_a_graduated_event(self) -> None:
        self._graduate(self._hypothesis())

        types = [e.event_type for e in self.events.list(limit=5)]
        self.assertIn(hg.EVENT_GRADUATED, types)


# ── exit 2: merged into a belief that already existed ─────────────────


class MergeTests(_Fixture):
    def test_a_twin_concept_takes_the_merge_exit(self) -> None:
        existing = self._concept()
        row = self._hypothesis()

        result = self._graduate(row)

        self.assertEqual(result.exit, hg.EXIT_MERGED)
        self.assertEqual(result.concept_id, existing.concept_id)

    def test_merging_does_not_fork_a_second_concept(self) -> None:
        self._concept()

        self._graduate(self._hypothesis())

        self.assertEqual(self.concepts.count(), 1)

    def test_the_answers_land_on_the_concept_that_already_held_it(
        self,
    ) -> None:
        existing = self._concept()

        self._graduate(self._hypothesis(answers=(91, 92)))

        self.assertEqual(
            self._evidence_ids(existing.concept_id), {"91", "92"}
        )

    def test_the_row_closes_as_merged_not_graduated(self) -> None:
        """L17f narrates the two differently, so they are two statuses."""
        self._concept()
        row = self._hypothesis()

        self._graduate(row)

        self.assertEqual(row.status, STATUS_MERGED)
        self.assertNotEqual(row.status, STATUS_GRADUATED)

    def test_it_records_a_merged_event(self) -> None:
        self._concept()

        self._graduate(self._hypothesis())

        types = [e.event_type for e in self.events.list(limit=5)]
        self.assertIn(hg.EVENT_MERGED, types)
        self.assertNotIn(hg.EVENT_GRADUATED, types)

    def test_merging_leaves_confidence_and_status_to_l3(self) -> None:
        existing = self._concept(status="candidate")
        before = existing.confidence

        self._graduate(self._hypothesis())

        merged = self.concepts.get(existing.concept_id)
        self.assertAlmostEqual(merged.confidence, before)
        self.assertEqual(merged.status, "candidate")

    def test_it_stamps_the_reinforcement_time(self) -> None:
        existing = self._concept()
        self.assertFalse(existing.last_reinforced_at)

        self._graduate(self._hypothesis())

        self.assertTrue(self.concepts.get(existing.concept_id).last_reinforced_at)


# ── exit 3: anchored as a durable memory ──────────────────────────────


class AnchorTests(_Fixture):
    def test_a_world_guess_becomes_a_memory_not_a_concept(self) -> None:
        row = self._hypothesis(
            "Espresso pucks channel when the grind is too coarse",
            subject=SUBJECT_WORLD,
        )

        result = self._graduate(row, memory_writer=lambda _s: 4242)

        self.assertEqual(result.exit, hg.EXIT_ANCHORED)
        self.assertEqual(result.memory_id, 4242)
        self.assertEqual(self.concepts.count(), 0)

    def test_the_statement_is_what_gets_written(self) -> None:
        seen: list[str] = []
        row = self._hypothesis("Cold brew needs a coarser grind", subject=SUBJECT_WORLD)

        self._graduate(row, memory_writer=lambda s: seen.append(s) or 7)

        self.assertEqual(seen, ["Cold brew needs a coarser grind"])

    def test_a_world_guess_skips_the_duplicate_check_entirely(self) -> None:
        """There is no concept subject for how something works.

        A near-cosine concept about the *user* is a coincidence of
        wording, and merging into it would be a category error.
        """
        self._concept(subject="user")
        row = self._hypothesis(subject=SUBJECT_WORLD)

        result = self._graduate(row, memory_writer=lambda _s: 5)

        self.assertEqual(result.exit, hg.EXIT_ANCHORED)
        self.assertIsNone(row.linked_concept_id)

    def test_a_missing_writer_still_closes_the_row(self) -> None:
        """Better to lose the memory than strand a proven guess."""
        row = self._hypothesis(subject=SUBJECT_WORLD)

        result = self._graduate(row)

        self.assertEqual(result.exit, hg.EXIT_ANCHORED)
        self.assertIsNone(result.memory_id)
        self.assertEqual(row.status, STATUS_GRADUATED)

    def test_a_raising_writer_is_not_fatal(self) -> None:
        def _boom(_s: str) -> int:
            raise RuntimeError("disk on fire")

        row = self._hypothesis(subject=SUBJECT_WORLD)

        result = self._graduate(row, memory_writer=_boom)

        self.assertEqual(result.exit, hg.EXIT_ANCHORED)
        self.assertEqual(row.status, STATUS_GRADUATED)


# ── the duplicate race ────────────────────────────────────────────────


class LinkTests(_Fixture):
    """``link_if_duplicate`` runs on *every* confirmation, not at the end."""

    def _link(self, row: Hypothesis, memory_id: int | None = None):
        return hg.link_if_duplicate(
            hypothesis_store=self.hypotheses,
            concept_store=self.concepts,
            row=row,
            memory_id=memory_id,
        )

    def test_the_link_is_stamped_on_the_first_confirmation(self) -> None:
        existing = self._concept()
        row = self._hypothesis(support=1)

        self._link(row, memory_id=31)

        self.assertEqual(row.linked_concept_id, existing.concept_id)

    def test_the_answer_attaches_to_the_concept_immediately(self) -> None:
        """Otherwise the first answer piles up unused until graduation."""
        existing = self._concept()

        self._link(self._hypothesis(support=1), memory_id=31)

        self.assertEqual(self._evidence_ids(existing.concept_id), {"31"})

    def test_a_distant_concept_is_not_a_link(self) -> None:
        self._concept(embedding=_FAR)
        row = self._hypothesis(support=1)

        self.assertIsNone(self._link(row))
        self.assertIsNone(row.linked_concept_id)

    def test_relinking_is_idempotent_and_keeps_attaching(self) -> None:
        existing = self._concept()
        row = self._hypothesis(support=1)

        self._link(row, memory_id=31)
        self._link(row, memory_id=32)

        self.assertEqual(row.linked_concept_id, existing.concept_id)
        self.assertEqual(
            self._evidence_ids(existing.concept_id), {"31", "32"}
        )

    def test_a_dead_link_is_cleared_rather_than_chased(self) -> None:
        stale = self._concept(embedding=_FAR)
        row = self._hypothesis(support=1)
        self.hypotheses.link(row, stale.concept_id)
        self.concepts.delete(stale.concept_id)
        live = self._concept()

        got = self._link(row)

        self.assertEqual(got.concept_id, live.concept_id)
        self.assertEqual(row.linked_concept_id, live.concept_id)

    def test_a_retired_belief_is_still_the_same_belief(self) -> None:
        """Arriving at it again should revive history, not fork a row."""
        retired = self._concept(status="retired")
        row = self._hypothesis(support=1)

        got = self._link(row)

        self.assertEqual(got.concept_id, retired.concept_id)

    def test_a_dormant_belief_matches_too(self) -> None:
        dormant = self._concept(status="dormant")

        got = self._link(self._hypothesis(support=1))

        self.assertEqual(got.concept_id, dormant.concept_id)

    def test_a_kind_disagreement_does_not_fork_the_graph(self) -> None:
        """The proposer's guessed kind carries no authority.

        L2 derived the same belief and filed it under ``identity``; the
        guess called it a ``pattern``. Filtering on kind would miss the
        duplicate and mint a near-twin.
        """
        existing = self._concept(kind="identity")
        row = self._hypothesis(kind="pattern")

        self.assertEqual(self._link(row).concept_id, existing.concept_id)

    def test_a_world_row_never_links(self) -> None:
        self._concept()
        row = self._hypothesis(subject=SUBJECT_WORLD)

        self.assertIsNone(self._link(row))

    def test_an_unembedded_row_cannot_be_matched(self) -> None:
        self._concept()
        row = self._hypothesis(embedding=np.zeros(0, dtype=np.float32))

        self.assertIsNone(self._link(row))

    def test_a_linked_row_takes_the_merge_exit_at_graduation(self) -> None:
        existing = self._concept()
        row = self._hypothesis(support=1)
        self._link(row, memory_id=31)
        row.support_count = 2

        result = self._graduate(row)

        self.assertEqual(result.exit, hg.EXIT_MERGED)
        self.assertEqual(result.concept_id, existing.concept_id)
        self.assertEqual(self.concepts.count(), 1)


# ── the graduation bar ────────────────────────────────────────────────


class ReadinessTests(_Fixture):
    def _ready(self, row: Hypothesis, **kw) -> bool:
        return hg.is_ready(
            row, **{"min_support": 2, "min_credence": 0.7, **kw}
        )

    def test_two_confirmations_and_enough_credence_earn_the_exit(self) -> None:
        self.assertTrue(self._ready(self._hypothesis(support=2, credence=0.8)))

    def test_one_confirmation_is_not_enough(self) -> None:
        self.assertFalse(self._ready(self._hypothesis(support=1)))

    def test_a_low_credence_row_waits(self) -> None:
        self.assertFalse(
            self._ready(self._hypothesis(support=3, credence=0.4))
        )

    def test_a_single_contradiction_disqualifies_it_outright(self) -> None:
        """The 'no' was about this belief; the yesses may have been polite."""
        row = self._hypothesis(support=3, credence=0.95)
        row.refute_count = 1

        self.assertFalse(self._ready(row))

    def test_a_closed_row_cannot_graduate_twice(self) -> None:
        row = self._hypothesis(support=3, status=STATUS_REFUTED)

        self.assertFalse(self._ready(row))

    def test_a_linked_row_is_held_to_the_same_bar(self) -> None:
        """The link is a cosine judgement, not a proof of the guess."""
        self._concept()
        row = self._hypothesis(support=1)
        self.hypotheses.link(row, 1)

        self.assertFalse(self._ready(row))


# ── the isolation guarantee ───────────────────────────────────────────


class IsolationTests(_Fixture):
    """An invention must not reach the concept graph before it earns it."""

    def test_an_open_hypothesis_is_invisible_to_the_concept_store(
        self,
    ) -> None:
        self._hypothesis(support=0, credence=0.4)

        self.assertEqual(self.concepts.count(), 0)
        self.assertEqual(self.concepts.list_by(), [])

    def test_a_confirmed_but_ungraduated_guess_mints_nothing(self) -> None:
        """One confirmation links (if a twin exists) but never creates."""
        row = self._hypothesis(support=1)

        hg.link_if_duplicate(
            hypothesis_store=self.hypotheses,
            concept_store=self.concepts,
            row=row,
            memory_id=5,
        )

        self.assertEqual(self.concepts.count(), 0)

    def test_only_graduation_writes_to_the_graph(self) -> None:
        row = self._hypothesis()
        self.assertEqual(self.concepts.count(), 0)

        self._graduate(row)

        self.assertEqual(self.concepts.count(), 1)

    def test_an_ungraduated_guess_reaches_no_prompt_lane(self) -> None:
        """Including the T0 profile block, which asserts what it holds."""
        from app.core.concepts.concept_view import ConceptView

        self._hypothesis(support=1)
        view = ConceptView(self.concepts)

        self.assertEqual(list(view.core()), [])
        self.assertEqual(
            list(view.for_target("profile_block", subject="user")), []
        )
        self.assertEqual(list(view.relevant(_NEAR_A, k=5)), [])

    def test_even_graduated_it_enters_the_tentative_register_first(
        self,
    ) -> None:
        """Graduation buys entry as a candidate, not a claim in the profile."""
        from app.core.concepts.concept_view import ConceptView

        self._graduate(self._hypothesis())
        self.concepts.load_all()
        view = ConceptView(self.concepts)

        self.assertEqual(
            list(view.for_target("profile_block", subject="user")), []
        )
        self.assertEqual(
            [c.concept_id for c, _s in view.hypotheses(_NEAR_A, k=5)],
            [1],
        )


class LaneRaceTests(_Fixture):
    """The surfacing half of the duplicate race.

    While a guess sits at one confirmation, L2 may already have minted a
    concept from its own answer memory. Both would then render as open
    questions about one belief -- the lane asking "is it true that X?"
    directly under the tentative register musing about X.
    """

    def _lane(self):
        from app.core.concepts.hypothesis_lane import nearest_invented

        return nearest_invented(self.hypotheses, _NEAR_A, k=5)

    def test_an_open_guess_is_offered_to_the_lane(self) -> None:
        row = self._hypothesis(support=0)

        self.assertEqual(
            [r.hypothesis_id for r, _s in self._lane()], [row.hypothesis_id]
        )

    def test_a_linked_guess_goes_quiet(self) -> None:
        row = self._hypothesis(support=1)
        self.hypotheses.link(row, 42)

        self.assertEqual(self._lane(), [])

    def test_confirming_against_a_twin_silences_the_lane_slot(self) -> None:
        """End to end: the concept exists, so the guess stops asking."""
        self._concept()
        row = self._hypothesis(support=1)
        self.assertEqual(len(self._lane()), 1)

        hg.link_if_duplicate(
            hypothesis_store=self.hypotheses,
            concept_store=self.concepts,
            row=row,
            memory_id=31,
        )

        self.assertEqual(self._lane(), [])

    def test_a_graduated_guess_stops_being_offered(self) -> None:
        row = self._hypothesis()

        self._graduate(row)

        self.assertEqual(self._lane(), [])

    def test_a_refuted_guess_stops_being_offered(self) -> None:
        row = self._hypothesis(status=STATUS_REFUTED)

        self.assertEqual(self._lane(), [])
        self.assertIsNotNone(self.hypotheses.get(row.hypothesis_id))

    def test_a_missing_store_leaves_the_slot_empty(self) -> None:
        from app.core.concepts.hypothesis_lane import nearest_invented

        self.assertEqual(nearest_invented(None, _NEAR_A), [])


class OrphanRepairTests(_Fixture):
    """A linked row whose concept was deleted must not be a dead slot.

    Linking is what makes a row go quiet: the ask worker filters
    ``linked=False``, the lane skips it, ``open_hypotheses`` drops it.
    There is self-healing in ``link_if_duplicate``, but it only runs on
    the *next confirmation* -- which a row nobody can ask about will never
    get. So without the repair the row sits ``live`` forever, invisible
    and holding one of twelve slots.
    """

    def _lane(self):
        from app.core.concepts.hypothesis_lane import nearest_invented

        return nearest_invented(self.hypotheses, _NEAR_A)

    def test_deleting_the_concept_releases_the_row(self) -> None:
        row = self._hypothesis(support=1)
        self.hypotheses.link(row, 4242)

        freed = self.hypotheses.unlink_concept(4242)

        self.assertEqual(freed, 1)
        self.assertIsNone(self.hypotheses.get(row.hypothesis_id).linked_concept_id)

    def test_the_released_row_is_askable_again(self) -> None:
        row = self._hypothesis(support=1)
        self.hypotheses.link(row, 4242)
        self.assertEqual(self._lane(), [])

        self.hypotheses.unlink_concept(4242)

        self.assertEqual(
            [r.hypothesis_id for r, _s in self._lane()], [row.hypothesis_id]
        )
        self.assertEqual(
            [r.hypothesis_id for r in self.hypotheses.list_by(live=True, linked=False)],
            [row.hypothesis_id],
        )

    def test_rows_linked_elsewhere_are_left_alone(self) -> None:
        keeper = self._hypothesis(support=1)
        self.hypotheses.link(keeper, 99)

        self.hypotheses.unlink_concept(4242)

        self.assertEqual(
            self.hypotheses.get(keeper.hypothesis_id).linked_concept_id, 99
        )

    def test_a_closed_row_keeps_the_trail_to_where_it_went(self) -> None:
        """``graduated_concept_id`` is history, not a live pointer.

        Blanking it when its concept is deleted would lose the only
        record that this guess became that belief, which is worse than a
        dangling id nothing reads.
        """
        row = self._hypothesis()
        result = self._graduate(row)

        self.hypotheses.unlink_concept(result.concept_id)

        self.assertEqual(
            self.hypotheses.get(row.hypothesis_id).graduated_concept_id,
            result.concept_id,
        )

    def test_the_facade_unlinks_when_a_concept_is_deleted(self) -> None:
        from app.core.session.memory_facade_mixin import MemoryFacadeMixin

        class _Host(MemoryFacadeMixin):
            pass

        host = _Host()
        host._concept_store = self.concepts
        host._hypothesis_store = self.hypotheses
        concept = self._concept(embedding=_FAR)
        row = self._hypothesis(support=1)
        self.hypotheses.link(row, concept.concept_id)

        self.assertEqual(host.delete_concept(concept.concept_id), 1)

        self.assertIsNone(
            self.hypotheses.get(row.hypothesis_id).linked_concept_id
        )

    def test_a_deleted_answer_is_not_counted_as_a_source(self) -> None:
        """Evidence must be edges to memories that exist.

        The answer ids were collected over earlier turns, so one can have
        been deleted since. Attaching it anyway would hand the new
        concept a distinct source that is not there for L3 to promote on.
        """
        row = self._hypothesis(answers=(77, 78))

        result = self._graduate(row, memory_exists=lambda mid: mid != 78)

        self.assertEqual(self._evidence_ids(result.concept_id), {"77"})
        self.assertEqual(
            self.concepts.get(result.concept_id).distinct_source_count, 1
        )

    def test_without_a_probe_every_remembered_answer_is_trusted(self) -> None:
        result = self._graduate(self._hypothesis(answers=(77, 78)))

        self.assertEqual(self._evidence_ids(result.concept_id), {"77", "78"})


class DurabilityTests(_Fixture):
    def test_a_failing_concept_store_does_not_raise(self) -> None:
        class _Broken:
            def nearest(self, *_a, **_k):
                raise RuntimeError("index gone")

            def add(self, *_a, **_k):
                raise RuntimeError("index gone")

        row = self._hypothesis()

        result = hg.graduate(
            hypothesis_store=self.hypotheses,
            concept_store=_Broken(),
            row=row,
            event_store=self.events,
        )

        self.assertIsNone(result)
        self.assertEqual(row.status, STATUS_OPEN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

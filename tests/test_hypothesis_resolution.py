"""L30c: what an answer does to the belief it was about.

The four write paths in :mod:`app.core.concepts.hypothesis_resolution`,
against a real :class:`ConceptStore` -- the edges and the recount are the
substance here, and a mock store would only assert that the module calls
itself the way it calls itself.

The load-bearing distinctions, and why each has a test:

* a confirm adds evidence but does **not** raise confidence, because L3
  owns that number and the promotion decision that follows from it;
* a deny writes the ``contradicts`` edge that makes the disconfirmation
  legible to L9 and L3 later, where a correct deliberately does not;
* the recount is a recount, so a user restating an old fact cannot
  manufacture a second distinct source for a belief that already has it.

The second half of the file runs the same four verdicts against an
*invented* hypothesis row, where the policy is deliberately harsher --
see the ``_InventedFixture`` docstring for why.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts import hypothesis_resolution as hr
from app.core.concepts.answer_adjudicator import (
    CONFIRM,
    CORRECT,
    DENY,
    UNCLEAR,
)
from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.concepts.hypothesis_store import (
    STATUS_OPEN,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    Hypothesis,
    HypothesisStore,
)
from app.core.infra.chat_database import ChatDatabase

_PENALTY = 0.25


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "test.db")
        self.store = ConceptStore(self.db)
        self.events = ConceptEventStore(self.db)

    def _concept(
        self,
        *,
        confidence: float = 0.5,
        plasticity: float = 0.5,
        evidence: tuple[int, ...] = (11,),
    ) -> Concept:
        c = Concept(
            label="Jacob treats walking as thinking time",
            kind="pattern",
            subject="user",
            status="candidate",
            confidence=confidence,
            plasticity=plasticity,
            evidence_count=len(evidence),
            distinct_source_count=len(evidence),
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        c.concept_id = self.store.add(c)
        for mem_id in evidence:
            self.store.add_edge(
                ConceptEdge(
                    src_type="memory",
                    src_id=str(mem_id),
                    dst_type="concept",
                    dst_id=str(c.concept_id),
                    relation="evidence",
                    polarity=1,
                    strength=1.0,
                )
            )
        return c

    def _apply(self, concept: Concept, verdict: str, memory_id=42):
        return hr.apply_verdict(
            store=self.store,
            concept=concept,
            verdict=verdict,
            memory_id=memory_id,
            penalty=_PENALTY,
            event_store=self.events,
            reason="test",
        )

    def _reload(self, concept: Concept) -> Concept:
        self.store.load_all()
        return self.store.get(concept.concept_id)

    def _relations(self, concept: Concept) -> list[str]:
        cid = concept.concept_id
        return [
            e.relation
            for e in (
                self.store.edges_into("concept", cid)
                + self.store.edges_from("concept", cid)
            )
        ]

    def _events(self, limit: int = 10):
        return self.events.list(limit=limit)


class ConfirmTests(_Fixture):
    def test_the_answer_becomes_evidence(self) -> None:
        concept = self._concept(evidence=(11,))
        result = self._apply(concept, CONFIRM)

        self.assertIsNotNone(result)
        self.assertTrue(result.evidence_added)
        self.assertEqual(result.distinct_sources, 2)
        reloaded = self._reload(concept)
        self.assertEqual(reloaded.evidence_count, 2)
        self.assertEqual(reloaded.distinct_source_count, 2)
        self.assertIn("evidence", self._relations(concept))

    def test_confidence_and_status_are_left_to_l3(self) -> None:
        # The whole promotion contract: a confirm supplies the *evidence*
        # for a promotion, it does not perform one. If this ever starts
        # nudging confidence, two writers own the number again.
        concept = self._concept(confidence=0.5)
        result = self._apply(concept, CONFIRM)

        self.assertEqual(result.confidence_before, result.confidence_after)
        reloaded = self._reload(concept)
        self.assertAlmostEqual(reloaded.confidence, 0.5)
        self.assertEqual(reloaded.status, "candidate")

    def test_it_stamps_the_reinforcement_time(self) -> None:
        concept = self._concept()
        self._apply(concept, CONFIRM)
        self.assertTrue(self._reload(concept).last_reinforced_at)

    def test_no_contradicts_edge(self) -> None:
        concept = self._concept()
        self._apply(concept, CONFIRM)
        self.assertNotIn("contradicts", self._relations(concept))

    def test_a_repeat_source_does_not_widen_the_grounding(self) -> None:
        # The reason the counters are recounted from ``evidence_of``
        # rather than incremented: the user restating something they
        # already told Aiko is one source twice, and a belief that can
        # inflate its own breadth would walk itself through the gate.
        concept = self._concept(evidence=(11,))
        result = self._apply(concept, CONFIRM, memory_id=11)

        self.assertEqual(result.distinct_sources, 1)
        self.assertEqual(self._reload(concept).distinct_source_count, 1)

    def test_a_confirm_with_no_stored_answer_writes_nothing(self) -> None:
        concept = self._concept(evidence=(11,))
        self.assertIsNone(self._apply(concept, CONFIRM, memory_id=None))
        self.assertEqual(self._reload(concept).distinct_source_count, 1)


class DenyTests(_Fixture):
    def test_penalty_and_contradicts_edge(self) -> None:
        concept = self._concept(confidence=0.6)
        result = self._apply(concept, DENY)

        self.assertTrue(result.contradiction_added)
        self.assertLess(result.confidence_after, 0.6)
        self.assertIn("contradicts", self._relations(concept))
        self.assertLess(self._reload(concept).confidence, 0.6)

    def test_status_survives_a_single_no(self) -> None:
        # A denial lowers conviction; whether the belief stops being
        # carried at all is L3's call on the next tick.
        concept = self._concept(confidence=0.6)
        self._apply(concept, DENY)
        self.assertEqual(self._reload(concept).status, "candidate")

    def test_it_adds_no_evidence(self) -> None:
        concept = self._concept(evidence=(11,))
        self._apply(concept, DENY)
        self.assertEqual(self._reload(concept).distinct_source_count, 1)

    def test_a_plastic_belief_gives_more_ground(self) -> None:
        soft = self._concept(confidence=0.6, plasticity=0.9)
        firm = self._concept(confidence=0.6, plasticity=0.1)
        soft_after = self._apply(soft, DENY).confidence_after
        firm_after = self._apply(firm, DENY).confidence_after
        self.assertLess(soft_after, firm_after)

    def test_the_penalty_never_goes_negative(self) -> None:
        concept = self._concept(confidence=0.05, plasticity=1.0)
        self.assertGreaterEqual(self._apply(concept, DENY).confidence_after, 0.0)


class CorrectTests(_Fixture):
    def test_penalty_without_a_contradicts_edge(self) -> None:
        # A near miss stays refinable: the edge is what pushes a belief
        # toward retirement, and "it's more that ..." has not falsified
        # anything, it has re-worded it.
        concept = self._concept(confidence=0.6)
        result = self._apply(concept, CORRECT)

        self.assertFalse(result.contradiction_added)
        self.assertLess(result.confidence_after, 0.6)
        self.assertNotIn("contradicts", self._relations(concept))

    def test_it_adds_no_evidence(self) -> None:
        concept = self._concept(evidence=(11,))
        self._apply(concept, CORRECT)
        self.assertEqual(self._reload(concept).distinct_source_count, 1)

    def test_it_costs_the_same_as_a_deny(self) -> None:
        # Same penalty, different edge. If the two ever need different
        # penalties that is a deliberate policy change, not a drift.
        denied = self._apply(self._concept(confidence=0.6), DENY)
        corrected = self._apply(self._concept(confidence=0.6), CORRECT)
        self.assertAlmostEqual(
            denied.confidence_after, corrected.confidence_after,
        )


class UnclearTests(_Fixture):
    def test_it_writes_nothing(self) -> None:
        concept = self._concept(confidence=0.5, evidence=(11,))
        self.assertIsNone(self._apply(concept, UNCLEAR))

        reloaded = self._reload(concept)
        self.assertAlmostEqual(reloaded.confidence, 0.5)
        self.assertEqual(reloaded.distinct_source_count, 1)
        self.assertEqual(self._relations(concept), ["evidence"])

    def test_it_records_no_event(self) -> None:
        concept = self._concept()
        self._apply(concept, UNCLEAR)
        self.assertEqual(self._events(), [])

    def test_an_unknown_verdict_is_also_inert(self) -> None:
        concept = self._concept()
        self.assertIsNone(self._apply(concept, "probably"))


class EventTests(_Fixture):
    def _event_types(self) -> list[str]:
        return [e.event_type for e in self._events()]

    def test_each_verdict_records_its_own_event(self) -> None:
        for verdict, expected in (
            (CONFIRM, hr.EVENT_CONFIRMED),
            (CORRECT, hr.EVENT_CORRECTED),
            (DENY, hr.EVENT_DENIED),
        ):
            with self.subTest(verdict=verdict):
                self._apply(self._concept(), verdict)
                self.assertIn(expected, self._event_types())

    def test_the_event_carries_the_post_write_state(self) -> None:
        # The drift worker reads these rows; if the event recorded the
        # pre-penalty confidence the diary would narrate a denial that
        # never happened.
        concept = self._concept(confidence=0.6)
        result = self._apply(concept, DENY)
        event = self._events(limit=1)[0]

        self.assertEqual(event.concept_id, concept.concept_id)
        self.assertAlmostEqual(event.confidence, result.confidence_after)
        self.assertEqual(event.label, concept.label)
        self.assertEqual(event.kind, concept.kind)

    def test_a_missing_event_store_is_not_fatal(self) -> None:
        concept = self._concept()
        result = hr.apply_verdict(
            store=self.store,
            concept=concept,
            verdict=DENY,
            memory_id=42,
            penalty=_PENALTY,
            event_store=None,
        )
        self.assertIsNotNone(result)


class DurabilityTests(_Fixture):
    def test_a_failing_store_is_swallowed(self) -> None:
        # This runs inside the post-turn path. A write that raises must
        # not take the turn down with it.
        class _Broken:
            def add_edge(self, edge):
                raise RuntimeError("db is gone")

            def update(self, concept):
                raise RuntimeError("db is gone")

            def evidence_of(self, cid):
                raise RuntimeError("db is gone")

        concept = self._concept()
        self.assertIsNone(
            hr.apply_verdict(
                store=_Broken(),
                concept=concept,
                verdict=CONFIRM,
                memory_id=42,
                penalty=_PENALTY,
                event_store=self.events,
            )
        )


# ── the invented target (Phase B) ─────────────────────────────────────


class _InventedFixture(unittest.TestCase):
    """Same four verdicts against a hypothesis row instead of a concept.

    The asymmetries with the concept side above are the whole point, and
    they all follow from one fact: a hypothesis has no evidence graph. A
    concept's confidence is derived and L3 recomputes it on the next
    tick, so L30c can nudge it and defer. Credence is the only number a
    hypothesis has and nothing revisits it, so an answer has to be
    conclusive here or nowhere.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "test.db")
        self.concepts = ConceptStore(self.db)
        self.hypotheses = HypothesisStore(self.db)

    def _row(self, *, credence: float = 0.5, **kw) -> Hypothesis:
        row = Hypothesis(
            statement="Jacob reads the last page first",
            kind="pattern",
            subject=kw.pop("subject", "user"),
            credence=credence,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
            **kw,
        )
        self.hypotheses.add(row)
        return row

    def _twin_concept(self) -> Concept:
        c = Concept(
            label="Jacob reads endings first",
            kind="identity",
            subject="user",
            status="active",
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        c.concept_id = self.concepts.add(c)
        return c

    def _apply(self, row: Hypothesis, verdict: str, **kw):
        return hr.apply_hypothesis_verdict(
            store=self.hypotheses,
            row=row,
            verdict=verdict,
            memory_id=kw.pop("memory_id", 42),
            credence_step=kw.pop("credence_step", 0.2),
            **kw,
        )


class InventedConfirmTests(_InventedFixture):
    def test_credence_rises_by_the_step(self) -> None:
        row = self._row(credence=0.5)

        result = self._apply(row, CONFIRM)

        self.assertAlmostEqual(result.credence_after, 0.7)
        self.assertAlmostEqual(row.credence, 0.7)

    def test_it_counts_the_support(self) -> None:
        row = self._row()

        self._apply(row, CONFIRM)

        self.assertEqual(row.support_count, 1)
        self.assertEqual(row.refute_count, 0)

    def test_the_row_stays_live_and_becomes_supported(self) -> None:
        row = self._row()

        self._apply(row, CONFIRM)

        self.assertEqual(row.status, STATUS_SUPPORTED)
        self.assertTrue(row.is_live)

    def test_the_answer_memory_is_remembered_on_the_row(self) -> None:
        """Graduation is on the second confirmation.

        By then the first answer's id is not recoverable from anywhere
        else, so the concept it graduates into would be built on half its
        evidence.
        """
        row = self._row()

        self._apply(row, CONFIRM, memory_id=31)
        self._apply(row, CONFIRM, memory_id=32)

        self.assertEqual(row.answer_memory_ids, [31, 32])

    def test_a_repeated_memory_id_is_not_counted_twice(self) -> None:
        row = self._row()

        self._apply(row, CONFIRM, memory_id=31)
        self._apply(row, CONFIRM, memory_id=31)

        self.assertEqual(row.answer_memory_ids, [31])

    def test_credence_is_capped_at_one(self) -> None:
        row = self._row(credence=0.95)

        self.assertAlmostEqual(self._apply(row, CONFIRM).credence_after, 1.0)

    def test_it_stamps_when_the_guess_was_tested(self) -> None:
        row = self._row()

        self._apply(row, CONFIRM)

        self.assertTrue(row.last_tested_at)

    def test_a_confirm_links_to_a_belief_she_already_holds(self) -> None:
        """The earliest moment the link can be stamped is right here."""
        twin = self._twin_concept()
        row = self._row()

        result = self._apply(
            row, CONFIRM, concept_store=self.concepts, memory_id=31
        )

        self.assertEqual(row.linked_concept_id, twin.concept_id)
        self.assertEqual(result.linked_concept_id, twin.concept_id)

    def test_linking_does_not_mint_a_concept(self) -> None:
        self._twin_concept()
        row = self._row()

        self._apply(row, CONFIRM, concept_store=self.concepts)

        self.assertEqual(self.concepts.count(), 1)

    def test_no_twin_leaves_the_row_unlinked(self) -> None:
        row = self._row()

        result = self._apply(row, CONFIRM, concept_store=self.concepts)

        self.assertIsNone(result.linked_concept_id)
        self.assertIsNone(row.linked_concept_id)

    def test_the_write_survives_a_reload(self) -> None:
        row = self._row()
        self._apply(row, CONFIRM)

        self.hypotheses.load_all()

        self.assertEqual(
            self.hypotheses.get(row.hypothesis_id).support_count, 1
        )


class InventedDenyTests(_InventedFixture):
    def test_one_no_closes_the_row_outright(self) -> None:
        """She made it up. Being told no is the end of it."""
        row = self._row()

        self._apply(row, DENY)

        self.assertEqual(row.status, STATUS_REFUTED)
        self.assertFalse(row.is_live)

    def test_credence_falls_by_the_step(self) -> None:
        row = self._row(credence=0.5)

        self.assertAlmostEqual(self._apply(row, DENY).credence_after, 0.3)

    def test_credence_never_goes_negative(self) -> None:
        row = self._row(credence=0.1)

        self.assertAlmostEqual(self._apply(row, DENY).credence_after, 0.0)

    def test_it_counts_the_refutation(self) -> None:
        row = self._row()

        self._apply(row, DENY)

        self.assertEqual(row.refute_count, 1)

    def test_the_refuted_row_survives_as_a_row(self) -> None:
        """Kept so the proposer cannot re-invent the same wrong guess."""
        row = self._row()

        self._apply(row, DENY)
        self.hypotheses.load_all()

        self.assertIsNotNone(self.hypotheses.get(row.hypothesis_id))

    def test_a_denied_guess_never_links(self) -> None:
        self._twin_concept()
        row = self._row()

        self._apply(row, DENY, concept_store=self.concepts)

        self.assertIsNone(row.linked_concept_id)


class InventedCorrectTests(_InventedFixture):
    def test_the_users_wording_replaces_the_guess(self) -> None:
        """The most valuable answer in the set: a better version of it."""
        row = self._row()

        self._apply(
            row,
            CORRECT,
            correction_text="It's more that I read reviews before books",
        )

        self.assertEqual(
            row.statement, "It's more that I read reviews before books"
        )

    def test_the_row_stays_open_for_another_look(self) -> None:
        row = self._row()

        self._apply(row, CORRECT, correction_text="not quite, more like this")

        self.assertTrue(row.is_live)

    def test_a_correction_costs_half_of_a_denial(self) -> None:
        """Aiko was close enough that they refined it rather than refused."""
        corrected = self._row(credence=0.5)
        denied = self._row(credence=0.5)

        self._apply(corrected, CORRECT, correction_text="more like this")
        self._apply(denied, DENY)

        self.assertAlmostEqual(corrected.credence, 0.4)
        self.assertAlmostEqual(denied.credence, 0.3)

    def test_the_restated_row_is_re_embedded(self) -> None:
        row = self._row()
        seen: list[str] = []

        def _embed(text: str):
            seen.append(text)
            return np.asarray([0.0, 1.0], dtype=np.float32)

        result = self._apply(
            row, CORRECT, correction_text="actually about endings", embed=_embed
        )

        self.assertTrue(result.restated)
        self.assertEqual(seen, ["actually about endings"])
        self.assertAlmostEqual(float(row.embedding[1]), 1.0)

    def test_a_failing_embedder_keeps_the_new_wording(self) -> None:
        def _boom(_text: str):
            raise RuntimeError("embedder down")

        row = self._row()

        self._apply(row, CORRECT, correction_text="better words", embed=_boom)

        self.assertEqual(row.statement, "better words")

    def test_an_echo_of_the_statement_is_not_a_restatement(self) -> None:
        row = self._row()

        result = self._apply(
            row, CORRECT, correction_text="Jacob reads the last page first"
        )

        self.assertFalse(result.restated)

    def test_it_does_not_count_as_support_or_refutation(self) -> None:
        row = self._row()

        self._apply(row, CORRECT, correction_text="more like this")

        self.assertEqual(row.support_count, 0)
        self.assertEqual(row.refute_count, 0)


class InventedUnclearTests(_InventedFixture):
    def test_it_writes_nothing(self) -> None:
        row = self._row(credence=0.5)

        self.assertIsNone(self._apply(row, UNCLEAR))
        self.assertAlmostEqual(row.credence, 0.5)
        self.assertEqual(row.status, STATUS_OPEN)

    def test_an_unknown_verdict_is_also_inert(self) -> None:
        row = self._row()

        self.assertIsNone(self._apply(row, "MAYBE"))


class InventedDurabilityTests(_InventedFixture):
    def test_a_failing_store_is_swallowed(self) -> None:
        class _Broken:
            def update(self, row):
                raise RuntimeError("db is gone")

            def close(self, row, **kw):
                raise RuntimeError("db is gone")

        row = self._row()

        self.assertIsNone(
            hr.apply_hypothesis_verdict(
                store=_Broken(),
                row=row,
                verdict=CONFIRM,
                memory_id=1,
                credence_step=0.2,
            )
        )

    def test_a_failing_link_does_not_lose_the_confirmation(self) -> None:
        class _BrokenConcepts:
            def nearest(self, *_a, **_k):
                raise RuntimeError("index gone")

        row = self._row()

        result = self._apply(row, CONFIRM, concept_store=_BrokenConcepts())

        self.assertEqual(result.support_count, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

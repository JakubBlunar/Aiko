"""The L30 debug surface: what the panel reads, and what its buttons do.

Two claims worth holding onto, and they are what this file is for.

The **read** is the inverse of the Aiko-facing one. ``open_hypotheses``
hides closed and linked rows on purpose -- she should not muse about a
guess that is finished, or one a concept already speaks for -- and those
are exactly the rows that explain a silent lane. If the shelf ever starts
hiding them too, the panel becomes useless for the one job it has.

The **write** goes through the live post-turn writer. A forced verdict
that reimplemented the credence math would drift from the real path and
start lying about it, so the tests assert on the consequences only a real
write produces: the link stamp, the graduation, the evidence edges.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.hypothesis_store import (
    STATUS_EXPIRED,
    STATUS_GRADUATED,
    STATUS_OPEN,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    Hypothesis,
    HypothesisStore,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.session.hypothesis_debug_mixin import HypothesisDebugMixin
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


def _unit(*xs: float) -> np.ndarray:
    v = np.asarray(xs, dtype=np.float32)
    return v / float(np.linalg.norm(v))


_NEAR_A = _unit(1.0, 0.0)
_NEAR_B = _unit(0.98, 0.2)
_FAR = _unit(0.0, 1.0)


class _FakeMemoryStore:
    """Just enough to hand back ids and answer "is it still there?".

    A real ``MemoryStore`` would drag an embedder and a Lance mirror into
    a test about the facade; what matters here is only that the answer
    memory gets an id and that graduation can probe for it.
    """

    def __init__(self) -> None:
        self.rows: dict[int, SimpleNamespace] = {}
        self._next = 900

    def add(self, *, content: str, **_kw) -> SimpleNamespace:
        self._next += 1
        row = SimpleNamespace(id=self._next, content=content)
        self.rows[self._next] = row
        return row

    def get(self, memory_id: int):
        return self.rows.get(int(memory_id))


class _Host(HypothesisDebugMixin, PostTurnHelpersMixin):
    """The two mixins the panel exercises, and nothing else."""


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "test.db")
        self.concepts = ConceptStore(self.db)
        self.hypotheses = HypothesisStore(self.db)
        self.memories = _FakeMemoryStore()

        host = _Host()
        host._hypothesis_store = self.hypotheses
        host._concept_store = self.concepts
        host._memory_store = self.memories
        host._concept_event_store = None
        host._embedder = SimpleNamespace(embed=lambda _t: _FAR)
        host._memory_settings = SimpleNamespace(
            hypothesis_credence_step=0.2,
            hypothesis_graduate_min_support=2,
            hypothesis_graduate_min_credence=0.7,
            hypothesis_max_open=12,
            hypothesis_ttl_hours=72.0,
            hypothesis_min_unsettled=0.22,
            hypothesis_min_sources=1,
        )
        host._agent_settings = SimpleNamespace(
            hypothesis_invention_enabled=True,
            concept_hypothesis_ask_enabled=True,
        )
        host.session_key = "test"
        host._notify_memory_added = lambda _m: None
        self.host = host

    def _row(
        self,
        statement: str = "Jacob tidies his desk when a deadline slips",
        *,
        status: str = STATUS_OPEN,
        credence: float = 0.5,
        support: int = 0,
        subject: str = "user",
        embedding: np.ndarray | None = None,
        answers: tuple[int, ...] = (),
    ) -> Hypothesis:
        row = Hypothesis(
            statement=statement,
            kind="pattern",
            subject=subject,
            rationale="he sorted the cables before the review slipped a week",
            credence=credence,
            support_count=support,
            status=status,
            embedding=_FAR if embedding is None else embedding,
            answer_memory_ids=list(answers),
        )
        self.hypotheses.add(row)
        return row

    def _concept(
        self,
        label: str = "Jacob tidies when a deadline slips",
        *,
        embedding: np.ndarray | None = None,
    ) -> Concept:
        c = Concept(
            label=label,
            kind="pattern",
            subject="user",
            status="active",
            confidence=0.6,
            embedding=_NEAR_B if embedding is None else embedding,
        )
        c.concept_id = self.concepts.add(c)
        return c


# ── the read ──────────────────────────────────────────────────────────


class ShelfTests(_Fixture):
    def test_it_shows_the_closed_rows_the_tool_read_hides(self) -> None:
        self._row("still guessing", status=STATUS_OPEN)
        self._row("was told no", status=STATUS_REFUTED)
        self._row("never got asked", status=STATUS_EXPIRED)

        shelf = self.host.hypothesis_shelf()
        visible = {r["statement"] for r in shelf["invented"]}

        self.assertEqual(len(visible), 3)
        self.assertIn("was told no", visible)
        self.assertIn("never got asked", visible)
        aiko_sees = {
            r["statement"]
            for r in self.host.open_hypotheses()["hypotheses"]
        }
        self.assertEqual(aiko_sees, {"still guessing"})

    def test_it_shows_linked_rows_because_they_explain_a_quiet_lane(
        self,
    ) -> None:
        row = self._row("spoken for by a concept")
        self.hypotheses.link(row, 77)

        shelf = self.host.hypothesis_shelf()

        self.assertEqual(
            [r["linked_concept_id"] for r in shelf["invented"]], [77]
        )
        self.assertEqual(self.host.open_hypotheses()["hypotheses"], [])

    def test_a_row_carries_its_whole_lifecycle(self) -> None:
        self._row(support=1, status=STATUS_SUPPORTED, answers=(901,))

        row = self.host.hypothesis_shelf()["invented"][0]

        for field in (
            "status",
            "credence",
            "support_count",
            "refute_count",
            "asked_count",
            "unsettled",
            "live",
            "origin",
            "linked_concept_id",
            "graduated_concept_id",
            "graduated_memory_id",
            "answer_memory_ids",
            "created_at",
            "last_tested_at",
            "closed_at",
        ):
            self.assertIn(field, row, field)
        self.assertEqual(row["answer_memory_ids"], [901])

    def test_the_state_block_reports_stock_against_the_caps(self) -> None:
        self._row()

        state = self.host.hypothesis_shelf()["state"]

        self.assertTrue(state["store"])
        self.assertEqual(state["live"], 1)
        self.assertEqual(state["max_open"], 12)
        self.assertEqual(state["by_status"], {STATUS_OPEN: 1})
        self.assertTrue(state["invention_enabled"])
        self.assertTrue(state["ask_enabled"])

    def test_a_status_filter_narrows_it(self) -> None:
        self._row("open one")
        self._row("closed one", status=STATUS_REFUTED)

        shelf = self.host.hypothesis_shelf(status=STATUS_REFUTED)

        self.assertEqual(
            [r["statement"] for r in shelf["invented"]], ["closed one"]
        )

    def test_a_status_filter_drops_the_grounded_half(self) -> None:
        """Candidate concepts have no status of this kind to filter on.

        Returning them anyway under a ``refuted`` filter would show rows
        that plainly contradict the filter the reader just set.
        """
        self._concept()

        self.assertEqual(
            self.host.hypothesis_shelf(status=STATUS_OPEN)["grounded"], []
        )

    def test_it_survives_a_missing_store(self) -> None:
        self.host._hypothesis_store = None

        shelf = self.host.hypothesis_shelf()

        self.assertEqual(shelf["invented"], [])
        self.assertFalse(shelf["state"]["store"])


# ── the write ─────────────────────────────────────────────────────────


class ForcedVerdictTests(_Fixture):
    def test_a_confirm_moves_credence_and_support(self) -> None:
        row = self._row(credence=0.5)

        result = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "yeah, pretty much"
        )

        self.assertEqual(result["before"]["credence"], 0.5)
        self.assertAlmostEqual(result["after"]["credence"], 0.7, places=3)
        self.assertEqual(result["after"]["support_count"], 1)
        self.assertEqual(result["after"]["status"], STATUS_SUPPORTED)

    def test_the_answer_is_stored_as_an_ordinary_memory(self) -> None:
        row = self._row()

        result = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "yeah, when things slip"
        )

        mid = result["answer_memory_id"]
        self.assertIsNotNone(mid)
        self.assertIn("yeah, when things slip", self.memories.get(mid).content)
        self.assertEqual(
            self.hypotheses.get(row.hypothesis_id).answer_memory_ids, [mid]
        )

    def test_two_confirms_graduate_into_a_concept(self) -> None:
        row = self._row(credence=0.6)

        self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "yes, that's me"
        )
        result = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "still true"
        )

        cid = result["after"]["graduated_concept_id"]
        self.assertIsNotNone(cid)
        self.assertEqual(result["after"]["status"], STATUS_GRADUATED)
        self.assertEqual(self.concepts.get(cid).label, row.statement)

    def test_the_minted_concept_rests_on_the_forced_answers(self) -> None:
        """The point of requiring text: real evidence, not a bare row.

        A graduated concept inherits exactly the answer memories, and L3
        promotes on distinct sources -- so a concept minted with none is
        one L3 demotes straight back.
        """
        row = self._row(credence=0.6)
        first = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "yes, that's me"
        )
        second = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "still true"
        )

        cid = second["after"]["graduated_concept_id"]
        sources = {
            e.src_id
            for e in self.concepts.evidence_of(cid)
            if e.src_type == "memory"
        }

        self.assertEqual(
            sources,
            {
                str(first["answer_memory_id"]),
                str(second["answer_memory_id"]),
            },
        )
        self.assertEqual(self.concepts.get(cid).distinct_source_count, 2)

    def test_a_confirm_without_text_is_refused(self) -> None:
        """Otherwise it would mint a concept resting on nothing.

        With no text there is no answer memory, so graduation would build
        a concept with zero evidence edges -- which looks like the loop
        worked and is undone by the next lifecycle tick.
        """
        row = self._row(credence=0.6)

        with self.assertRaises(ValueError):
            self.host.force_hypothesis_verdict(row.hypothesis_id, "confirm", "")

        after = self.hypotheses.get(row.hypothesis_id)
        self.assertEqual(after.status, STATUS_OPEN)
        self.assertEqual(after.support_count, 0)

    def test_a_confirm_against_a_twin_concept_links_instead_of_forking(
        self,
    ) -> None:
        existing = self._concept()
        row = self._row(embedding=_NEAR_A)

        result = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "confirm", "yeah, exactly that"
        )

        self.assertEqual(
            result["after"]["linked_concept_id"], existing.concept_id
        )

    def test_a_deny_closes_the_row_outright(self) -> None:
        row = self._row(credence=0.5)

        result = self.host.force_hypothesis_verdict(
            row.hypothesis_id, "deny", "no, not really"
        )

        self.assertEqual(result["after"]["status"], STATUS_REFUTED)
        self.assertEqual(result["after"]["refute_count"], 1)
        self.assertFalse(self.hypotheses.get(row.hypothesis_id).is_live)

    def test_a_correct_takes_the_better_wording(self) -> None:
        row = self._row(credence=0.6)

        result = self.host.force_hypothesis_verdict(
            row.hypothesis_id,
            "correct",
            "it's more that he tidies when he is stuck",
        )

        after = self.hypotheses.get(row.hypothesis_id)
        self.assertEqual(after.statement, "it's more that he tidies when he is stuck")
        self.assertNotEqual(result["before"]["statement"], after.statement)
        self.assertTrue(after.is_live)
        # Half a step, because being refined is partly a hit.
        self.assertAlmostEqual(after.credence, 0.5, places=3)

    def test_unclear_is_not_offered(self) -> None:
        """It writes nothing by design, so a button for it would be a lie."""
        row = self._row()

        with self.assertRaises(ValueError):
            self.host.force_hypothesis_verdict(
                row.hypothesis_id, "unclear", "hmm"
            )

    def test_an_unknown_verdict_is_refused(self) -> None:
        row = self._row()

        with self.assertRaises(ValueError):
            self.host.force_hypothesis_verdict(row.hypothesis_id, "maybe", "x")

    def test_a_missing_row_is_reported_as_such(self) -> None:
        with self.assertRaises(LookupError):
            self.host.force_hypothesis_verdict(9999, "deny", "no")


class DeleteTests(_Fixture):
    def test_it_removes_the_row(self) -> None:
        row = self._row()

        self.assertTrue(self.host.delete_hypothesis(row.hypothesis_id))

        self.assertIsNone(self.hypotheses.get(row.hypothesis_id))

    def test_deleting_twice_reports_nothing_went(self) -> None:
        row = self._row()
        self.host.delete_hypothesis(row.hypothesis_id)

        self.assertFalse(self.host.delete_hypothesis(row.hypothesis_id))

    def test_it_leaves_nothing_for_the_novelty_gate(self) -> None:
        """The difference from a deny, and the reason both exist.

        A refuted row survives so the proposer will not re-invent the
        guess. Deleting is for clearing out test rows, so it must not
        leave that block behind.
        """
        row = self._row(embedding=_NEAR_A)

        self.host.delete_hypothesis(row.hypothesis_id)

        self.assertEqual(self.hypotheses.nearest(_NEAR_A, k=3, live_only=False), [])

    def test_a_missing_store_is_not_an_error(self) -> None:
        self.host._hypothesis_store = None

        self.assertFalse(self.host.delete_hypothesis(1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

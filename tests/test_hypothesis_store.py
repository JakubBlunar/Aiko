"""Schema v33: persistence for things Aiko has guessed.

Mostly ordinary CRUD-plus-mirror coverage, with three things that are
actually load-bearing:

* the ``hypotheses`` table is **separate from ``concepts``**, and an
  invention must not be visible to anything reading the concept graph —
  the guarantee the whole layer rests on;
* ``credence`` is not ``confidence``, so it does not move on its own and
  has no decay;
* TTL expiry only touches rows nobody ever asked about, because a clock
  should not settle a question that has a real answer pending.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import numpy as np

from app.core.concepts.concept_store import ConceptStore
from app.core.concepts.hypothesis_store import (
    ORIGIN_CONCEPT,
    ORIGIN_FREE,
    STATUS_EXPIRED,
    STATUS_GRADUATED,
    STATUS_MERGED,
    STATUS_OPEN,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    SUBJECT_WORLD,
    Hypothesis,
    HypothesisStore,
)
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase


def _vec(*xs: float) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.store = HypothesisStore(self.db)

    def _add(
        self,
        statement: str = "Jacob might prefer mornings",
        **kw,
    ) -> Hypothesis:
        row = Hypothesis(statement=statement, **kw)
        self.store.add(row)
        return row


class RoundTripTests(_Fixture):
    def test_every_field_survives_a_reload(self) -> None:
        row = self._add(
            "Jacob probably reads the ending first",
            kind="identity",
            subject="user",
            rationale="he skims documentation the same way",
            origin=ORIGIN_CONCEPT,
            origin_refs=[3, 9],
            credence=0.35,
            embedding=_vec(0.6, 0.8),
            origin_session="s1",
        )
        row.support_count = 1
        row.asked_count = 2
        row.linked_concept_id = 44
        self.store.update(row)

        fresh = HypothesisStore(self.db)
        fresh.load_all()
        got = fresh.get(row.hypothesis_id)

        self.assertEqual(got.statement, "Jacob probably reads the ending first")
        self.assertEqual(got.rationale, "he skims documentation the same way")
        self.assertEqual(got.origin, ORIGIN_CONCEPT)
        self.assertEqual(got.origin_refs, [3, 9])
        self.assertAlmostEqual(got.credence, 0.35, places=5)
        self.assertEqual(got.support_count, 1)
        self.assertEqual(got.asked_count, 2)
        self.assertEqual(got.linked_concept_id, 44)
        self.assertEqual(got.origin_session, "s1")
        np.testing.assert_allclose(got.embedding, _vec(0.6, 0.8), rtol=1e-5)

    def test_defaults_are_the_open_free_guess(self) -> None:
        row = self._add()
        self.assertEqual(row.status, STATUS_OPEN)
        self.assertEqual(row.origin, ORIGIN_FREE)
        self.assertEqual(row.origin_refs, [])
        self.assertTrue(row.is_live)

    def test_an_unembedded_row_round_trips(self) -> None:
        row = self._add("a guess with no vector yet")
        fresh = HypothesisStore(self.db)
        fresh.load_all()
        self.assertEqual(fresh.get(row.hypothesis_id).embedding.size, 0)

    def test_an_update_needs_a_persisted_id(self) -> None:
        with self.assertRaises(ValueError):
            self.store.update(Hypothesis(statement="never saved"))

    def test_delete_removes_it_from_both_stores(self) -> None:
        row = self._add(embedding=_vec(1.0, 0.0))
        self.store.delete(row.hypothesis_id)

        self.assertIsNone(self.store.get(row.hypothesis_id))
        self.assertEqual(self.store.nearest(_vec(1.0, 0.0)), [])
        fresh = HypothesisStore(self.db)
        fresh.load_all()
        self.assertEqual(fresh.all(), [])


class IsolationTests(_Fixture):
    """The guarantee the separate table exists to provide."""

    def test_a_hypothesis_is_invisible_to_the_concept_graph(self) -> None:
        # The whole justification for a second table: an invention rests
        # on nothing, and must reach the concept layer only by being
        # written there at graduation, on purpose. If these ever shared a
        # table, every reader that trusts the graph would need a new
        # exclusion and one missed check puts a guess into the T0 profile
        # block as something Aiko believes.
        self._add("Jacob is secretly a morning person", embedding=_vec(1.0, 0.0))
        concepts = ConceptStore(self.db)
        concepts.load_all()

        self.assertEqual(concepts.all(), [])
        self.assertEqual(concepts.list_by(status="candidate"), [])
        self.assertEqual(concepts.nearest(_vec(1.0, 0.0), status=None), [])

    def test_the_world_subject_has_no_concept_equivalent(self) -> None:
        row = self._add("compilers probably cache more than they admit",
                        subject=SUBJECT_WORLD)
        self.assertTrue(row.is_world)
        self.assertEqual(
            [h.hypothesis_id
             for h in self.store.list_by(subject=SUBJECT_WORLD)],
            [row.hypothesis_id],
        )


class CredenceTests(_Fixture):
    def test_credence_does_not_move_on_its_own(self) -> None:
        # Unlike confidence, there is no decay curve and no lifecycle
        # worker: a guess nobody asked about has not become less
        # plausible, it has only gone stale.
        row = self._add(credence=0.4)
        self.store.update(row)
        fresh = HypothesisStore(self.db)
        fresh.load_all()
        self.assertAlmostEqual(fresh.get(row.hypothesis_id).credence, 0.4)


class ListingTests(_Fixture):
    def test_live_covers_open_and_supported_only(self) -> None:
        self._add("open one")
        self._add("supported one", status=STATUS_SUPPORTED)
        self._add("refuted one", status=STATUS_REFUTED)
        self._add("graduated one", status=STATUS_GRADUATED)

        live = self.store.list_by(live=True)
        self.assertEqual(
            sorted(h.statement for h in live), ["open one", "supported one"],
        )
        self.assertEqual(self.store.count_live(), 2)

    def test_filters_compose(self) -> None:
        self._add("a", subject="user", kind="identity", origin=ORIGIN_FREE)
        self._add("b", subject="aiko", kind="identity", origin=ORIGIN_FREE)
        self._add("c", subject="user", kind="value", origin=ORIGIN_CONCEPT)

        self.assertEqual(
            [h.statement for h in self.store.list_by(subject="user")],
            ["c", "a"],
        )
        self.assertEqual(
            [h.statement for h in self.store.list_by(kind="value")], ["c"],
        )
        self.assertEqual(
            [h.statement for h in self.store.list_by(origin=ORIGIN_CONCEPT)],
            ["c"],
        )

    def test_linked_is_a_tri_state_filter(self) -> None:
        linked = self._add("already believed")
        self._add("still just a guess")
        self.store.link(linked, 7)

        self.assertEqual(
            [h.statement for h in self.store.list_by(linked=True)],
            ["already believed"],
        )
        self.assertEqual(
            [h.statement for h in self.store.list_by(linked=False)],
            ["still just a guess"],
        )
        self.assertEqual(len(self.store.list_by()), 2)

    def test_counts_by_status(self) -> None:
        self._add("a")
        self._add("b")
        self._add("c", status=STATUS_MERGED)
        self.assertEqual(
            self.store.counts_by_status(), {STATUS_OPEN: 2, STATUS_MERGED: 1},
        )


class NearestTests(_Fixture):
    def test_it_ranks_by_cosine(self) -> None:
        near = self._add("near", embedding=_vec(1.0, 0.0))
        far = self._add("far", embedding=_vec(0.0, 1.0))

        ranked = self.store.nearest(_vec(1.0, 0.05))
        self.assertEqual(
            [h.hypothesis_id for h, _s in ranked],
            [near.hypothesis_id, far.hypothesis_id],
        )
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_refuted_rows_stay_visible_to_the_novelty_check(self) -> None:
        # Re-inventing a guess the user already rejected is exactly the
        # repetition worth catching, so the default search is not
        # live-only.
        self._add("already said no", status=STATUS_REFUTED,
                  embedding=_vec(1.0, 0.0))
        self.assertEqual(len(self.store.nearest(_vec(1.0, 0.0))), 1)
        self.assertEqual(
            self.store.nearest(_vec(1.0, 0.0), live_only=True), [],
        )

    def test_unembedded_rows_and_degenerate_queries_are_skipped(self) -> None:
        self._add("no vector")
        self.assertEqual(self.store.nearest(_vec(1.0, 0.0)), [])

        self._add("has one", embedding=_vec(1.0, 0.0))
        self.assertEqual(self.store.nearest(_vec(0.0, 0.0)), [])
        self.assertEqual(self.store.nearest(_vec(1.0, 0.0), k=0), [])

    def test_a_dimension_mismatch_does_not_crash_the_matmul(self) -> None:
        self._add("two dims", embedding=_vec(1.0, 0.0))
        self._add("three dims", embedding=_vec(1.0, 0.0, 0.0))
        ranked = self.store.nearest(_vec(1.0, 0.0))
        self.assertEqual([h.statement for h, _s in ranked], ["two dims"])

    def test_k_caps_the_result(self) -> None:
        for i in range(5):
            self._add(f"guess {i}", embedding=_vec(1.0, float(i) / 10.0))
        self.assertEqual(len(self.store.nearest(_vec(1.0, 0.0), k=2)), 2)


class ClosingTests(_Fixture):
    def test_close_stamps_the_exit(self) -> None:
        row = self._add()
        self.store.close(row, status=STATUS_GRADUATED, concept_id=12)

        self.assertEqual(row.status, STATUS_GRADUATED)
        self.assertEqual(row.graduated_concept_id, 12)
        self.assertTrue(row.closed_at)
        self.assertFalse(row.is_live)

    def test_merged_is_distinct_from_graduated(self) -> None:
        # "My guess was already true" and "my guess became a new belief"
        # are different stories, and the diary should narrate them
        # differently.
        merged = self._add("a")
        graduated = self._add("b")
        self.store.close(merged, status=STATUS_MERGED, concept_id=1)
        self.store.close(graduated, status=STATUS_GRADUATED, concept_id=2)

        self.assertNotEqual(merged.status, graduated.status)

    def test_a_world_row_exits_to_a_memory(self) -> None:
        row = self._add(subject=SUBJECT_WORLD)
        self.store.close(row, status=STATUS_GRADUATED, memory_id=99)
        self.assertEqual(row.graduated_memory_id, 99)
        self.assertIsNone(row.graduated_concept_id)


class ExpiryTests(_Fixture):
    def _age(self, row: Hypothesis, hours: float) -> None:
        stamp = timephrase.utcnow() - timedelta(hours=hours)
        row.created_at = stamp.isoformat()
        self.store.update(row)

    def test_an_untested_guess_ages_out(self) -> None:
        row = self._add()
        self._age(row, 400.0)
        self.assertEqual(self.store.expire_stale(ttl_hours=336.0), 1)
        self.assertEqual(self.store.get(row.hypothesis_id).status,
                         STATUS_EXPIRED)

    def test_a_fresh_guess_survives(self) -> None:
        row = self._add()
        self.assertEqual(self.store.expire_stale(ttl_hours=336.0), 0)
        self.assertEqual(self.store.get(row.hypothesis_id).status, STATUS_OPEN)

    def test_an_answered_guess_is_never_expired_by_the_clock(self) -> None:
        # The answer came back and moved it, so the row is settled or being
        # settled; a TTL should not overrule that.
        row = self._add(asked_count=1)
        row.last_tested_at = timephrase.utcnow().isoformat()
        self.store.update(row)
        self._age(row, 5000.0)
        self.assertEqual(self.store.expire_stale(ttl_hours=336.0), 0)
        self.assertEqual(self.store.get(row.hypothesis_id).status, STATUS_OPEN)

    def test_asked_but_never_answered_still_ages_out(self) -> None:
        """The wedge that shut the live lane down.

        Asking used to grant permanent immunity, but a row asked once and
        never answered cannot be asked again either (one ask per
        invention), so it held a ``hypothesis_max_open`` slot forever.
        Twelve such rows filled the shelf and invention stopped.
        """
        row = self._add(asked_count=1)
        self._age(row, 5000.0)
        self.assertEqual(self.store.expire_stale(ttl_hours=336.0), 1)
        self.assertEqual(
            self.store.get(row.hypothesis_id).status, STATUS_EXPIRED,
        )

    def test_a_closed_row_is_left_alone(self) -> None:
        row = self._add(status=STATUS_REFUTED)
        self._age(row, 5000.0)
        self.assertEqual(self.store.expire_stale(ttl_hours=336.0), 0)
        self.assertEqual(row.status, STATUS_REFUTED)

    def test_a_zero_ttl_disables_expiry(self) -> None:
        row = self._add()
        self._age(row, 99999.0)
        self.assertEqual(self.store.expire_stale(ttl_hours=0.0), 0)

    def test_an_unparseable_timestamp_is_not_expired(self) -> None:
        row = self._add()
        row.created_at = "not a date"
        self.store.update(row)
        self.assertEqual(self.store.expire_stale(ttl_hours=1.0), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

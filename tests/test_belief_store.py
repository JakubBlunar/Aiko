"""Tests for :mod:`app.core.relationship.belief_store` and the schema v12 migration."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta

from app.core.infra import timephrase
from pathlib import Path

import numpy as np

from app.core.relationship.belief_store import (
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
    SOURCE_SELF_TAG,
    STATUS_ACTIVE,
    STATUS_CONFIRMED,
    STATUS_CONTRADICTED,
    STATUS_STALE,
)
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION


def _build_db() -> tuple[ChatDatabase, BeliefStore, Path]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, BeliefStore(db), path


class SchemaMigrationTests(unittest.TestCase):
    def test_fresh_database_lands_on_current_schema(self) -> None:
        _, _, path = _build_db()
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], _SCHEMA_VERSION)
        self.assertGreaterEqual(_SCHEMA_VERSION, 12)
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(beliefs)"
            ).fetchall()
        }
        expected = {
            "id", "user_id", "kind", "topic", "topic_embedding",
            "predicted_state", "confidence", "valence", "arousal",
            "source", "source_message_id", "observed_at",
            "last_checked_at", "status", "gap_seen_at", "metadata",
        }
        self.assertTrue(expected.issubset(cols), f"missing cols: {expected - cols}")
        idx = {
            r[1]
            for r in conn.execute(
                "PRAGMA index_list(beliefs)"
            ).fetchall()
        }
        self.assertIn("idx_beliefs_status", idx)
        self.assertIn("idx_beliefs_topic", idx)
        conn.close()

    def test_v11_database_upgrades_to_current_schema(self) -> None:
        """A pre-v12 database (no beliefs table) gets migrated cleanly."""
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "legacy.db"
        # Build a minimal v11-shaped database by hand.
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (11)")
        conn.commit()
        conn.close()

        # Opening through ChatDatabase triggers migration to the current
        # schema version (v12 added beliefs; later versions are no-ops
        # for the columns this test exercises).
        db = ChatDatabase(path)
        del db

        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], _SCHEMA_VERSION)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("beliefs", tables)
        conn.close()


class CRUDTests(unittest.TestCase):
    def test_upsert_inserts_new_row(self) -> None:
        _, store, _ = _build_db()
        belief = store.upsert(
            user_id="u1",
            kind=KIND_MOOD,
            topic="Tokyo Trip",
            predicted_state="excited",
            confidence=0.8,
            valence=0.5,
            arousal=0.7,
            source=SOURCE_SELF_TAG,
        )
        self.assertIsNotNone(belief)
        assert belief is not None
        self.assertEqual(belief.kind, KIND_MOOD)
        # Topic normalised to lowercase/trim.
        self.assertEqual(belief.topic, "tokyo trip")
        self.assertEqual(belief.predicted_state, "excited")
        self.assertEqual(belief.confidence, 0.8)
        self.assertEqual(belief.valence, 0.5)
        self.assertEqual(belief.arousal, 0.7)
        self.assertEqual(belief.source, SOURCE_SELF_TAG)
        self.assertEqual(belief.status, STATUS_ACTIVE)

    def test_upsert_updates_existing_same_topic(self) -> None:
        _, store, _ = _build_db()
        first = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.6,
        )
        second = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="Tokyo Trip",
            predicted_state="nervous", confidence=0.9,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.predicted_state, "nervous")
        self.assertEqual(second.confidence, 0.9)

    def test_upsert_rejects_empty_topic_or_state(self) -> None:
        _, store, _ = _build_db()
        self.assertIsNone(
            store.upsert(
                user_id="u1", kind=KIND_MOOD, topic="",
                predicted_state="excited",
            )
        )
        self.assertIsNone(
            store.upsert(
                user_id="u1", kind=KIND_MOOD, topic="tokyo",
                predicted_state="",
            )
        )

    def test_upsert_rejects_invalid_kind(self) -> None:
        _, store, _ = _build_db()
        self.assertIsNone(
            store.upsert(
                user_id="u1", kind="bogus", topic="x",
                predicted_state="y",
            )
        )

    def test_topic_embedding_dedupe(self) -> None:
        _, store, _ = _build_db()
        v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v1 /= np.linalg.norm(v1)
        first = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust language",
            predicted_state="overhyped", topic_embedding=v1,
        )
        # Close vector (~0.99 cosine) -- should fuzzy-merge.
        v2 = np.array([0.99, 0.10, 0.05, 0.0], dtype=np.float32)
        v2 /= np.linalg.norm(v2)
        second = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="the rust lang",
            predicted_state="solid", topic_embedding=v2,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        # And the second upsert's topic + state wins.
        self.assertEqual(second.topic, "the rust lang")
        self.assertEqual(second.predicted_state, "solid")
        # A distant vector (cosine ~0.0) should NOT merge.
        v3 = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        third = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="ruby framework",
            predicted_state="ok", topic_embedding=v3,
        )
        self.assertIsNotNone(third)
        assert third is not None
        self.assertNotEqual(third.id, first.id)

    def test_mark_status_helpers(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="y",
        )
        assert b is not None
        self.assertTrue(store.mark_contradicted(b.id))
        self.assertEqual(store.get(b.id).status, STATUS_CONTRADICTED)
        self.assertIsNotNone(store.get(b.id).gap_seen_at)
        b2 = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="z",
            predicted_state="y",
        )
        assert b2 is not None
        self.assertTrue(store.mark_confirmed(b2.id))
        self.assertEqual(store.get(b2.id).status, STATUS_CONFIRMED)
        self.assertTrue(store.mark_stale(b2.id))
        self.assertEqual(store.get(b2.id).status, STATUS_STALE)

    def test_update_partial(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="initial", confidence=0.5,
        )
        assert b is not None
        b2 = store.update(b.id, predicted_state="updated", confidence=0.9)
        self.assertIsNotNone(b2)
        assert b2 is not None
        self.assertEqual(b2.predicted_state, "updated")
        self.assertEqual(b2.confidence, 0.9)

    def test_update_rejects_invalid_status(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="y",
        )
        assert b is not None
        with self.assertRaises(ValueError):
            store.update(b.id, status="bogus")

    def test_delete(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="y",
        )
        assert b is not None
        self.assertTrue(store.delete(b.id))
        self.assertIsNone(store.get(b.id))


class ListAndCountTests(unittest.TestCase):
    def test_list_active_filters_status(self) -> None:
        _, store, _ = _build_db()
        a = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="a",
            predicted_state="x",
        )
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="b",
            predicted_state="x",
        )
        assert a is not None and b is not None
        store.mark_contradicted(b.id)
        active = store.list_active(user_id="u1")
        self.assertEqual({row.id for row in active}, {a.id})

    def test_list_active_for_gap_check_requires_valence(self) -> None:
        _, store, _ = _build_db()
        # Mood belief without numeric valence: excluded.
        store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="t1",
            predicted_state="happy",
        )
        # Mood belief with valence: included.
        store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="t2",
            predicted_state="happy", valence=0.4,
        )
        rows = store.list_active_for_gap_check(user_id="u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].topic, "t2")

    def test_count_by_status(self) -> None:
        _, store, _ = _build_db()
        a = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="a",
            predicted_state="x",
        )
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="b",
            predicted_state="x",
        )
        assert a is not None and b is not None
        store.mark_contradicted(b.id)
        counts = store.count_by_status(user_id="u1")
        self.assertEqual(counts[STATUS_ACTIVE], 1)
        self.assertEqual(counts[STATUS_CONTRADICTED], 1)
        self.assertEqual(counts[STATUS_CONFIRMED], 0)
        self.assertEqual(counts[STATUS_STALE], 0)


class ReobservationTests(unittest.TestCase):
    """The status a re-observed belief lands in.

    Re-observation used to force ``active`` unconditionally, so the
    worker re-deriving a topic quietly undid the user's "mark correct"
    and the same rows kept reappearing in the review tab.
    """

    def _reobserve(self, store, **kwargs):
        return store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust",
            predicted_state="overhyped", **kwargs,
        )

    def test_confirmed_survives_reobservation(self) -> None:
        _, store, _ = _build_db()
        b = self._reobserve(store)
        assert b is not None
        store.mark_confirmed(b.id)
        self._reobserve(store)
        self.assertEqual(store.get(b.id).status, STATUS_CONFIRMED)

    def test_replaying_an_extraction_is_not_confirmation(self) -> None:
        _, store, _ = _build_db()
        b = self._reobserve(store)
        assert b is not None
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)
        self.assertEqual(store.get(b.id).metadata.get("observations"), 1)
        self._reobserve(store)
        row = store.get(b.id)
        self.assertEqual(row.status, STATUS_ACTIVE)
        self.assertEqual(row.metadata.get("observations"), 1)

    def test_old_threshold_does_not_turn_repetition_into_verification(self) -> None:
        _, store, _ = _build_db()
        b = self._reobserve(store, auto_confirm_after=3)
        assert b is not None
        self._reobserve(store, auto_confirm_after=3)
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)
        self._reobserve(store, auto_confirm_after=3)
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)

    def test_changed_state_restarts_the_count(self) -> None:
        """A new claim about the same topic inherits no corroboration."""
        _, store, _ = _build_db()
        b = self._reobserve(store)
        assert b is not None
        store.update(b.id, status="confirmed")
        self.assertEqual(store.get(b.id).status, STATUS_CONFIRMED)
        store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust",
            predicted_state="actually pretty good",
        )
        row = store.get(b.id)
        self.assertEqual(row.status, STATUS_ACTIVE)
        self.assertEqual(row.metadata.get("observations"), 1)

    def test_contradicted_row_never_auto_confirms(self) -> None:
        _, store, _ = _build_db()
        b = self._reobserve(store)
        assert b is not None
        store.mark_contradicted(b.id)
        for _ in range(4):
            self._reobserve(store)
        self.assertEqual(store.get(b.id).status, STATUS_CONTRADICTED)

    def test_fresh_source_ids_count_as_support_without_certifying_the_reading(self):
        _, store, _ = _build_db()
        b = self._reobserve(store, source_message_id=10)
        self._reobserve(store, source_message_id=10)
        self.assertEqual(store.get(b.id).metadata["observations"], 1)
        self._reobserve(store, source_message_id=11)
        row = store.get(b.id)
        self.assertEqual(row.metadata["observations"], 2)
        self.assertEqual(row.status, STATUS_ACTIVE)
        store.mark_contradicted(b.id)
        self._reobserve(store, source_message_id=12)
        self.assertEqual(store.get(b.id).status, STATUS_ACTIVE)


class BelievedAndTrustedTests(unittest.TestCase):
    def test_confirming_does_not_exempt_from_gap_checks(self) -> None:
        """The bug this split exists for.

        ``confirmed`` used to drop a row out of the only pool anything
        read, so marking a belief correct made it permanently
        uncontradictable -- behaviourally the same as deleting it.
        """
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", valence=0.6,
        )
        assert b is not None
        store.mark_confirmed(b.id)
        rows = store.list_active_for_gap_check(user_id="u1")
        self.assertEqual([r.id for r in rows], [b.id])

    def test_list_believed_spans_active_and_confirmed_only(self) -> None:
        _, store, _ = _build_db()
        active = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="a", predicted_state="x",
        )
        confirmed = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="b", predicted_state="x",
        )
        gone = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="c", predicted_state="x",
        )
        stale = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="d", predicted_state="x",
        )
        assert active and confirmed and gone and stale
        store.mark_confirmed(confirmed.id)
        store.mark_contradicted(gone.id)
        store.mark_stale(stale.id)
        ids = {r.id for r in store.list_believed(user_id="u1")}
        self.assertEqual(ids, {active.id, confirmed.id})

    def test_list_trusted_is_confirmed_only(self) -> None:
        """An uncorroborated guess must not be spoken as if it were known."""
        _, store, _ = _build_db()
        active = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="a", predicted_state="x",
        )
        confirmed = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="b", predicted_state="x",
        )
        assert active and confirmed
        store.mark_confirmed(confirmed.id)
        ids = [r.id for r in store.list_trusted(user_id="u1")]
        self.assertEqual(ids, [confirmed.id])


class OutcomeTests(unittest.TestCase):
    def test_only_manual_confirmation_creates_a_resolution_from_repeated_extraction(self) -> None:
        _, store, _ = _build_db()
        claim = dict(user_id="u1", kind=KIND_OPINION, topic="testing", predicted_state="useful")
        belief = store.upsert(**claim)
        store.upsert(**claim)
        store.update(belief.id, status="confirmed")
        store.update(belief.id, status="confirmed")
        self.assertEqual(
            [row["method"] for row in store.list_outcomes(user_id="u1")],
            ["manual"],
        )

    def test_upgrade_does_not_invent_historic_outcomes(self) -> None:
        db, store, path = _build_db()
        belief = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="testing", predicted_state="useful",
        )
        db.execute_commit("UPDATE beliefs SET status = 'confirmed' WHERE id = ?", (belief.id,))
        db.execute_commit("DROP TABLE belief_outcomes")
        db.execute_commit("UPDATE schema_version SET version = 44")
        reopened = BeliefStore(ChatDatabase(path))
        self.assertEqual(reopened.get(belief.id).status, "confirmed")
        self.assertEqual(reopened.list_outcomes(user_id="u1"), [])

    def test_manual_edit_resolution_snapshots_the_edited_claim(self) -> None:
        _, store, _ = _build_db()
        belief = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="testing", predicted_state="useful",
        )
        store.update(belief.id, status="CONFIRMED")
        store.update(belief.id, predicted_state="very useful", status="confirmed")
        rows = store.list_outcomes(user_id="u1")
        self.assertEqual([row["claim"]["predicted_state"] for row in rows],
                         ["useful", "very useful"])

    def test_outcomes_preserve_claim_and_separate_methods(self) -> None:
        _, store, _ = _build_db()
        claim = dict(user_id="u1", kind=KIND_OPINION, topic="testing", predicted_state="useful")
        belief = store.upsert(**claim)
        store.update(belief.id, status="confirmed")
        store.mark_contradicted(
            belief.id, method="opinion_heuristic", evidence_message_id=42,
            evidence={"signals": ["negation"]},
        )
        store.mark_contradicted(
            belief.id, method="opinion_heuristic", evidence_message_id=42,
        )
        store.upsert(**{**claim, "predicted_state": "sometimes useful"})
        rows = store.list_outcomes(user_id="u1")
        self.assertEqual([row["method"] for row in rows], ["manual", "opinion_heuristic"])
        self.assertEqual(rows[1]["claim"]["predicted_state"], "useful")
        self.assertEqual(rows[1]["evidence_message_id"], 42)
        self.assertEqual(rows[1]["evidence"], {"signals": ["negation"]})
        self.assertEqual(store.list_outcomes(user_id="other"), [])
        self.assertEqual(store.list_outcomes(user_id="u1", after_id=rows[0]["id"]), rows[1:])

    def test_manual_resolution_and_revival_are_separate_events(self) -> None:
        _, store, _ = _build_db()
        belief = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="testing", predicted_state="useful",
        )
        store.update(belief.id, status="contradicted")
        store.update(belief.id, status="contradicted")
        store.update(belief.id, status="active")
        store.update(belief.id, status="contradicted")
        rows = store.list_outcomes(user_id="u1")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["method"] == "manual" for row in rows))
        store.delete(belief.id)
        self.assertEqual(store.list_outcomes(user_id="u1"), [])

    def test_failed_ledger_write_rolls_back_status(self) -> None:
        db, store, _ = _build_db()
        belief = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="testing", predicted_state="useful",
        )
        db.execute_commit("DROP TABLE belief_outcomes")
        with self.assertRaises(sqlite3.OperationalError):
            store.mark_confirmed(belief.id)
        self.assertEqual(store.get(belief.id).status, "active")


class MaintenanceTests(unittest.TestCase):
    def test_mark_stale_older_than(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="ancient",
            predicted_state="x",
            observed_at="1990-01-01T00:00:00+00:00",
        )
        assert b is not None
        # cutoff = 2050 -> the ancient row qualifies
        n = store.mark_stale_older_than(
            cutoff_iso="2050-01-01T00:00:00+00:00", user_id="u1",
        )
        self.assertEqual(n, 1)
        self.assertEqual(store.get(b.id).status, STATUS_STALE)

    def test_active_cap_retires_oldest_without_deleting_receipts(self) -> None:
        _, store, _ = _build_db()
        keep_high = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="keep",
            predicted_state="x", confidence=0.9,
            observed_at="2024-06-01T00:00:00+00:00",
        )
        drop_low = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="drop1",
            predicted_state="x", confidence=0.1,
            observed_at="2024-01-01T00:00:00+00:00",
        )
        drop_mid = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="drop2",
            predicted_state="x", confidence=0.4,
            observed_at="2024-02-01T00:00:00+00:00",
        )
        assert keep_high is not None and drop_low is not None and drop_mid is not None
        pruned = store.prune_to_cap(user_id="u1", cap=1)
        self.assertEqual(pruned, 2)
        self.assertEqual(store.get(drop_low.id).status, STATUS_STALE)
        self.assertEqual(store.get(drop_mid.id).status, STATUS_STALE)
        self.assertEqual(store.get(keep_high.id).status, STATUS_ACTIVE)


if __name__ == "__main__":
    unittest.main()


class InterpretationLifecycleTests(unittest.TestCase):
    def _write(self, store, **overrides):
        now = timephrase.utcnow()
        args = dict(user_id="u1", kind="mood", topic="quiet company",
                    predicted_state="They may want quiet companionship while tired.",
                    source="worker", source_message_id=1, metadata={
                        "interpretation_version": 2, "scope": "exchange",
                        "session_key": "u1:s", "situation": "Winding down.",
                        "uncertainty": "Amount of conversation unknown.",
                        "expires_at": (now + timedelta(hours=2)).isoformat(),
                        "evidence": [{"message_id": 1, "quote": "Stay with me."}],
                    })
        args.update(overrides)
        return store.upsert(**args)

    def test_revising_a_reading_retains_a_bounded_original_receipt(self):
        _, store, _ = _build_db()
        first = self._write(store)
        store.update(first.id, status="confirmed")
        changed = self._write(
            store, predicted_state="They may enjoy an active conversation instead.",
            source_message_id=2,
        )
        self.assertEqual(changed.id, first.id)
        self.assertEqual(changed.status, "active")
        self.assertNotIn("confirmation_basis", changed.metadata)
        self.assertEqual(changed.metadata["interpretation_history"][0]["interpretation"],
                         first.predicted_state)
        for i in range(6):
            self._write(store, predicted_state=f"Another possible interpretation, number {i}.")
        self.assertEqual(len(store.get(first.id).metadata["interpretation_history"]), 3)

    def test_topic_similarity_alone_cannot_erase_different_meanings(self):
        _, store, _ = _build_db()
        vector = np.array([1.0, 0.0])
        first = self._write(store, topic="welcoming affection", topic_embedding=vector)
        second = self._write(store, topic="feeling secure", topic_embedding=vector,
                             predicted_state="They may feel secure in Aiko's company.")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(store.list_active(user_id="u1")), 2)

    def test_manual_scope_correction_preserves_the_previous_reading(self):
        _, store, _ = _build_db()
        first = self._write(store)
        store.update(first.id, status="confirmed")
        corrected = store.update(first.id, situation="Only while winding down before sleep.")
        self.assertEqual(corrected.status, "active")
        self.assertTrue(corrected.metadata["manually_edited"])
        self.assertNotIn("confirmation_basis", corrected.metadata)
        self.assertEqual(corrected.metadata["interpretation_history"][-1]["metadata"]["situation"],
                         "Winding down.")
        self.assertIsNone(self._write(store))  # worker cannot undo the user's current correction

    def test_confirmed_moods_expire_despite_unrelated_inspections(self):
        _, store, _ = _build_db()
        old = (timephrase.utcnow() - timedelta(days=3)).isoformat()
        row = store.upsert(user_id="u1", kind="mood", topic="trip", predicted_state="Excited",
                           observed_at=old)
        store.update(row.id, status="confirmed")
        store.stamp_checked(row.id)
        cutoff = (timephrase.utcnow() - timedelta(days=90)).isoformat()
        self.assertEqual(store.mark_stale_older_than(cutoff_iso=cutoff), 1)
        self.assertEqual(store.get(row.id).status, "stale")

    def test_legacy_confidence_cannot_crowd_out_a_new_tentative_reading(self):
        _, store, _ = _build_db()
        new = self._write(store, confidence=0.3)
        legacy = store.upsert(user_id="u1", kind="mood", topic="old warmth",
                              predicted_state="Deeply affectionate", confidence=0.99)
        self.assertEqual(store.prune_to_cap(user_id="u1", cap=1), 1)
        self.assertEqual(store.get(new.id).status, STATUS_ACTIVE)
        self.assertEqual(store.get(legacy.id).status, STATUS_STALE)

    def test_legacy_filter_applies_before_pagination_and_to_counts(self):
        _, store, _ = _build_db()
        row = self._write(store)
        for i in range(4):
            store.upsert(user_id="u1", kind="mood", topic=f"legacy {i}", predicted_state="Warm")
        scoped = store.list_recent(user_id="u1", interpretations_only=True, limit=1)
        self.assertEqual([b.id for b in scoped], [row.id])
        counts = store.count_by_status(user_id="u1", interpretations_only=True)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(store.count_by_status(user_id="u1")["active"], 5)

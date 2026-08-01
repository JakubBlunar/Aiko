"""CueStore: the cue pool's state machine, supersession, and TTL."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.proactive.cue_store import (
    STATE_AWAITING,
    STATE_EXPIRED,
    STATE_PENDING,
    STATE_SUPERSEDED,
    STATE_SURFACED,
    STATE_USED,
    CueStore,
    normalise_subject,
)


class _Fixture:
    def __init__(self) -> None:
        # Windows keeps the sqlite file handle alive past the connection,
        # so cleanup is best-effort as it is everywhere else in the suite.
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = CueStore(self.db, user_id="default")

    def close(self) -> None:
        self.tmp.cleanup()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)
        self.store = self.fx.store


# ── 1. writes and the basic state walk ────────────────────────────────


class AddTests(_Base):
    def test_add_returns_id_and_row_reads_back(self) -> None:
        cue_id = self.store.add(
            "dormant_interest",
            "Film Photography",
            "we haven't talked about film photography in ages",
            payload={"days_quiet": 40},
        )
        self.assertGreater(cue_id, 0)
        row = self.store.get(cue_id)
        assert row is not None
        self.assertEqual(row.cue_type, "dormant_interest")
        self.assertEqual(row.state, STATE_PENDING)
        self.assertEqual(row.payload, {"days_quiet": 40})
        self.assertEqual(row.surfaced_count, 0)

    def test_subject_is_normalised(self) -> None:
        cue_id = self.store.add("interest_drift", "  Film   Photography ", "x")
        row = self.store.get(cue_id)
        assert row is not None
        self.assertEqual(row.subject, "film photography")
        self.assertEqual(normalise_subject("Film   Photography"), "film photography")

    def test_incomplete_input_is_refused_rather_than_stored(self) -> None:
        self.assertEqual(self.store.add("", "topic", "text"), 0)
        self.assertEqual(self.store.add("kind", "", "text"), 0)
        self.assertEqual(self.store.add("kind", "topic", "  "), 0)
        self.assertEqual(self.store.count_for_user(), 0)

    def test_embedding_round_trips_only_when_asked_for(self) -> None:
        vec = [0.5, -0.25, 0.125, 0.0]
        cue_id = self.store.add("curiosity_seed", "bread", "x", embedding=vec)
        plain = self.store.get(cue_id)
        assert plain is not None
        self.assertIsNone(plain.embedding)
        loaded = self.store.get(cue_id, with_embedding=True)
        assert loaded is not None and loaded.embedding is not None
        for got, want in zip(loaded.embedding, vec, strict=True):
            self.assertAlmostEqual(got, want, places=5)

    def test_embedding_is_absent_from_the_serialised_shape(self) -> None:
        cue_id = self.store.add("curiosity_seed", "bread", "x", embedding=[1.0])
        row = self.store.get(cue_id, with_embedding=True)
        assert row is not None
        self.assertNotIn("embedding", row.as_dict())


class SupersessionTests(_Base):
    def test_same_subject_retires_the_older_cue(self) -> None:
        old = self.store.add("dormant_interest", "bread", "older line")
        new = self.store.add("dormant_interest", "bread", "newer line")
        self.assertEqual(self.store.get(old).state, STATE_SUPERSEDED)
        self.assertEqual(self.store.get(new).state, STATE_PENDING)
        self.assertEqual(self.store.count_pending("dormant_interest"), 1)

    def test_supersession_crosses_cue_types(self) -> None:
        """Two cues about one subject are one conversational move.

        Whichever worker noticed it, letting both queue would have Aiko
        raise the same subject twice from two angles.
        """
        old = self.store.add("dormant_interest", "bread", "older line")
        self.store.add("interest_drift", "bread", "newer line")
        self.assertEqual(self.store.get(old).state, STATE_SUPERSEDED)

    def test_terminal_rows_are_not_resurrected_or_re_retired(self) -> None:
        used = self.store.add("dormant_interest", "bread", "line")
        self.store.mark_used(used, evidence="lexical:2")
        self.store.add("dormant_interest", "bread", "another line")
        row = self.store.get(used)
        assert row is not None
        self.assertEqual(row.state, STATE_USED)
        self.assertEqual(row.used_evidence, "lexical:2")

    def test_a_different_subject_is_left_alone(self) -> None:
        other = self.store.add("dormant_interest", "sourdough", "line")
        self.store.add("dormant_interest", "bread", "line")
        self.assertEqual(self.store.get(other).state, STATE_PENDING)


class StateWalkTests(_Base):
    def test_surfaced_then_used(self) -> None:
        cue_id = self.store.add("interest_drift", "bread", "line")
        self.assertTrue(self.store.mark_surfaced(cue_id))
        row = self.store.get(cue_id)
        self.assertEqual(row.state, STATE_SURFACED)
        self.assertEqual(row.surfaced_count, 1)
        self.assertIsNotNone(row.last_surfaced_at)

        self.assertTrue(self.store.mark_used(cue_id, evidence="cosine:0.61"))
        row = self.store.get(cue_id)
        self.assertEqual(row.state, STATE_USED)
        self.assertEqual(row.used_evidence, "cosine:0.61")
        self.assertIsNotNone(row.used_at)

    def test_surfaced_then_asked_then_used(self) -> None:
        cue_id = self.store.add("forward_curiosity", "the move", "line")
        self.store.mark_surfaced(cue_id)
        self.assertTrue(self.store.mark_asked(cue_id))
        row = self.store.get(cue_id)
        self.assertEqual(row.state, STATE_AWAITING)
        self.assertEqual(row.ask_count, 1)
        self.assertIsNotNone(row.last_asked_at)
        self.store.mark_used(cue_id)
        self.assertEqual(self.store.get(cue_id).state, STATE_USED)

    def test_counters_are_independent(self) -> None:
        """The two failure modes are counted separately on purpose."""
        cue_id = self.store.add("forward_curiosity", "the move", "line")
        self.store.mark_surfaced(cue_id)
        self.store.release(cue_id)
        self.store.mark_surfaced(cue_id)
        self.store.mark_asked(cue_id)
        row = self.store.get(cue_id)
        self.assertEqual(row.surfaced_count, 2)
        self.assertEqual(row.ask_count, 1)

    def test_release_returns_the_cue_to_the_pool(self) -> None:
        cue_id = self.store.add("interest_drift", "bread", "line")
        self.store.mark_surfaced(cue_id)
        self.assertEqual(self.store.count_pending("interest_drift"), 0)
        self.store.release(cue_id)
        self.assertEqual(self.store.count_pending("interest_drift"), 1)

    def test_release_behind_a_cooldown_withholds_the_cue(self) -> None:
        now = timephrase.utcnow()
        cue_id = self.store.add("forward_curiosity", "the move", "line")
        self.store.mark_asked(cue_id)
        self.store.release(cue_id, not_before=now + timedelta(hours=6))
        self.assertEqual(self.store.count_pending("forward_curiosity"), 0)
        self.assertEqual(
            self.store.count_pending(
                "forward_curiosity", now=now + timedelta(hours=7),
            ),
            1,
        )

    def test_expire_is_terminal(self) -> None:
        cue_id = self.store.add("interest_drift", "bread", "line")
        self.store.expire(cue_id, evidence="max_surfacings")
        row = self.store.get(cue_id)
        self.assertEqual(row.state, STATE_EXPIRED)
        self.assertEqual(self.store.count_pending(), 0)

    def test_updates_on_a_missing_row_report_failure(self) -> None:
        self.assertFalse(self.store.mark_used(9999))
        self.assertFalse(self.store.mark_surfaced(0))


# ── 2. inventory reads (the demand() hot path) ────────────────────────


class PendingTests(_Base):
    def test_count_pending_is_scoped_by_type(self) -> None:
        self.store.add("interest_drift", "a", "line")
        self.store.add("interest_drift", "b", "line")
        self.store.add("dormant_interest", "c", "line")
        self.assertEqual(self.store.count_pending("interest_drift"), 2)
        self.assertEqual(self.store.count_pending("dormant_interest"), 1)
        self.assertEqual(self.store.count_pending(), 3)

    def test_never_surfaced_cues_come_first(self) -> None:
        tried = self.store.add("interest_drift", "a", "tried")
        self.store.mark_surfaced(tried)
        self.store.release(tried)
        fresh = self.store.add("interest_drift", "b", "fresh")
        order = [row.id for row in self.store.pending("interest_drift")]
        self.assertEqual(order, [fresh, tried])

    def test_non_pending_states_are_not_stock(self) -> None:
        surfaced = self.store.add("interest_drift", "a", "line")
        self.store.mark_surfaced(surfaced)
        awaiting = self.store.add("interest_drift", "b", "line")
        self.store.mark_asked(awaiting)
        self.assertEqual(self.store.count_pending("interest_drift"), 0)

    def test_in_state_finds_the_post_turn_working_sets(self) -> None:
        surfaced = self.store.add("interest_drift", "a", "line")
        self.store.mark_surfaced(surfaced)
        awaiting = self.store.add("forward_curiosity", "b", "line")
        self.store.mark_asked(awaiting)
        self.assertEqual(
            [r.id for r in self.store.in_state(STATE_SURFACED)], [surfaced],
        )
        self.assertEqual(
            [r.id for r in self.store.in_state(STATE_AWAITING)], [awaiting],
        )


class TtlTests(_Base):
    def test_expired_stock_does_not_count_as_pending(self) -> None:
        now = timephrase.utcnow()
        self.store.add("interest_drift", "a", "line", ttl_hours=24, now=now)
        self.assertEqual(self.store.count_pending("interest_drift", now=now), 1)
        later = now + timedelta(hours=25)
        self.assertEqual(
            self.store.count_pending("interest_drift", now=later), 0,
        )
        self.assertEqual(self.store.pending("interest_drift", now=later), [])

    def test_sweep_retires_live_rows_past_their_ttl(self) -> None:
        now = timephrase.utcnow()
        stale = self.store.add("interest_drift", "a", "line", ttl_hours=1, now=now)
        fresh = self.store.add(
            "interest_drift", "b", "line", ttl_hours=100, now=now,
        )
        forever = self.store.add("interest_drift", "c", "line", now=now)
        changed = self.store.sweep_expired(now=now + timedelta(hours=2))
        self.assertEqual(changed, 1)
        self.assertEqual(self.store.get(stale).state, STATE_EXPIRED)
        self.assertEqual(self.store.get(stale).used_evidence, "ttl")
        self.assertEqual(self.store.get(fresh).state, STATE_PENDING)
        self.assertEqual(self.store.get(forever).state, STATE_PENDING)

    def test_sweep_leaves_terminal_rows_alone(self) -> None:
        now = timephrase.utcnow()
        cue_id = self.store.add("interest_drift", "a", "line", ttl_hours=1, now=now)
        self.store.mark_used(cue_id, evidence="lexical:3")
        self.store.sweep_expired(now=now + timedelta(hours=2))
        row = self.store.get(cue_id)
        self.assertEqual(row.state, STATE_USED)
        self.assertEqual(row.used_evidence, "lexical:3")


class RecentSubjectTests(_Base):
    def test_every_state_counts_as_spoken_for(self) -> None:
        used = self.store.add("dormant_interest", "bread", "line")
        self.store.mark_used(used)
        self.store.add("dormant_interest", "pottery", "line")
        self.assertEqual(
            self.store.recent_subjects("dormant_interest"),
            {"bread", "pottery"},
        )

    def test_the_window_forgets(self) -> None:
        self.store.add("dormant_interest", "bread", "line")
        self.assertEqual(
            self.store.recent_subjects("dormant_interest", within_hours=0.0),
            set(),
        )

    def test_states_filter_narrows_to_live_stock(self) -> None:
        used = self.store.add("dormant_interest", "bread", "line")
        self.store.mark_used(used)
        self.store.add("dormant_interest", "pottery", "line")
        self.assertEqual(
            self.store.recent_subjects(
                "dormant_interest", states=[STATE_PENDING],
            ),
            {"pottery"},
        )


# ── 3. the UI / diagnostic reads ──────────────────────────────────────


class ListingTests(_Base):
    def test_filters_and_paging(self) -> None:
        for i in range(5):
            self.store.add("interest_drift", f"topic-{i}", "line")
        self.store.add("dormant_interest", "other", "line")
        self.assertEqual(len(self.store.list_for_user()), 6)
        self.assertEqual(
            len(self.store.list_for_user(cue_type="interest_drift")), 5,
        )
        self.assertEqual(
            len(self.store.list_for_user(limit=2, offset=4)), 2,
        )
        self.assertEqual(self.store.count_for_user(cue_type="interest_drift"), 5)

    def test_state_filter(self) -> None:
        cue_id = self.store.add("interest_drift", "a", "line")
        self.store.add("interest_drift", "b", "line")
        self.store.mark_surfaced(cue_id)
        self.assertEqual(
            len(self.store.list_for_user(state=STATE_SURFACED)), 1,
        )
        self.assertEqual(self.store.count_for_user(state=STATE_PENDING), 1)

    def test_another_user_is_invisible(self) -> None:
        other = CueStore(self.fx.db, user_id="someone-else")
        other.add("interest_drift", "a", "line")
        self.assertEqual(self.store.count_for_user(), 0)
        self.assertEqual(self.store.count_pending(), 0)
        self.assertEqual(other.count_pending(), 1)


class StatsTests(_Base):
    def test_mean_surfacings_before_use_is_reported_per_type(self) -> None:
        # One cue Aiko took immediately, one she needed shown twice.
        quick = self.store.add("interest_drift", "a", "line")
        self.store.mark_surfaced(quick)
        self.store.mark_used(quick)
        slow = self.store.add("interest_drift", "b", "line")
        self.store.mark_surfaced(slow)
        self.store.release(slow)
        self.store.mark_surfaced(slow)
        self.store.mark_used(slow)
        # And one nobody ever wanted.
        dead = self.store.add("interest_drift", "c", "line")
        self.store.expire(dead)

        stats = {row["cue_type"]: row for row in self.store.stats()}
        drift = stats["interest_drift"]
        self.assertEqual(drift["total"], 3)
        self.assertEqual(drift[STATE_USED], 2)
        self.assertEqual(drift[STATE_EXPIRED], 1)
        self.assertEqual(drift["mean_surfacings_before_use"], 1.5)

    def test_every_state_key_is_present_even_at_zero(self) -> None:
        self.store.add("interest_drift", "a", "line")
        drift = self.store.stats()[0]
        self.assertEqual(drift[STATE_PENDING], 1)
        self.assertEqual(drift[STATE_USED], 0)
        self.assertEqual(drift[STATE_EXPIRED], 0)


# ── 4. schema ─────────────────────────────────────────────────────────


class SchemaTests(unittest.TestCase):
    def test_v29_lands_on_a_v28_database(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "legacy.db"
        db = ChatDatabase(path)
        conn = db._get_conn()
        conn.execute("DROP TABLE IF EXISTS cue_pool")
        conn.execute("UPDATE schema_version SET version = 28")
        conn.commit()
        conn.close()
        db._local.conn = None

        db2 = ChatDatabase(path)
        conn2 = db2._get_conn()
        version = conn2.execute("SELECT version FROM schema_version").fetchone()[0]
        self.assertEqual(int(version), _SCHEMA_VERSION)
        store = CueStore(db2)
        self.assertGreater(store.add("interest_drift", "bread", "line"), 0)


class SeedMigrationTests(unittest.TestCase):
    """v29 moves K9 seeds out of ``memories`` and into the pool.

    The only journal-to-pool import worth doing, because a seed memory
    already records what the pool wants to know -- whether the
    conversation reached it, and how many times it sat in the prompt
    first. That is evidence, not a guess.
    """

    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "legacy.db"

    def _v28_with_seeds(self, rows: list[tuple]) -> None:
        db = ChatDatabase(self.path)
        conn = db._get_conn()
        conn.execute("DELETE FROM cue_pool")
        for content, metadata, use_count in rows:
            conn.execute(
                "INSERT INTO memories (content, kind, salience, embedding, "
                "created_at, use_count, metadata, tier) "
                "VALUES (?, 'curiosity_seed', 0.45, ?, ?, ?, ?, 'scratchpad')",
                (
                    content,
                    b"\x00" * 8,
                    "2026-01-01T00:00:00+00:00",
                    use_count,
                    json.dumps(metadata),
                ),
            )
        conn.execute("UPDATE schema_version SET version = 28")
        conn.commit()
        conn.close()
        db._local.conn = None

    def _reopen(self) -> ChatDatabase:
        return ChatDatabase(self.path)

    def test_a_live_seed_arrives_pending_with_its_prompt(self) -> None:
        self._v28_with_seeds([(
            "tea ritual",
            {
                "topic": "tea ritual",
                "prompt_text": "what does your perfect cup look like?",
                "candidate_score": 0.42,
            },
            1,
        )])
        db = self._reopen()
        rows = CueStore(db).list_for_user()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "pending")
        self.assertEqual(rows[0].subject, "tea ritual")
        self.assertEqual(
            rows[0].payload["prompt_text"],
            "what does your perfect cup look like?",
        )
        # The times it sat in the prompt unspoken carry over -- that is
        # the counter max_surfacings is measured against.
        self.assertEqual(rows[0].surfaced_count, 1)

    def test_a_consumed_seed_arrives_already_used(self) -> None:
        self._v28_with_seeds([(
            "old topic",
            {
                "topic": "old topic",
                "prompt_text": "p",
                "consumed_at": "2026-02-01T00:00:00+00:00",
            },
            2,
        )])
        db = self._reopen()
        row = CueStore(db).list_for_user()[0]
        self.assertEqual(row.state, "used")
        self.assertEqual(row.used_evidence, "migrated/k9")

    def test_the_memories_rows_are_gone_afterwards(self) -> None:
        """Leaving them would let RAG surface an unsaid topic as a fact."""
        self._v28_with_seeds([
            ("tea ritual", {"topic": "tea ritual"}, 0),
            ("old topic", {"topic": "old topic", "consumed_at": "x"}, 0),
        ])
        db = self._reopen()
        left = db._get_conn().execute(
            "SELECT COUNT(*) FROM memories WHERE kind = 'curiosity_seed'"
        ).fetchone()[0]
        self.assertEqual(int(left), 0)
        self.assertEqual(len(CueStore(db).list_for_user()), 2)

    def test_a_database_with_no_seeds_is_untouched(self) -> None:
        self._v28_with_seeds([])
        db = self._reopen()
        self.assertEqual(CueStore(db).list_for_user(), [])


if __name__ == "__main__":
    unittest.main()

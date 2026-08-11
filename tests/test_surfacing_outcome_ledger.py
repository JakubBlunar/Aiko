"""L37 -- the surfacing outcome ledger.

The load-bearing property is the **off-by-one**: K14 derives engagement
from the gap between Aiko's last reply and the user's current message, so
the label computed at post-turn N describes the reaction to reply *N-1*.
Attributing it to reply N instead would silently invert the whole signal
while every count still looked plausible, so it gets the most coverage
here.

Also covered: an unsettled row is the *correct* state for a session that
ended rather than a bug (silence after a goodbye is not disengagement),
counts stay distinguishable from rates (a 1-for-1 item must not look like
a 40-of-50 one), the window bounds the aggregate, the v26 migration lands
on legacy databases, and no failure in the ledger can reach the turn.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.memory.echo_detector import (
    ECHO_LEXICAL,
    ECHO_NONE,
    ECHO_SEMANTIC,
    EchoVerdict,
)
from app.core.memory.surfacing_outcome_store import (
    ITEM_KIND_CLUSTER,
    ITEM_KIND_CONCEPT,
    ITEM_KIND_MEMORY,
    ClusterTaste,
    ItemStats,
    SurfacedItem,
    SurfacingOutcomeStore,
    items_from_selection,
)
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.db_path)
        self.store = SurfacingOutcomeStore(self.db)

    def close(self) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def backdate(self, item_id: int, days: int) -> None:
        """Age every row for one item, to exercise ``window_days``."""
        stamp = (timephrase.utcnow() - timedelta(days=days)).isoformat()
        conn = self.db._get_conn()
        conn.execute(
            "UPDATE surfacing_outcomes SET created_at = ? WHERE item_id = ?",
            (stamp, int(item_id)),
        )
        conn.commit()


def _concepts(*ids: int, lane: str = "core") -> list[SurfacedItem]:
    return [
        SurfacedItem(ITEM_KIND_CONCEPT, cid, lane=lane, score=0.5, rank=i)
        for i, cid in enumerate(ids)
    ]


# ── schema ───────────────────────────────────────────────────────────


class SchemaTests(unittest.TestCase):
    def test_fresh_db_has_the_table_and_v26(self) -> None:
        f = _Fixture()
        try:
            self.assertGreaterEqual(_SCHEMA_VERSION, 26)
            row = f.db._get_conn().execute(
                "SELECT version FROM schema_version LIMIT 1",
            ).fetchone()
            self.assertEqual(int(row[0]), _SCHEMA_VERSION)
            cols = {
                r[1] for r in f.db._get_conn().execute(
                    "PRAGMA table_info(surfacing_outcomes)"
                )
            }
            self.assertLessEqual(
                {
                    "assistant_message_id", "item_kind", "item_id", "lane",
                    "surface_reason", "score", "rank", "echoed",
                    "echo_kind", "echo_score",
                    "engagement_label", "created_at", "settled_at",
                },
                cols,
            )
        finally:
            f.close()

    def test_legacy_v25_db_gains_the_table_on_reopen(self) -> None:
        """A pre-v26 database must pick the ledger up in place, with its
        existing rows untouched -- the ladder entry is comment-only, so
        this is really a check that the idempotent DDL is what lands it.
        """
        f = _Fixture()
        try:
            mid = f.db.add_message(
                session_id="s1", role="assistant", content="legacy row",
            )
            conn = f.db._get_conn()
            conn.execute("DROP TABLE surfacing_outcomes")
            conn.execute("UPDATE schema_version SET version = 25")
            conn.commit()
            conn.close()
            f.db._local.conn = None

            f.db = ChatDatabase(f.db_path)
            conn = f.db._get_conn()
            version = int(conn.execute(
                "SELECT version FROM schema_version LIMIT 1",
            ).fetchone()[0])
            self.assertEqual(version, _SCHEMA_VERSION)

            # The table is usable, and the pre-existing message survived.
            store = SurfacingOutcomeStore(f.db)
            self.assertEqual(store.add_many(mid, _concepts(7)), 1)
            self.assertEqual(store.count(), 1)
            self.assertEqual(len(f.db.get_messages("s1")), 1)
        finally:
            f.close()

    def test_legacy_v26_db_gains_the_echo_columns_in_place(self) -> None:
        """v27 is the first ledger bump with a real ALTER, so unlike the
        v26 entry this one has to be exercised: the table already exists,
        so the idempotent CREATE would not add the new columns.

        Existing rows must keep NULL on both. Back-filling ``lexical``
        would assert a semantic comparison had been made and lost, when in
        fact none was ever attempted for those turns.
        """
        f = _Fixture()
        try:
            conn = f.db._get_conn()
            conn.execute("ALTER TABLE surfacing_outcomes DROP COLUMN echo_kind")
            conn.execute("ALTER TABLE surfacing_outcomes DROP COLUMN echo_score")
            conn.execute(
                "INSERT INTO surfacing_outcomes "
                "(assistant_message_id, item_kind, item_id, lane, "
                " surface_reason, score, rank, echoed, created_at) "
                "VALUES (5, 'concept', 42, '', '', 0.0, 0, 1, ?)",
                (timephrase.utcnow().isoformat(),),
            )
            conn.execute("UPDATE schema_version SET version = 26")
            conn.commit()
            conn.close()
            f.db._local.conn = None

            f.db = ChatDatabase(f.db_path)
            conn = f.db._get_conn()
            self.assertEqual(
                int(conn.execute(
                    "SELECT version FROM schema_version LIMIT 1",
                ).fetchone()[0]),
                _SCHEMA_VERSION,
            )
            row = conn.execute(
                "SELECT echoed, echo_kind, echo_score FROM surfacing_outcomes"
            ).fetchone()
            self.assertEqual(int(row[0]), 1)
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])

            # And the widened write path works against the migrated table.
            store = SurfacingOutcomeStore(f.db)
            self.assertEqual(
                store.add_many(
                    5, _concepts(7),
                    echoes={(ITEM_KIND_CONCEPT, 7): EchoVerdict(
                        ECHO_SEMANTIC, 0.71,
                    )},
                ),
                1,
            )
        finally:
            f.close()

    def test_reopening_an_up_to_date_db_does_not_duplicate_columns(
        self,
    ) -> None:
        """The ALTER is guarded by a swallowed OperationalError, so a
        no-op reopen has to stay a no-op rather than a logged failure.
        """
        f = _Fixture()
        try:
            f.db._local.conn.close()
            f.db._local.conn = None
            f.db = ChatDatabase(f.db_path)
            cols = [
                r[1] for r in f.db._get_conn().execute(
                    "PRAGMA table_info(surfacing_outcomes)"
                )
            ]
            self.assertEqual(cols.count("echo_kind"), 1)
            self.assertEqual(cols.count("echo_score"), 1)
        finally:
            f.close()

    def test_windowed_aggregate_uses_the_covering_index(self) -> None:
        """The per-item read runs once per candidate per turn once L38
        lands, so an item seek followed by a date scan would not do.
        """
        f = _Fixture()
        try:
            plan = " ".join(
                str(r[-1]) for r in f.db._get_conn().execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT item_id, COUNT(*) FROM surfacing_outcomes "
                    "WHERE item_kind = ? AND item_id IN (1, 2) "
                    "  AND created_at >= ? GROUP BY item_id",
                    ("concept", "2026-01-01"),
                ).fetchall()
            )
            self.assertIn("idx_surfacing_outcomes_item", plan)
            self.assertIn("COVERING INDEX", plan.upper())
        finally:
            f.close()


# ── store ────────────────────────────────────────────────────────────


class StoreWriteTests(unittest.TestCase):
    def test_add_many_records_the_whole_set_with_provenance(self) -> None:
        f = _Fixture()
        try:
            items = [
                SurfacedItem(
                    ITEM_KIND_CONCEPT, 4, lane="flex",
                    surface_reason="topic_match", score=0.62, rank=2,
                ),
            ]
            self.assertEqual(f.store.add_many(11, items), 1)
            row = f.db._get_conn().execute(
                "SELECT item_kind, item_id, lane, surface_reason, score, rank "
                "FROM surfacing_outcomes"
            ).fetchone()
            self.assertEqual(row[0], ITEM_KIND_CONCEPT)
            self.assertEqual(int(row[1]), 4)
            self.assertEqual(row[2], "flex")
            self.assertEqual(row[3], "topic_match")
            self.assertAlmostEqual(float(row[4]), 0.62, places=5)
            self.assertEqual(int(row[5]), 2)
        finally:
            f.close()

    def test_add_many_rejects_junk_keys_and_ids(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(f.store.add_many(0, _concepts(1)), 0)
            self.assertEqual(f.store.add_many(5, []), 0)
            self.assertEqual(
                f.store.add_many(5, [SurfacedItem(ITEM_KIND_CONCEPT, 0)]), 0,
            )
            self.assertEqual(f.store.add_many(5, [SurfacedItem("", 3)]), 0)
            self.assertEqual(f.store.count(), 0)
        finally:
            f.close()

    def test_unmarked_items_leave_echoed_null(self) -> None:
        """NULL means "we could not look", which has to stay distinct
        from a computed False -- a cluster we cannot resolve is not the
        same finding as a memory Aiko demonstrably ignored.
        """
        f = _Fixture()
        try:
            f.store.add_many(
                12,
                [
                    SurfacedItem(ITEM_KIND_CONCEPT, 1),
                    SurfacedItem(ITEM_KIND_CONCEPT, 2),
                    SurfacedItem(ITEM_KIND_CLUSTER, 3),
                ],
                echoes={
                    (ITEM_KIND_CONCEPT, 1): EchoVerdict(ECHO_LEXICAL, 4.0),
                    (ITEM_KIND_CONCEPT, 2): EchoVerdict(),
                },
            )
            got = {
                (r[0], int(r[1])): (r[2], r[3], r[4])
                for r in f.db._get_conn().execute(
                    "SELECT item_kind, item_id, echoed, echo_kind, echo_score "
                    "FROM surfacing_outcomes"
                )
            }
            self.assertEqual(got[(ITEM_KIND_CONCEPT, 1)], (1, ECHO_LEXICAL, 4.0))
            self.assertEqual(got[(ITEM_KIND_CONCEPT, 2)], (0, ECHO_NONE, 0.0))
            self.assertEqual(got[(ITEM_KIND_CLUSTER, 3)], (None, None, None))
        finally:
            f.close()

    def test_a_sub_floor_cosine_is_still_recorded(self) -> None:
        """The near misses are the calibration data. A floor cannot be
        re-derived from a table that only kept the rows that cleared the
        floor we happened to guess first.
        """
        f = _Fixture()
        try:
            f.store.add_many(
                13,
                [SurfacedItem(ITEM_KIND_MEMORY, 7)],
                echoes={(ITEM_KIND_MEMORY, 7): EchoVerdict(ECHO_NONE, 0.58)},
            )
            row = f.db._get_conn().execute(
                "SELECT echoed, echo_kind, echo_score FROM surfacing_outcomes"
            ).fetchone()
            self.assertEqual(int(row[0]), 0)
            self.assertEqual(row[1], ECHO_NONE)
            self.assertAlmostEqual(float(row[2]), 0.58, places=6)
        finally:
            f.close()

    def test_settle_is_idempotent_and_never_overwrites(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(20, _concepts(1, 2))
            self.assertEqual(f.store.settle(20, "engaged"), 2)
            # A retry, or a later turn arriving with a stale key, must not
            # be able to replace an already-recorded verdict.
            self.assertEqual(f.store.settle(20, "abandoned"), 0)
            labels = {
                r[0] for r in f.db._get_conn().execute(
                    "SELECT engagement_label FROM surfacing_outcomes"
                )
            }
            self.assertEqual(labels, {"engaged"})
        finally:
            f.close()

    def test_settle_ignores_unknown_and_blank_keys(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(20, _concepts(1))
            self.assertEqual(f.store.settle(999, "engaged"), 0)
            self.assertEqual(f.store.settle(20, ""), 0)
            self.assertEqual(f.store.unsettled_count(), 1)
        finally:
            f.close()

    def test_store_methods_swallow_a_broken_database(self) -> None:
        """Every method degrades to an empty result: a ledger write must
        never be able to break a turn.
        """
        f = _Fixture()
        try:
            f.db._get_conn().execute("DROP TABLE surfacing_outcomes")
            self.assertEqual(f.store.add_many(1, _concepts(1)), 0)
            self.assertEqual(f.store.settle(1, "engaged"), 0)
            self.assertEqual(
                f.store.stats_for(ITEM_KIND_CONCEPT, [1], window_days=None), {},
            )
            self.assertEqual(f.store.leaderboard(), [])
            self.assertEqual(
                f.store.engaged_rate_by_cluster(window_days=None), {},
            )
            self.assertEqual(f.store.lane_breakdown(), [])
            self.assertEqual(f.store.count(), 0)
            self.assertEqual(f.store.unsettled_count(), 0)
            self.assertEqual(f.store.prune(1), 0)
        finally:
            f.close()


class StoreReadTests(unittest.TestCase):
    def test_counts_split_settled_engaged_and_echoed(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(
                1, _concepts(1),
                echoes={(ITEM_KIND_CONCEPT, 1): EchoVerdict(ECHO_LEXICAL, 3.0)},
            )
            f.store.settle(1, "engaged")
            f.store.add_many(
                2, _concepts(1), echoes={(ITEM_KIND_CONCEPT, 1): EchoVerdict()},
            )
            f.store.settle(2, "disengaged")
            stats = f.store.stats_for(
                ITEM_KIND_CONCEPT, [1], window_days=None,
            )[1]
            self.assertEqual(
                stats,
                ItemStats(
                    surfaced=2, settled=2, engaged=1, echoed=1, judged=2,
                ),
            )
            self.assertAlmostEqual(stats.engaged_rate, 0.5)
            self.assertAlmostEqual(stats.echo_rate, 0.5)
        finally:
            f.close()

    def test_an_unjudged_item_reports_no_echo_rate_rather_than_zero(self) -> None:
        """Clusters never get an echo test. Dividing echoes by surfacings
        would report a confident 0.0 for a population nobody measured --
        and L38 reads exactly this denominator.
        """
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1))
            f.store.settle(1, "engaged")
            stats = f.store.stats_for(
                ITEM_KIND_CONCEPT, [1], window_days=None,
            )[1]
            self.assertEqual(stats.surfaced, 1)
            self.assertEqual(stats.judged, 0)
            self.assertIsNone(stats.echo_rate)
        finally:
            f.close()

    def test_no_settled_rows_reports_none_not_a_zero_rate(self) -> None:
        """"No evidence" and "evidence it never lands" are different
        findings; collapsing both to 0.0 would let a consumer punish an
        item purely for being new.
        """
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1))
            stats = f.store.stats_for(
                ITEM_KIND_CONCEPT, [1], window_days=None,
            )[1]
            self.assertEqual(stats.settled, 0)
            self.assertEqual(stats.surfaced, 1)
            self.assertIsNone(stats.engaged_rate)
            self.assertIsNone(ItemStats().echo_rate)
        finally:
            f.close()

    def test_a_thin_sample_is_distinguishable_from_a_confident_one(self) -> None:
        """Both items are 100% engaged. Only the denominator separates a
        coincidence from a real finding, which is why the API returns
        counts rather than a rate.
        """
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1, 2))
            f.store.settle(1, "engaged")
            for turn in range(2, 12):
                f.store.add_many(turn, _concepts(2))
                f.store.settle(turn, "engaged")
            stats = f.store.stats_for(
                ITEM_KIND_CONCEPT, [1, 2], window_days=None,
            )
            self.assertEqual(stats[1].engaged_rate, stats[2].engaged_rate)
            self.assertEqual(stats[1].settled, 1)
            self.assertEqual(stats[2].settled, 11)
        finally:
            f.close()

    def test_window_days_bounds_the_aggregate(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1))
            f.store.settle(1, "engaged")
            f.backdate(1, days=90)
            f.store.add_many(2, _concepts(1))
            f.store.settle(2, "disengaged")

            lifetime = f.store.stats_for(
                ITEM_KIND_CONCEPT, [1], window_days=None,
            )[1]
            self.assertEqual(lifetime.settled, 2)
            self.assertAlmostEqual(lifetime.engaged_rate, 0.5)

            recent = f.store.stats_for(
                ITEM_KIND_CONCEPT, [1], window_days=14,
            )[1]
            self.assertEqual(recent.settled, 1)
            self.assertEqual(recent.engaged, 0)
        finally:
            f.close()

    def test_stats_for_can_filter_to_flex_and_activation_lanes(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1, lane="core"))
            f.store.settle(1, "engaged")
            f.store.add_many(2, _concepts(1, lane="flex"))
            f.store.settle(2, "disengaged")
            f.store.add_many(3, _concepts(1, lane="activation"))
            f.store.settle(3, "engaged")

            flex = f.store.stats_for(
                ITEM_KIND_CONCEPT,
                [1],
                window_days=None,
                lanes=("flex", "activation"),
            )[1]
            self.assertEqual(
                flex, ItemStats(surfaced=2, settled=2, engaged=1, echoed=0),
            )
            self.assertEqual(
                f.store.stats_for(
                    ITEM_KIND_CONCEPT,
                    [1],
                    window_days=None,
                    lanes=("missing",),
                ),
                {},
            )
        finally:
            f.close()

    def test_stats_for_is_namespaced_by_kind_and_skips_unknown_ids(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, [
                SurfacedItem(ITEM_KIND_CONCEPT, 5),
                SurfacedItem(ITEM_KIND_MEMORY, 5),
            ])
            concepts = f.store.stats_for(
                ITEM_KIND_CONCEPT, [5], window_days=None,
            )
            self.assertEqual(concepts[5].surfaced, 1)
            self.assertEqual(
                f.store.stats_for(ITEM_KIND_MEMORY, [5], window_days=None,
                                  )[5].surfaced, 1,
            )
            self.assertEqual(
                f.store.stats_for(ITEM_KIND_CONCEPT, [404], window_days=None),
                {},
            )
            self.assertEqual(
                f.store.stats_for(ITEM_KIND_CONCEPT, [], window_days=None), {},
            )
            self.assertEqual(
                f.store.stats_for("", [5], window_days=None), {},
            )
        finally:
            f.close()

    def test_leaderboard_min_settled_keeps_out_single_observations(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1, 2))
            f.store.settle(1, "engaged")
            f.store.add_many(2, _concepts(2))
            f.store.settle(2, "engaged")
            top = f.store.leaderboard(min_settled=2)
            self.assertEqual([r["item_id"] for r in top], [2])
            self.assertEqual(top[0]["settled"], 2)
            self.assertEqual(top[0]["engaged_rate"], 1.0)
            self.assertEqual(
                len(f.store.leaderboard(min_settled=1)), 2,
            )
        finally:
            f.close()

    def test_lane_breakdown_only_counts_settled_rows(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1, lane="core"))
            f.store.add_many(1, _concepts(2, lane="activation"))
            f.store.settle(1, "engaged")
            # An unsettled turn contributes to neither lane.
            f.store.add_many(2, _concepts(3, lane="activation"))
            lanes = {r["lane"]: r for r in f.store.lane_breakdown()}
            self.assertEqual(lanes["core"]["settled"], 1)
            self.assertEqual(lanes["activation"]["settled"], 1)
            self.assertEqual(lanes["activation"]["engaged_rate"], 1.0)
        finally:
            f.close()

    def test_prune_drops_only_aged_rows(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, _concepts(1))
            f.backdate(1, days=400)
            f.store.add_many(2, _concepts(2))
            self.assertEqual(f.store.prune(0), 0)
            self.assertEqual(f.store.prune(365), 1)
            self.assertEqual(f.store.count(), 1)
            self.assertEqual(
                f.store.stats_for(ITEM_KIND_CONCEPT, [2], window_days=None,
                                  )[2].surfaced, 1,
            )
        finally:
            f.close()


# ── per-cluster taste (K81) ──────────────────────────────────────────


class ClusterTasteTests(unittest.TestCase):
    """K81's read-model: engagement folded per topic cluster.

    Both a ``cluster`` row (``item_id`` is the cluster) and a ``memory``
    row joined through ``memory_topic_assignments`` count toward the same
    cluster; concept / cue rows never do. The ``min_settled`` warmup floor
    keeps a one-observation cluster from claiming a confident taste.
    """

    def _assign(self, f: _Fixture, memory_id: int, cluster_id: int) -> None:
        conn = f.db._get_conn()
        conn.execute(
            "INSERT INTO memory_topic_assignments "
            "(memory_id, cluster_id, assigned_at) VALUES (?, ?, ?)",
            (int(memory_id), int(cluster_id), timephrase.utcnow().isoformat()),
        )
        conn.commit()

    def test_cluster_rows_aggregate_by_cluster_id(self) -> None:
        f = _Fixture()
        try:
            # Cluster 7 lands twice of two; cluster 8 lands zero of one.
            f.store.add_many(1, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            f.store.settle(1, "engaged")
            f.store.add_many(2, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            f.store.settle(2, "engaged")
            f.store.add_many(3, [SurfacedItem(ITEM_KIND_CLUSTER, 8)])
            f.store.settle(3, "disengaged")
            taste = f.store.engaged_rate_by_cluster(window_days=None)
            self.assertEqual(taste[7], ClusterTaste(7, surfaced=2, settled=2, engaged=2))
            self.assertAlmostEqual(taste[7].engaged_rate, 1.0)
            self.assertAlmostEqual(taste[8].engaged_rate, 0.0)
        finally:
            f.close()

    def test_memory_rows_join_to_their_cluster(self) -> None:
        f = _Fixture()
        try:
            self._assign(f, memory_id=9, cluster_id=7)
            f.store.add_many(1, [SurfacedItem(ITEM_KIND_MEMORY, 9)])
            f.store.settle(1, "engaged")
            # A cluster label surfaced for the same cluster on another turn
            # folds into the same bucket.
            f.store.add_many(2, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            f.store.settle(2, "disengaged")
            taste = f.store.engaged_rate_by_cluster(window_days=None)
            self.assertEqual(taste[7].surfaced, 2)
            self.assertEqual(taste[7].settled, 2)
            self.assertEqual(taste[7].engaged, 1)
            self.assertAlmostEqual(taste[7].engaged_rate, 0.5)
        finally:
            f.close()

    def test_unclustered_memory_and_other_kinds_are_ignored(self) -> None:
        f = _Fixture()
        try:
            # Memory 9 has no assignment; concept + cue carry no cluster.
            f.store.add_many(1, [
                SurfacedItem(ITEM_KIND_MEMORY, 9),
                SurfacedItem(ITEM_KIND_CONCEPT, 3),
            ])
            f.store.add_many(2, [SurfacedItem("cue", 0, )])
            f.store.settle(1, "engaged")
            f.store.settle(2, "engaged")
            self.assertEqual(
                f.store.engaged_rate_by_cluster(window_days=None), {},
            )
        finally:
            f.close()

    def test_min_settled_is_a_warmup_floor(self) -> None:
        f = _Fixture()
        try:
            # Cluster 7: one settled. Cluster 8: two settled.
            f.store.add_many(1, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            f.store.settle(1, "engaged")
            f.store.add_many(2, [SurfacedItem(ITEM_KIND_CLUSTER, 8)])
            f.store.settle(2, "engaged")
            f.store.add_many(3, [SurfacedItem(ITEM_KIND_CLUSTER, 8)])
            f.store.settle(3, "engaged")
            got = f.store.engaged_rate_by_cluster(window_days=None, min_settled=2)
            self.assertEqual(set(got), {8})
            self.assertEqual(got[8].settled, 2)
        finally:
            f.close()

    def test_window_days_bounds_the_cluster_aggregate(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            f.store.settle(1, "engaged")
            # A cluster row uses item_id = cluster_id, so backdating by
            # item_id ages exactly this cluster's row.
            f.backdate(7, days=90)
            f.store.add_many(2, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            f.store.settle(2, "disengaged")
            lifetime = f.store.engaged_rate_by_cluster(window_days=None)
            self.assertEqual(lifetime[7].settled, 2)
            recent = f.store.engaged_rate_by_cluster(window_days=14)
            self.assertEqual(recent[7].settled, 1)
            self.assertEqual(recent[7].engaged, 0)
        finally:
            f.close()

    def test_unsettled_cluster_absent_below_default_floor(self) -> None:
        f = _Fixture()
        try:
            f.store.add_many(1, [SurfacedItem(ITEM_KIND_CLUSTER, 7)])
            # No settle: settled=0, below the default min_settled=1.
            self.assertEqual(
                f.store.engaged_rate_by_cluster(window_days=None), {},
            )
        finally:
            f.close()


# ── selection projection ─────────────────────────────────────────────


@dataclass
class _Cand:
    payload: object
    relevance: float = 0.0
    order: int = 0


class _Source:
    def __init__(self, chosen: list) -> None:
        self.chosen = chosen


class _Selection:
    def __init__(self, **sources: list) -> None:
        self._sources = {k: _Source(v) for k, v in sources.items()}

    def source(self, name: str) -> _Source:
        return self._sources[name]


@dataclass
class _Concept:
    concept_id: int


@dataclass
class _Record:
    id: int


@dataclass
class _Hit:
    source: str
    record: object


class SelectionProjectionTests(unittest.TestCase):
    def test_projects_all_three_kinds_with_rank_and_score(self) -> None:
        selection = _Selection(
            concept=[_Cand(_Concept(3), relevance=0.8, order=1)],
            memory=[_Cand(_Hit("memory", _Record(9)), relevance=0.7, order=0)],
            cluster=[_Cand((4, "gardening", 0.6), relevance=0.6, order=2)],
        )
        items = items_from_selection(
            selection, score_components={3: {"lane": "core", "reason": "pin"}},
        )
        by_kind = {i.item_kind: i for i in items}
        self.assertEqual(by_kind[ITEM_KIND_CONCEPT].item_id, 3)
        self.assertEqual(by_kind[ITEM_KIND_CONCEPT].lane, "core")
        self.assertEqual(by_kind[ITEM_KIND_CONCEPT].surface_reason, "pin")
        self.assertAlmostEqual(by_kind[ITEM_KIND_CONCEPT].score, 0.8)
        self.assertEqual(by_kind[ITEM_KIND_CONCEPT].rank, 1)
        self.assertEqual(by_kind[ITEM_KIND_MEMORY].item_id, 9)
        self.assertEqual(by_kind[ITEM_KIND_CLUSTER].item_id, 4)

    def test_non_memory_rag_hits_are_skipped(self) -> None:
        """Message and document ids live in different namespaces; recording
        them as memories would collide with real memory ids.
        """
        selection = _Selection(
            concept=[], cluster=[],
            memory=[
                _Cand(_Hit("message", _Record(1))),
                _Cand(_Hit("document", _Record(2))),
                _Cand(_Hit("memory", _Record(3))),
            ],
        )
        items = items_from_selection(selection)
        self.assertEqual(
            [(i.item_kind, i.item_id) for i in items],
            [(ITEM_KIND_MEMORY, 3)],
        )

    def test_malformed_candidates_are_dropped_not_raised(self) -> None:
        selection = _Selection(
            concept=[_Cand(_Concept(0)), _Cand(None)],
            memory=[_Cand(_Hit("memory", None))],
            cluster=[_Cand(()), _Cand(("bad",))],
        )
        self.assertEqual(items_from_selection(selection), [])
        self.assertEqual(items_from_selection(None), [])

    def test_a_missing_source_does_not_lose_the_others(self) -> None:
        selection = _Selection(concept=[_Cand(_Concept(1), order=0)])
        items = items_from_selection(selection)
        self.assertEqual([i.item_id for i in items], [1])


# ── post-turn wiring: the off-by-one ─────────────────────────────────


class _FakeSettings:
    class agent:  # noqa: N801 -- mirrors the settings attribute path
        surfacing_echo_min_overlap_concept = 1


class _FakeMemorySettings:
    revival_min_word_overlap = 3
    tiers_enabled = True
    # Off by default in this harness so the lexical assertions stay about
    # the lexical test; the semantic tests switch it on explicitly.
    semantic_revival_enabled = False
    semantic_revival_min_cosine = 0.62
    semantic_revival_per_hit = 0.05


class _Host(PostTurnHelpersMixin):
    """Minimal carrier for the two ledger helpers.

    Only the attributes those helpers touch, so the test exercises the
    attribution logic rather than the whole post-turn orchestrator.
    """

    session_key = "s1"

    def __init__(
        self, store: SurfacingOutcomeStore, chat_db: ChatDatabase | None = None,
    ) -> None:
        self._surfacing_outcome_store = store
        self._settings = _FakeSettings()
        self._memory_settings = _FakeMemorySettings()
        self._memory_store = None
        self._concept_store = None
        self._chat_db = chat_db
        self._last_surfaced_items: list = []
        self._prev_surfacing_message_id = 0

    def turn(
        self,
        *,
        assistant_message_id: int | None,
        surfaced: list,
        engagement_label: str | None,
        assistant_text: str = "",
        user_message_id: int | None = None,
    ) -> None:
        """One simulated turn: surfacing stashes, then post-turn runs."""
        self._last_surfaced_items = list(surfaced)
        self._record_surfacing_outcomes(
            assistant_text=assistant_text,
            assistant_message_id=assistant_message_id,
            engagement_label=engagement_label,
            user_message_id=user_message_id,
        )


class OffByOneAttributionTests(unittest.TestCase):
    """The single most important property in the feature."""

    def setUp(self) -> None:
        self.f = _Fixture()
        self.host = _Host(self.f.store)

    def tearDown(self) -> None:
        self.f.close()

    def _labels(self) -> dict[int, str | None]:
        return {
            int(r[0]): r[1]
            for r in self.f.db._get_conn().execute(
                "SELECT assistant_message_id, engagement_label "
                "FROM surfacing_outcomes"
            )
        }

    def test_the_label_settles_the_previous_reply_not_the_current_one(
        self,
    ) -> None:
        # Turn 1: nothing has been surfaced before, so the label observed
        # here belongs to no ledger row.
        self.host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label="engaged",
        )
        self.assertEqual(self._labels(), {101: None})

        # Turn 2: the label observed now is the user's reaction to reply
        # 101, so it must land on 101's rows and leave 102's open.
        self.host.turn(
            assistant_message_id=102, surfaced=_concepts(2),
            engagement_label="disengaged",
        )
        self.assertEqual(self._labels(), {101: "disengaged", 102: None})

        # Turn 3 settles 102, and so on down the chain.
        self.host.turn(
            assistant_message_id=103, surfaced=_concepts(3),
            engagement_label="engaged",
        )
        self.assertEqual(
            self._labels(), {101: "disengaged", 102: "engaged", 103: None},
        )

    def test_a_session_ending_leaves_the_last_turn_unsettled(self) -> None:
        """Silence after a goodbye is not disengagement. The row stays
        open rather than being credited with the worst label.
        """
        self.host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label="engaged",
        )
        self.assertEqual(self.f.store.unsettled_count(), 1)
        stats = self.f.store.stats_for(
            ITEM_KIND_CONCEPT, [1], window_days=None,
        )[1]
        self.assertEqual(stats.surfaced, 1)
        self.assertEqual(stats.settled, 0)
        self.assertEqual(stats.engaged, 0)

    def test_a_missing_label_leaves_the_previous_rows_open(self) -> None:
        """A turn whose engagement pass failed must not settle with a
        stale label -- it settles with nothing.
        """
        self.host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label="engaged",
        )
        self.host.turn(
            assistant_message_id=102, surfaced=_concepts(2),
            engagement_label=None,
        )
        self.assertEqual(self._labels(), {101: None, 102: None})

    def test_a_turn_that_surfaced_nothing_breaks_the_carry(self) -> None:
        """Otherwise turn 3's engagement would be attributed to turn 1's
        reply, two turns and one whole exchange later.
        """
        self.host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label=None,
        )
        self.host.turn(
            assistant_message_id=102, surfaced=[],
            engagement_label="engaged",
        )
        self.assertEqual(self._labels(), {101: "engaged"})
        self.host.turn(
            assistant_message_id=103, surfaced=_concepts(3),
            engagement_label="abandoned",
        )
        # 101 keeps its own verdict; nothing was re-attributed to it.
        self.assertEqual(self._labels(), {101: "engaged", 103: None})

    def test_the_stash_is_consumed_so_a_set_is_never_credited_twice(
        self,
    ) -> None:
        self.host.turn(
            assistant_message_id=101, surfaced=_concepts(1, 2),
            engagement_label=None,
        )
        self.assertEqual(self.host._last_surfaced_items, [])
        # A second post-turn with no fresh surfacing writes nothing more.
        self.host._record_surfacing_outcomes(
            assistant_text="", assistant_message_id=102,
            engagement_label=None,
        )
        self.assertEqual(self.f.store.count(), 2)

    def test_no_assistant_message_id_records_nothing(self) -> None:
        self.host.turn(
            assistant_message_id=None, surfaced=_concepts(1),
            engagement_label=None,
        )
        self.assertEqual(self.f.store.count(), 0)
        self.assertEqual(self.host._prev_surfacing_message_id, 0)

    def test_a_disabled_ledger_still_drains_the_stash(self) -> None:
        """Otherwise turning the feature off would leave a stale set on
        the session to be written by a later enabled turn.
        """
        host = _Host(self.f.store)
        host._surfacing_outcome_store = None
        host._last_surfaced_items = _concepts(1)
        host._record_surfacing_outcomes(
            assistant_text="", assistant_message_id=101,
            engagement_label=None,
        )
        self.assertEqual(host._last_surfaced_items, [])
        self.assertEqual(self.f.store.count(), 0)

    def test_a_skipped_post_turn_declines_to_settle_a_stale_carry(
        self,
    ) -> None:
        """``_post_turn_inner_life`` can bail before this hook, leaving the
        carry a turn behind. Settling it anyway would credit turn 3's
        reaction to turn 1's reply, which is a wrong number rather than a
        missing one.
        """
        host = _Host(self.f.store, chat_db=self.f.db)
        u1 = self.f.db.add_message(session_id="s1", role="user", content="a")
        a1 = self.f.db.add_message(
            session_id="s1", role="assistant", content="reply one",
        )
        host.turn(
            assistant_message_id=a1, surfaced=_concepts(1),
            engagement_label=None, user_message_id=u1,
        )
        # Turn 2 happens but its post-turn bails before the hook, so the
        # carry still points at a1 while a2 exists in the transcript.
        self.f.db.add_message(session_id="s1", role="user", content="b")
        self.f.db.add_message(
            session_id="s1", role="assistant", content="reply two",
        )
        # Turn 3: the label describes reply two, not reply one.
        u3 = self.f.db.add_message(session_id="s1", role="user", content="c")
        a3 = self.f.db.add_message(
            session_id="s1", role="assistant", content="reply three",
        )
        host.turn(
            assistant_message_id=a3, surfaced=_concepts(3),
            engagement_label="engaged", user_message_id=u3,
        )
        self.assertEqual(self._labels(), {a1: None, a3: None})
        self.assertEqual(host._prev_surfacing_message_id, a3)

    def test_an_unbroken_chain_still_settles_with_the_guard_on(self) -> None:
        host = _Host(self.f.store, chat_db=self.f.db)
        u1 = self.f.db.add_message(session_id="s1", role="user", content="a")
        a1 = self.f.db.add_message(
            session_id="s1", role="assistant", content="reply one",
        )
        host.turn(
            assistant_message_id=a1, surfaced=_concepts(1),
            engagement_label=None, user_message_id=u1,
        )
        u2 = self.f.db.add_message(session_id="s1", role="user", content="b")
        a2 = self.f.db.add_message(
            session_id="s1", role="assistant", content="reply two",
        )
        host.turn(
            assistant_message_id=a2, surfaced=_concepts(2),
            engagement_label="engaged", user_message_id=u2,
        )
        self.assertEqual(self._labels(), {a1: "engaged", a2: None})

    def test_an_unverifiable_carry_is_treated_as_current(self) -> None:
        """No user message id and no database is the normal shape on
        several paths; defaulting to "decline" there would starve the
        ledger of every outcome it exists to record.
        """
        host = _Host(self.f.store, chat_db=None)
        self.assertTrue(host._surfacing_carry_is_current(5, 9))
        host_db = _Host(self.f.store, chat_db=self.f.db)
        self.assertTrue(host_db._surfacing_carry_is_current(5, None))
        self.assertTrue(host_db._surfacing_carry_is_current(0, 9))

    def test_a_raising_carry_check_does_not_block_settlement(self) -> None:
        class _Boom:
            def has_assistant_message_between(self, *_a):
                raise RuntimeError("locked")

        host = _Host(self.f.store, chat_db=_Boom())
        self.assertTrue(host._surfacing_carry_is_current(5, 9))

    def test_a_broken_store_does_not_raise_into_the_turn(self) -> None:
        self.f.db._get_conn().execute("DROP TABLE surfacing_outcomes")
        self.host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label="engaged",
        )
        # The insert failed, so nothing is carried for a later settle.
        self.assertEqual(self.host._prev_surfacing_message_id, 0)


# ── echo marks ───────────────────────────────────────────────────────


@dataclass
class _Memory:
    content: str


@dataclass
class _LabelledConcept:
    label: str


class _Store:
    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def get(self, key: int):
        return self._rows.get(int(key))


class EchoMarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = _Fixture()
        self.host = _Host(self.f.store)
        self.host._memory_store = _Store({
            9: _Memory("dmitri keeps a sourdough starter in the fridge"),
            8: _Memory("prefers window seats on long haul flights"),
        })
        self.host._concept_store = _Store({
            3: _LabelledConcept("guarded about family"),
            4: _LabelledConcept("collects vinyl records"),
        })

    def tearDown(self) -> None:
        self.f.close()

    def test_concepts_use_a_lower_bar_than_memories(self) -> None:
        """A concept label is three to six words, so the memory-tuned
        three-word overlap would be a far harsher test of the same thing.
        """
        items = [
            SurfacedItem(ITEM_KIND_CONCEPT, 3),
            SurfacedItem(ITEM_KIND_MEMORY, 9),
        ]
        # One content word shared with each: enough for the concept at a
        # floor of 1, not enough for the memory at a floor of 3.
        marks = self.host._surfacing_echo_marks(
            items, "you get guarded whenever sourdough comes up",
        )
        self.assertEqual(marks[(ITEM_KIND_CONCEPT, 3)].kind, ECHO_LEXICAL)
        self.assertFalse(marks[(ITEM_KIND_MEMORY, 9)].echoed)

    def test_a_memory_clears_its_floor_on_real_overlap(self) -> None:
        marks = self.host._surfacing_echo_marks(
            [SurfacedItem(ITEM_KIND_MEMORY, 9)],
            "how is the sourdough starter doing in the fridge",
        )
        self.assertEqual(marks[(ITEM_KIND_MEMORY, 9)].kind, ECHO_LEXICAL)

    def test_unresolvable_items_are_omitted_rather_than_marked_false(
        self,
    ) -> None:
        marks = self.host._surfacing_echo_marks(
            [
                SurfacedItem(ITEM_KIND_CONCEPT, 404),
                SurfacedItem(ITEM_KIND_CLUSTER, 3),
                SurfacedItem(ITEM_KIND_MEMORY, 0),
            ],
            "some reply with plenty of content words in it",
        )
        self.assertEqual(marks, {})

    def test_a_curt_reply_marks_false_rather_than_unknown(self) -> None:
        """No content words is a real observation -- she echoed nothing --
        unlike an item whose text could not be loaded.
        """
        marks = self.host._surfacing_echo_marks(
            [SurfacedItem(ITEM_KIND_CONCEPT, 3)], "ok, yes",
        )
        self.assertEqual(marks, {(ITEM_KIND_CONCEPT, 3): EchoVerdict()})

    def test_no_assistant_text_yields_no_marks(self) -> None:
        self.assertEqual(
            self.host._surfacing_echo_marks(
                [SurfacedItem(ITEM_KIND_CONCEPT, 3)], "",
            ),
            {},
        )

    def test_marks_reach_the_ledger_through_post_turn(self) -> None:
        self.host.turn(
            assistant_message_id=101,
            surfaced=[
                SurfacedItem(ITEM_KIND_CONCEPT, 3),
                SurfacedItem(ITEM_KIND_CONCEPT, 4),
            ],
            engagement_label=None,
            assistant_text="you seem guarded about that",
        )
        self.host.turn(
            assistant_message_id=102, surfaced=[],
            engagement_label="engaged",
        )
        stats = self.f.store.stats_for(
            ITEM_KIND_CONCEPT, [3, 4], window_days=None,
        )
        self.assertEqual(stats[3].echoed, 1)
        self.assertEqual(stats[4].echoed, 0)
        self.assertEqual(stats[3].engaged, 1)
        self.assertEqual(stats[4].engaged, 1)

    def test_a_raising_concept_store_does_not_break_the_marks(self) -> None:
        class _Boom:
            def get(self, _key):
                raise RuntimeError("cold")

        self.host._concept_store = _Boom()
        marks = self.host._surfacing_echo_marks(
            [
                SurfacedItem(ITEM_KIND_CONCEPT, 3),
                SurfacedItem(ITEM_KIND_MEMORY, 9),
            ],
            "how is the sourdough starter doing in the fridge",
        )
        self.assertEqual(
            marks, {(ITEM_KIND_MEMORY, 9): EchoVerdict(ECHO_LEXICAL, 3.0)},
        )


# ── MCP view ─────────────────────────────────────────────────────────


class McpReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = _Fixture()

    def tearDown(self) -> None:
        self.f.close()

    def _report(self, session, **kw):
        from app.mcp.server_tools.surfacing_outcome_tools import build_report

        kw.setdefault("window_days", None)
        kw.setdefault("min_settled", 1)
        kw.setdefault("top", 20)
        return build_report(session, **kw)

    def test_reports_denominators_beside_every_rate(self) -> None:
        host = _Host(self.f.store)
        host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label=None,
        )
        host.turn(
            assistant_message_id=102, surfaced=_concepts(2),
            engagement_label="engaged",
        )
        report = self._report(host)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["rows_total"], 2)
        self.assertEqual(report["rows_unsettled"], 1)
        self.assertEqual(report["pending_message_id"], 102)
        row = report["leaderboard"][0]
        self.assertEqual(row["item_id"], 1)
        self.assertEqual(row["settled"], 1)
        self.assertEqual(row["engaged"], 1)
        self.assertIn("settled", report["reading_guide"])

    def test_a_disabled_ledger_says_so_rather_than_reporting_zeroes(
        self,
    ) -> None:
        host = _Host(self.f.store)
        host._surfacing_outcome_store = None
        report = self._report(host)
        self.assertFalse(report["enabled"])
        self.assertIn("hint", report)

    def test_an_empty_ledger_explains_itself(self) -> None:
        report = self._report(_Host(self.f.store))
        self.assertEqual(report["rows_total"], 0)
        self.assertIn("No rows yet", report["hint"])

    def test_an_all_unsettled_ledger_explains_the_empty_board(self) -> None:
        host = _Host(self.f.store)
        host.turn(
            assistant_message_id=101, surfaced=_concepts(1),
            engagement_label=None,
        )
        report = self._report(host)
        self.assertEqual(report["leaderboard"], [])
        self.assertIn("unsettled by design", report["hint"])


if __name__ == "__main__":
    unittest.main()

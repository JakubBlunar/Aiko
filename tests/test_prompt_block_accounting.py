"""K90: per-turn prompt-block accounting.

Two things have to hold or every rate this feeds is quietly wrong: the
denominator has to be *turns*, not rows (a turn that fired forty blocks
is still one turn), and the blocks recorded have to belong to the reply
they are filed against, not to the previous assembly.
"""
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.memory.turn_prompt_block_store import TurnPromptBlockStore
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.db_path)
        self.store = TurnPromptBlockStore(self.db)

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

    def backdate(self, block: str, days: int) -> None:
        stamp = (timephrase.utcnow() - timedelta(days=days)).isoformat()
        conn = self.db._get_conn()
        conn.execute(
            "UPDATE turn_prompt_blocks SET created_at = ? WHERE block = ?",
            (stamp, str(block)),
        )
        conn.commit()


class _Host(PostTurnHelpersMixin):
    """Minimal controller stand-in exposing just the recorder's inputs."""

    def __init__(self, store) -> None:
        self._turn_prompt_block_store = store


class StoreWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_only_rendered_blocks_are_stored(self) -> None:
        written = self.fx.store.add_turn(
            1, {"persona": 900, "taste_lean_block": 0, "wants_block": 40},
        )
        self.assertEqual(written, 2)
        blocks = {r["block"] for r in self.fx.store.firing_rates()}
        self.assertEqual(blocks, {"persona", "wants_block"})

    def test_an_all_empty_assembly_writes_nothing(self) -> None:
        self.assertEqual(self.fx.store.add_turn(1, {"persona": 0}), 0)
        self.assertEqual(self.fx.store.count(), 0)

    def test_a_missing_message_id_is_refused(self) -> None:
        self.assertEqual(self.fx.store.add_turn(0, {"persona": 10}), 0)
        self.assertEqual(self.fx.store.add_turn(-3, {"persona": 10}), 0)

    def test_an_empty_table_is_refused(self) -> None:
        self.assertEqual(self.fx.store.add_turn(1, {}), 0)


class FiringRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_the_denominator_is_turns_not_rows(self) -> None:
        # Turn 1 fires forty blocks, turn 2 fires one. A rate computed
        # over rows would say the persona fired on 2 of 41 "turns".
        self.fx.store.add_turn(1, {f"block_{i}": 10 for i in range(40)})
        self.fx.store.add_turn(1, {"persona": 900})
        self.fx.store.add_turn(2, {"persona": 900})
        self.assertEqual(self.fx.store.turns_recorded(), 2)
        rates = {r["block"]: r for r in self.fx.store.firing_rates()}
        self.assertEqual(rates["persona"]["rate"], 1.0)
        self.assertEqual(rates["block_0"]["rate"], 0.5)

    def test_a_block_recorded_twice_on_one_turn_counts_once(self) -> None:
        self.fx.store.add_turn(1, {"persona": 900})
        self.fx.store.add_turn(1, {"persona": 900})
        rates = {r["block"]: r for r in self.fx.store.firing_rates()}
        self.assertEqual(rates["persona"]["fired"], 1)
        self.assertEqual(rates["persona"]["rate"], 1.0)

    def test_average_size_comes_back_with_the_rate(self) -> None:
        self.fx.store.add_turn(1, {"wants_block": 100})
        self.fx.store.add_turn(2, {"wants_block": 200})
        rates = {r["block"]: r for r in self.fx.store.firing_rates()}
        self.assertEqual(rates["wants_block"]["avg_chars"], 150.0)

    def test_the_window_excludes_old_rows_from_both_sides(self) -> None:
        self.fx.store.add_turn(1, {"stale_block": 10})
        self.fx.backdate("stale_block", 30)
        self.fx.store.add_turn(2, {"fresh_block": 10})
        rates = {
            r["block"]: r for r in self.fx.store.firing_rates(window_days=7)
        }
        self.assertNotIn("stale_block", rates)
        # The stale turn left the denominator too, so the fresh block
        # reads 1.0 rather than 0.5.
        self.assertEqual(rates["fresh_block"]["rate"], 1.0)

    def test_an_empty_table_reports_no_rates_and_no_crash(self) -> None:
        self.assertEqual(self.fx.store.firing_rates(), [])
        self.assertEqual(self.fx.store.turns_recorded(), 0)


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_prune_drops_rows_past_the_horizon(self) -> None:
        self.fx.store.add_turn(1, {"old_block": 10})
        self.fx.backdate("old_block", 90)
        self.fx.store.add_turn(2, {"new_block": 10})
        self.assertEqual(self.fx.store.prune(30), 1)
        self.assertEqual(self.fx.store.count(), 1)

    def test_a_non_positive_horizon_is_a_noop(self) -> None:
        self.fx.store.add_turn(1, {"block": 10})
        self.assertEqual(self.fx.store.prune(0), 0)
        self.assertEqual(self.fx.store.count(), 1)


class RecorderTests(unittest.TestCase):
    """The post-turn seam."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_it_records_this_turns_telemetry(self) -> None:
        host = _Host(self.fx.store)
        host._record_prompt_blocks(
            assistant_message_id=7,
            telemetry=SimpleNamespace(
                block_chars={"persona": 900, "wants_block": 0},
            ),
        )
        rates = {r["block"]: r for r in self.fx.store.firing_rates()}
        self.assertEqual(set(rates), {"persona"})

    def test_a_turn_with_no_assembly_is_not_counted(self) -> None:
        # Banter and aborted turns build no prompt. Recording them would
        # put a turn in the denominator that never had blocks to fire,
        # dragging every rate down.
        host = _Host(self.fx.store)
        host._record_prompt_blocks(
            assistant_message_id=7,
            telemetry=SimpleNamespace(block_chars={}),
        )
        self.assertEqual(self.fx.store.turns_recorded(), 0)

    def test_a_missing_telemetry_object_is_survivable(self) -> None:
        host = _Host(self.fx.store)
        host._record_prompt_blocks(assistant_message_id=7, telemetry=None)
        self.assertEqual(self.fx.store.count(), 0)

    def test_no_store_is_a_noop_not_a_crash(self) -> None:
        host = _Host(None)
        host._record_prompt_blocks(
            assistant_message_id=7,
            telemetry=SimpleNamespace(block_chars={"persona": 900}),
        )

    def test_an_unsaved_reply_is_skipped(self) -> None:
        host = _Host(self.fx.store)
        host._record_prompt_blocks(
            assistant_message_id=None,
            telemetry=SimpleNamespace(block_chars={"persona": 900}),
        )
        self.assertEqual(self.fx.store.count(), 0)


class SchemaTests(unittest.TestCase):
    def test_v35_lands_on_a_database_without_the_table(self) -> None:
        tmp = TemporaryDirectory()
        try:
            path = Path(tmp.name) / "legacy.db"
            db = ChatDatabase(path)
            conn = db._get_conn()
            conn.execute("DROP TABLE IF EXISTS turn_prompt_blocks")
            conn.execute("UPDATE schema_version SET version = 34")
            conn.commit()
            conn.close()
            db._local.conn = None

            db2 = ChatDatabase(path)
            store = TurnPromptBlockStore(db2)
            self.assertEqual(store.add_turn(1, {"persona": 900}), 1)
            self.assertEqual(store.count(), 1)
            conn2 = db2._get_conn()
            version = conn2.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            # The symbol, not the literal: this test is about a legacy
            # database gaining the table, and it should keep passing
            # every time the schema moves past the version that added it.
            self.assertEqual(int(version), _SCHEMA_VERSION)
            conn2.close()
            db2._local.conn = None
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass


if __name__ == "__main__":
    unittest.main()

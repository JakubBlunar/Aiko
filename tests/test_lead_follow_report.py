"""K90: the offline report over the message log.

The pairing logic is the part worth pinning: an assistant reply has to
be scored against the user turn it actually answered, and sessions must
not bleed into one another. Getting that wrong produces a report that
looks entirely reasonable and measures the wrong pairs.
"""
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.persona.lead_follow_corpus import (  # noqa: E402
    block_firing,
    collect,
    load_turns,
)
from scripts.lead_follow_report import _render  # noqa: E402


def _build(rows) -> sqlite3.Connection:
    """In-memory ``messages`` table shaped like the real one."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


class PairingTests(unittest.TestCase):
    def test_a_reply_is_paired_with_the_user_turn_before_it(self):
        conn = _build([
            (1, "s1", "user", "how was the deployment", _iso(1)),
            (2, "s1", "assistant", "It went fine, all told.", _iso(1)),
        ])
        turns = load_turns(conn)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["user_text"], "how was the deployment")

    def test_sessions_do_not_bleed_into_each_other(self):
        conn = _build([
            (1, "s1", "user", "how was the deployment", _iso(2)),
            (2, "s2", "assistant", "The lettuce recovered nicely.", _iso(1)),
        ])
        turns = load_turns(conn)
        self.assertEqual(turns[0]["user_text"], "")
        self.assertEqual(turns[0]["history"], [])

    def test_a_proactive_reply_with_no_prompt_is_kept(self):
        # She led by definition; dropping these would bias the corpus
        # against the behaviour being measured.
        conn = _build([
            (1, "s1", "assistant", "I finished the paperback today.", _iso(1)),
        ])
        turns = load_turns(conn)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["user_text"], "")

    def test_consecutive_replies_do_not_reuse_the_same_prompt(self):
        conn = _build([
            (1, "s1", "user", "how was the deployment", _iso(1)),
            (2, "s1", "assistant", "It went fine, all told.", _iso(1)),
            (3, "s1", "assistant", "Also the lettuce recovered.", _iso(1)),
        ])
        turns = load_turns(conn)
        self.assertEqual(turns[0]["user_text"], "how was the deployment")
        self.assertEqual(turns[1]["user_text"], "")

    def test_the_paired_user_turn_is_not_also_counted_as_history(self):
        conn = _build([
            (1, "s1", "user", "the lettuce looked rough", _iso(1)),
            (2, "s1", "assistant", "It did, yes.", _iso(1)),
            (3, "s1", "user", "how was the deployment", _iso(1)),
            (4, "s1", "assistant", "Fine, all told.", _iso(1)),
        ])
        turns = load_turns(conn)
        self.assertNotIn("how was the deployment", turns[1]["history"])
        self.assertIn("the lettuce looked rough", turns[1]["history"])

    def test_one_word_reactions_are_left_out(self):
        conn = _build([
            (1, "s1", "user", "how was the deployment", _iso(1)),
            (2, "s1", "assistant", "Yeah.", _iso(1)),
        ])
        self.assertEqual(load_turns(conn), [])

    def test_system_rows_are_ignored(self):
        conn = _build([
            (1, "s1", "system", "you are a companion", _iso(1)),
            (2, "s1", "user", "how was the deployment", _iso(1)),
            (3, "s1", "assistant", "It went fine, all told.", _iso(1)),
        ])
        turns = load_turns(conn)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["user_text"], "how was the deployment")


class CollectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _build([
            (1, "s1", "user", "how was the deployment", _iso(40)),
            (2, "s1", "assistant", "That makes sense to me.", _iso(40)),
            (3, "s1", "user", "and the garden", _iso(1)),
            (4, "s1", "assistant", "The lettuce recovered nicely.", _iso(1)),
        ])

    def test_windows_slice_the_corpus(self):
        data = collect(self.conn, now=_now(), windows=(7.0, None))
        recent, everything = data["cohorts"]
        self.assertEqual(recent["turns"], 1)
        self.assertEqual(everything["turns"], 2)
        self.assertEqual(data["total_assistant_turns"], 2)

    def test_the_anaphoric_rate_reaches_the_report(self):
        data = collect(self.conn, now=_now(), windows=(None,))
        self.assertEqual(data["cohorts"][0]["anaphoric_opener_rate"], 0.5)

    def test_render_produces_a_readable_table(self):
        text = _render(collect(self.conn, now=_now(), windows=(None,)))
        self.assertIn("Lead/follow report", text)
        self.assertIn("all time", text)
        self.assertIn("anaph", text)


class BlockFiringTests(unittest.TestCase):
    def test_a_database_without_the_table_says_so(self):
        conn = _build([])
        result = block_firing(conn, _now(), None)
        self.assertFalse(result["available"])
        self.assertIn("schema v35", result["reason"])

    def test_an_empty_table_explains_that_it_is_not_retroactive(self):
        conn = _build([])
        conn.execute(
            "CREATE TABLE turn_prompt_blocks (id INTEGER PRIMARY KEY, "
            "assistant_message_id INTEGER, block TEXT, chars INTEGER, "
            "created_at TEXT)"
        )
        result = block_firing(conn, _now(), None)
        self.assertFalse(result["available"])
        self.assertIn("not", result["reason"])

    def test_rates_are_per_turn_not_per_row(self):
        conn = _build([])
        conn.execute(
            "CREATE TABLE turn_prompt_blocks (id INTEGER PRIMARY KEY, "
            "assistant_message_id INTEGER, block TEXT, chars INTEGER, "
            "created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO turn_prompt_blocks "
            "(assistant_message_id, block, chars, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (1, "persona", 900, _iso(1)),
                (1, "wants_block", 40, _iso(1)),
                (2, "persona", 900, _iso(1)),
            ],
        )
        conn.commit()
        result = block_firing(conn, _now(), None)
        self.assertTrue(result["available"])
        self.assertEqual(result["turns"], 2)
        rates = {b["block"]: b for b in result["blocks"]}
        self.assertEqual(rates["persona"]["per_hundred_turns"], 100.0)
        self.assertEqual(rates["wants_block"]["per_hundred_turns"], 50.0)


class SmokeTests(unittest.TestCase):
    """Against a real ``ChatDatabase``, whose connection has no row factory.

    The CLI sets one and the endpoint cannot -- it is handed the shared
    connection every other store uses -- so the corpus queries have to
    work either way.
    """

    def test_it_runs_over_a_connection_with_no_row_factory(self):
        from app.core.infra.chat_database import ChatDatabase

        tmp = TemporaryDirectory()
        try:
            db = ChatDatabase(Path(tmp.name) / "chat.db")
            conn = db._get_conn()
            self.assertIsNone(conn.row_factory)
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    ("s1", "user", "how was the deployment", _iso(1)),
                    ("s1", "assistant", "That makes sense to me.", _iso(1)),
                ],
            )
            conn.commit()

            data = collect(conn, now=_now(), windows=(None,))
            self.assertEqual(data["total_assistant_turns"], 1)
            self.assertEqual(data["cohorts"][0]["anaphoric_opener_rate"], 1.0)
            conn.close()
            db._local.conn = None
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass

    def test_it_runs_against_a_real_empty_database(self):
        from app.core.infra.chat_database import ChatDatabase

        tmp = TemporaryDirectory()
        try:
            path = Path(tmp.name) / "chat.db"
            db = ChatDatabase(path)
            conn = db._get_conn()
            conn.close()
            db._local.conn = None

            ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            ro.row_factory = sqlite3.Row
            try:
                data = collect(ro, now=_now())
                self.assertEqual(data["total_assistant_turns"], 0)
                self.assertIn("Lead/follow report", _render(data))
            finally:
                ro.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass


if __name__ == "__main__":
    unittest.main()

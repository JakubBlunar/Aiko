"""Schema v13 migration: messages.arc + messages.dialogue_act.

Covers:
  * Fresh DBs include both columns and roundtrip values via ``add_message``.
  * Pre-v13 DBs upgrade in place: existing rows keep ``NULL`` for the new
    columns; new rows can populate them; the schema_version row bumps to 13.
  * ``update_message_arc`` and ``update_message_dialogue_act`` patch
    individual rows without touching ``content`` / ``token_count``.
  * ``get_message_signals`` returns ``{id: (arc, dialogue_act)}`` for the
    H1+K4 RAG boost.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.db_path)

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


class FreshDbTests(unittest.TestCase):
    def test_schema_version_at_v13(self) -> None:
        f = _Fixture()
        try:
            row = f.db._get_conn().execute(
                "SELECT version FROM schema_version LIMIT 1",
            ).fetchone()
            self.assertEqual(int(row[0]), _SCHEMA_VERSION)
            self.assertGreaterEqual(_SCHEMA_VERSION, 13)
        finally:
            f.close()

    def test_messages_table_has_new_columns(self) -> None:
        f = _Fixture()
        try:
            cols = {
                row[1]
                for row in f.db._get_conn().execute(
                    "PRAGMA table_info(messages)"
                )
            }
            self.assertIn("arc", cols)
            self.assertIn("dialogue_act", cols)
        finally:
            f.close()

    def test_add_message_with_signals_roundtrips(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message(
                session_id="s1",
                role="user",
                content="i feel exhausted",
                arc="support",
                dialogue_act="vent",
            )
            rows = f.db.get_messages("s1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, mid)
            self.assertEqual(rows[0].arc, "support")
            self.assertEqual(rows[0].dialogue_act, "vent")
        finally:
            f.close()

    def test_add_message_defaults_to_null_signals(self) -> None:
        f = _Fixture()
        try:
            f.db.add_message(session_id="s1", role="user", content="hello")
            rows = f.db.get_messages("s1")
            self.assertIsNone(rows[0].arc)
            self.assertIsNone(rows[0].dialogue_act)
        finally:
            f.close()

    def test_update_helpers_patch_in_place(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message(
                session_id="s1", role="user", content="some text",
            )
            self.assertTrue(f.db.update_message_arc(mid, "playful"))
            self.assertTrue(
                f.db.update_message_dialogue_act(mid, "banter"),
            )
            rows = f.db.get_messages("s1")
            self.assertEqual(rows[0].arc, "playful")
            self.assertEqual(rows[0].dialogue_act, "banter")
            # Setting ``None`` clears the column.
            self.assertTrue(f.db.update_message_arc(mid, None))
            rows = f.db.get_messages("s1")
            self.assertIsNone(rows[0].arc)
        finally:
            f.close()

    def test_update_helpers_reject_invalid_id(self) -> None:
        f = _Fixture()
        try:
            self.assertFalse(f.db.update_message_arc(0, "support"))
            self.assertFalse(f.db.update_message_arc(-1, "support"))
            self.assertFalse(f.db.update_message_dialogue_act(0, "vent"))
        finally:
            f.close()

    def test_get_message_signals_batches_lookup(self) -> None:
        f = _Fixture()
        try:
            mid_a = f.db.add_message(
                session_id="s1", role="user", content="a",
                arc="support", dialogue_act="vent",
            )
            mid_b = f.db.add_message(
                session_id="s1", role="user", content="b",
                arc="silly", dialogue_act="banter",
            )
            mid_c = f.db.add_message(session_id="s1", role="user", content="c")
            signals = f.db.get_message_signals([mid_a, mid_b, mid_c, 9999])
            self.assertEqual(signals[mid_a], ("support", "vent"))
            self.assertEqual(signals[mid_b], ("silly", "banter"))
            self.assertEqual(signals[mid_c], (None, None))
            self.assertNotIn(9999, signals)
        finally:
            f.close()

    def test_get_message_signals_handles_empty_input(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(f.db.get_message_signals([]), {})
            self.assertEqual(f.db.get_message_signals([0, -3]), {})
        finally:
            f.close()


class UpgradeInPlaceTests(unittest.TestCase):
    """Simulate a legacy v12 DB by stripping the new columns / forcing
    ``schema_version`` back, then re-opening it via ``ChatDatabase`` to
    confirm the migration ladder restores them.
    """

    def test_v12_upgrade_to_v13_is_noop_safe_and_adds_columns(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message(
                session_id="s1", role="user", content="legacy row",
            )
            conn = f.db._get_conn()

            # Force a downgrade of the schema_version row + drop the
            # new columns so the migration ladder has work to do on
            # the next open.
            conn.execute("UPDATE schema_version SET version = 12")
            for col in ("arc", "dialogue_act"):
                try:
                    conn.execute(f"ALTER TABLE messages DROP COLUMN {col}")
                except sqlite3.OperationalError:
                    # Older SQLite that doesn't support DROP COLUMN --
                    # rebuild the table without the columns instead.
                    conn.executescript(
                        """
                        CREATE TABLE messages_legacy (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL,
                            role TEXT NOT NULL,
                            content TEXT NOT NULL,
                            token_count INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL
                        );
                        INSERT INTO messages_legacy
                            (id, session_id, role, content, token_count, created_at)
                            SELECT id, session_id, role, content, token_count, created_at
                            FROM messages;
                        DROP TABLE messages;
                        ALTER TABLE messages_legacy RENAME TO messages;
                        """
                    )
                    break
            conn.commit()
            conn.close()
            f.db._local.conn = None

            # Re-open: the migration ladder should add the columns and
            # bump the version back to v13.
            f.db = ChatDatabase(f.db_path)
            cols = {
                row[1]
                for row in f.db._get_conn().execute(
                    "PRAGMA table_info(messages)"
                )
            }
            self.assertIn("arc", cols)
            self.assertIn("dialogue_act", cols)

            row = f.db._get_conn().execute(
                "SELECT version FROM schema_version LIMIT 1",
            ).fetchone()
            self.assertEqual(int(row[0]), _SCHEMA_VERSION)

            # Existing rows stay NULL on the new columns.
            rows = f.db.get_messages("s1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, mid)
            self.assertIsNone(rows[0].arc)
            self.assertIsNone(rows[0].dialogue_act)

            # New rows can populate the new columns.
            mid2 = f.db.add_message(
                session_id="s1",
                role="user",
                content="post-migration row",
                arc="planning",
                dialogue_act="planning",
            )
            rows = f.db.get_messages("s1")
            second = next(r for r in rows if r.id == mid2)
            self.assertEqual(second.arc, "planning")
            self.assertEqual(second.dialogue_act, "planning")
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()

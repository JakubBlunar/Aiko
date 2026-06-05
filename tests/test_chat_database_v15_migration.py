"""Schema v15 migration: ``messages.gestures`` + ``messages.reactions``.

K31 (touch gestures) needs the assistant bubble to remember which
gesture kinds Aiko fired during the turn; K32 (user reactions) needs
the same bubble to remember the per-kind reaction counter map. Both
land as nullable JSON TEXT columns so the migration is additive and
the value is opaque to SQLite.

Covers:

  - Fresh DBs include both columns at the current schema version.
  - Pre-v15 DBs migrate cleanly: existing rows keep ``NULL`` for the
    new columns; new rows can populate them via the helpers; the
    ``schema_version`` row bumps to 15+.
  - ``update_message_gestures`` and ``update_message_reactions`` write
    individual rows without touching ``content`` / ``token_count``.
  - ``get_messages`` returns the new columns on every row.
  - ``get_message_row`` returns the single-row variant including the
    new columns.

All in-memory + tempfile DBs; no I/O outside the test fixture. Runs
in <50ms.
"""
from __future__ import annotations

import json
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
    def test_schema_version_at_v15_or_above(self) -> None:
        f = _Fixture()
        try:
            row = f.db._get_conn().execute(
                "SELECT version FROM schema_version LIMIT 1",
            ).fetchone()
            self.assertEqual(int(row[0]), _SCHEMA_VERSION)
            self.assertGreaterEqual(_SCHEMA_VERSION, 15)
        finally:
            f.close()

    def test_messages_has_gestures_and_reactions_columns(self) -> None:
        f = _Fixture()
        try:
            cols = {
                row[1]
                for row in f.db._get_conn().execute(
                    "PRAGMA table_info(messages)"
                )
            }
            self.assertIn("gestures", cols)
            self.assertIn("reactions", cols)
        finally:
            f.close()

    def test_new_rows_default_to_null(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message("s1", "assistant", "hi")
            rows = f.db.get_messages("s1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, mid)
            self.assertIsNone(rows[0].gestures)
            self.assertIsNone(rows[0].reactions)
        finally:
            f.close()


class UpdateHelpersTests(unittest.TestCase):
    def test_update_message_gestures_persists_json(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message("s1", "assistant", "hello")
            payload = json.dumps(["hug", "wave"])
            f.db.update_message_gestures(mid, payload)
            row = f.db.get_message_row(mid)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.gestures, payload)
            # Content stays untouched.
            self.assertEqual(row.content, "hello")
        finally:
            f.close()

    def test_update_message_reactions_persists_json(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message("s1", "assistant", "hello")
            payload = json.dumps({"heart": 2, "laugh": 1})
            f.db.update_message_reactions(mid, payload)
            row = f.db.get_message_row(mid)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.reactions, payload)
        finally:
            f.close()

    def test_update_message_reactions_with_null_clears(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message("s1", "assistant", "hello")
            f.db.update_message_reactions(mid, json.dumps({"heart": 1}))
            f.db.update_message_reactions(mid, None)
            row = f.db.get_message_row(mid)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertIsNone(row.reactions)
        finally:
            f.close()

    def test_get_messages_returns_new_columns(self) -> None:
        f = _Fixture()
        try:
            mid1 = f.db.add_message("s1", "user", "hello")
            mid2 = f.db.add_message("s1", "assistant", "hi back")
            f.db.update_message_gestures(mid2, json.dumps(["wave"]))
            f.db.update_message_reactions(mid2, json.dumps({"heart": 1}))
            rows = f.db.get_messages("s1")
            by_id = {r.id: r for r in rows}
            self.assertIsNone(by_id[mid1].gestures)
            self.assertIsNone(by_id[mid1].reactions)
            self.assertEqual(by_id[mid2].gestures, json.dumps(["wave"]))
            self.assertEqual(by_id[mid2].reactions, json.dumps({"heart": 1}))
        finally:
            f.close()


class V14MigrationTests(unittest.TestCase):
    """Force a downgrade of the schema, drop the new columns, then
    reopen via :class:`ChatDatabase` to confirm the migration ladder
    restores them and bumps ``schema_version`` back to current."""

    def test_v14_upgrade_to_v15_adds_columns(self) -> None:
        f = _Fixture()
        try:
            mid = f.db.add_message("s1", "user", "legacy row")
            conn = f.db._get_conn()
            conn.execute("UPDATE schema_version SET version = 14")
            for col in ("gestures", "reactions"):
                try:
                    conn.execute(f"ALTER TABLE messages DROP COLUMN {col}")
                except sqlite3.OperationalError:
                    # Older SQLite that doesn't support DROP COLUMN
                    # -- rebuild the table without the columns. We
                    # only rebuild once (the second iteration would
                    # find the column already missing).
                    cols = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(messages)")
                    }
                    keep_cols = [c for c in cols if c not in ("gestures", "reactions")]
                    col_list = ", ".join(keep_cols)
                    conn.executescript(
                        f"""
                        CREATE TABLE messages_legacy AS
                            SELECT {col_list} FROM messages;
                        DROP TABLE messages;
                        ALTER TABLE messages_legacy RENAME TO messages;
                        """
                    )
                    break
            conn.commit()
            conn.close()
            f.db._local.conn = None

            f.db = ChatDatabase(f.db_path)
            cols = {
                row[1]
                for row in f.db._get_conn().execute(
                    "PRAGMA table_info(messages)"
                )
            }
            self.assertIn("gestures", cols)
            self.assertIn("reactions", cols)

            row = f.db._get_conn().execute(
                "SELECT version FROM schema_version LIMIT 1",
            ).fetchone()
            self.assertEqual(int(row[0]), _SCHEMA_VERSION)

            # Legacy row keeps NULL on the new columns.
            rows = f.db.get_messages("s1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, mid)
            self.assertIsNone(rows[0].gestures)
            self.assertIsNone(rows[0].reactions)

            # New helpers work post-migration.
            f.db.update_message_gestures(mid, json.dumps(["hug"]))
            f.db.update_message_reactions(mid, json.dumps({"heart": 3}))
            after = f.db.get_message_row(mid)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertEqual(after.gestures, json.dumps(["hug"]))
            self.assertEqual(after.reactions, json.dumps({"heart": 3}))
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()

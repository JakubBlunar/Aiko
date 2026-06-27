"""K-time2 follow-up: direct ``[start, end]`` message recall.

When a query names a clearly retrospective window ("what did we say
yesterday?"), the semantic top-N can miss the actual lines from that day.
:meth:`ChatDatabase.messages_in_range` is the verbatim fallback and
:class:`RagRetriever` injects its rows as ``message`` hits for *guardable*
windows only.

Two surfaces under test:

* ``ChatDatabase.messages_in_range`` — inclusive range scan, newest-first,
  capped, optional current-session exclusion.
* ``RagRetriever.retrieve`` — injects in-window messages for a guardable
  query, stays silent on chit-chat ("today") and when disabled, and the
  injected lines satisfy the empty-window guard.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import RagHit

# Wednesday, 2026-06-17 12:00 UTC.
NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class MessagesInRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = ChatDatabase(Path(self._tmp.name) / "chat.db")
        # Three messages: two yesterday, one today.
        rows = [
            ("u:s1", "user", "yesterday morning note", _iso(NOW - timedelta(days=1, hours=3))),
            ("u:s1", "assistant", "yesterday evening reply", _iso(NOW - timedelta(days=1) + timedelta(hours=7))),
            ("u:s2", "user", "today note", _iso(NOW - timedelta(hours=2))),
        ]
        for sid, role, content, created in rows:
            self.db.execute_commit(
                "INSERT INTO messages (session_id, role, content, token_count, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, role, content, 0, created),
            )

    def tearDown(self) -> None:
        # Close the thread-local SQLite connection before removing the temp
        # dir; WAL mode otherwise keeps the file open and Windows refuses
        # the rmtree.
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_returns_only_in_window_rows_newest_first(self) -> None:
        start = _iso((NOW - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        end = _iso((NOW - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=999999))
        rows = self.db.messages_in_range(start, end, limit=10)
        contents = [r["content"] for r in rows]
        self.assertEqual(
            contents, ["yesterday evening reply", "yesterday morning note"],
        )
        # Shape is MessageRecord.from_row-friendly.
        self.assertEqual(rows[0]["id"], f"{rows[0]['session_id']}:{rows[0]['message_id']}")

    def test_limit_caps_results(self) -> None:
        start = _iso(NOW - timedelta(days=3))
        end = _iso(NOW)
        rows = self.db.messages_in_range(start, end, limit=1)
        self.assertEqual(len(rows), 1)
        # Newest of the three is "today note".
        self.assertEqual(rows[0]["content"], "today note")

    def test_exclude_session_drops_current(self) -> None:
        start = _iso(NOW - timedelta(days=3))
        end = _iso(NOW)
        rows = self.db.messages_in_range(start, end, limit=10, exclude_session_id="u:s2")
        self.assertNotIn("today note", [r["content"] for r in rows])

    def test_zero_limit_is_empty(self) -> None:
        self.assertEqual(self.db.messages_in_range(_iso(NOW), _iso(NOW), limit=0), [])


class _StubStore:
    def search_memories(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_messages(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []

    def search_documents(self, *_a: Any, **_k: Any) -> list[RagHit]:
        return []


class _StubEmbedder:
    def embed(self, _text: str) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


class _FakeChatDb:
    """Minimal chat_db surface: a fixed in-window row + a no-op signal join."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, str, int, str | None]] = []

    def messages_in_range(
        self, start_iso: str, end_iso: str, *, limit: int = 8,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((start_iso, end_iso, limit, exclude_session_id))
        return list(self._rows[:limit])

    def get_message_signals(self, _ids: Any) -> dict[int, Any]:
        return {}


def _row(content: str, created: datetime, *, sid: str = "u:old", mid: int = 1) -> dict[str, Any]:
    return {
        "id": f"{sid}:{mid}",
        "session_id": sid,
        "message_id": mid,
        "role": "user",
        "content": content,
        "created_at": _iso(created),
    }


def _build(*, enabled: bool = True, rows: list[dict[str, Any]] | None = None) -> tuple[RagRetriever, _FakeChatDb]:
    if rows is None:
        rows = [_row("we talked about the dashboard rollout", NOW - timedelta(days=1))]
    chat_db = _FakeChatDb(rows)
    r = RagRetriever(
        _StubStore(),  # type: ignore[arg-type]
        _StubEmbedder(),  # type: ignore[arg-type]
        top_k=10,
        score_threshold=-5.0,
        include_messages=True,
        include_documents=False,
        chat_db=chat_db,  # type: ignore[arg-type]
        direct_recall_enabled=enabled,
        direct_recall_max_messages=6,
    )
    return r, chat_db


class DirectRecallIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        timephrase.set_now_provider(lambda: NOW)

    def tearDown(self) -> None:
        timephrase.set_now_provider(None)

    def test_guardable_query_injects_in_window_messages(self) -> None:
        r, chat_db = _build()
        hits = r.retrieve("what did we say yesterday?")
        self.assertEqual(len(chat_db.calls), 1)
        texts = [h.text for h in hits]
        self.assertIn("we talked about the dashboard rollout", texts)

    def test_non_guardable_today_does_not_inject(self) -> None:
        r, chat_db = _build()
        hits = r.retrieve("how are you today?")
        self.assertEqual(chat_db.calls, [])
        self.assertEqual(hits, [])

    def test_disabled_flag_skips_injection(self) -> None:
        r, chat_db = _build(enabled=False)
        hits = r.retrieve("what did we say yesterday?")
        self.assertEqual(chat_db.calls, [])
        self.assertEqual(hits, [])

    def test_out_of_window_rows_filtered_out(self) -> None:
        # The fake returns a row dated *today* even though the query asks
        # about yesterday (simulating the widened-bounds margin). The
        # precise contains() re-filter must drop it.
        r, _ = _build(rows=[_row("today only line", NOW - timedelta(hours=1))])
        hits = r.retrieve("what did we say yesterday?")
        self.assertEqual(hits, [])

    def test_direct_recall_satisfies_empty_window_guard(self) -> None:
        # An in-window direct hit means the retrospective guard must NOT
        # fire (we *do* have something from yesterday).
        r, _ = _build()
        block = r.block_for("what did we say yesterday?")
        self.assertNotIn("instead of guessing", block)


if __name__ == "__main__":
    unittest.main()

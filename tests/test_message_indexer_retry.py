"""Tests for :class:`app.core.rag.message_indexer.MessageIndexer` retry path.

The old indexer logged an embed / write failure at DEBUG and dropped the
message permanently — a transient Ollama hiccup silently rotted RAG
recall. These tests exercise the bounded-retry hardening (I2):

  * a transient embed failure schedules a retry that re-enqueues the row
    with an incremented attempt count, and the next attempt indexes it;
  * exhausting the attempt budget logs at WARNING (so the rot is visible)
    and schedules no further retries;
  * a clean success indexes immediately with no retry timer;
  * ``stop()`` cancels any pending retry timers.

The tests drive ``_index_one`` directly (no worker thread) and fire the
scheduled timer's callback manually so they stay deterministic and don't
wait out the real backoff.
"""
from __future__ import annotations

import unittest

from app.core.infra.chat_database import MessageRow
from app.core.rag.message_indexer import MessageIndexer


def _row(msg_id: int = 1, content: str = "a reasonably long message body") -> MessageRow:
    return MessageRow(
        id=msg_id,
        session_id="sess-1",
        role="user",
        content=content,
        token_count=0,
        created_at="2026-06-09T12:00:00+00:00",
    )


class _FakeDb:
    def add_message_listener(self, _cb) -> None:
        pass

    def remove_message_listener(self, _cb) -> None:
        pass


class _FakeRag:
    def __init__(self, *, fail_writes: int = 0) -> None:
        self.added: list[int] = []
        self._fail_writes = fail_writes

    def has_message(self, _session_id: str, _message_id: int) -> bool:
        return False

    def add_message(self, *, message_id: int, **_kw) -> None:
        if self._fail_writes > 0:
            self._fail_writes -= 1
            raise RuntimeError("write boom")
        self.added.append(message_id)


class _FakeEmbedder:
    def __init__(self, *, fail_times: int = 0) -> None:
        self._fail_times = fail_times
        self.calls = 0

    def embed(self, _text: str):
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("embed boom")
        return [0.0, 1.0, 0.0]


def _make(rag: _FakeRag, embedder: _FakeEmbedder) -> MessageIndexer:
    return MessageIndexer(_FakeDb(), rag, embedder)


class RetrySchedulingTests(unittest.TestCase):
    def test_embed_failure_schedules_retry_then_indexes(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder(fail_times=1)
        indexer = _make(rag, embedder)

        # First attempt: embed raises -> a retry timer is armed, nothing
        # indexed yet.
        indexer._index_one(_row(), attempt=0)
        self.assertEqual(len(indexer._retry_timers), 1)
        self.assertEqual(rag.added, [])

        # Fire the timer's callback manually (skip the real 2 s backoff)
        # and confirm it re-enqueued the row with attempt=1.
        timer = next(iter(indexer._retry_timers))
        timer.cancel()
        timer.function()
        self.assertEqual(indexer._retry_timers, set())
        queued = indexer._queue.get_nowait()
        self.assertEqual(queued[0].id, 1)
        self.assertEqual(queued[1], 1)

        # The retried attempt succeeds (embedder no longer fails).
        indexer._index_one(queued[0], attempt=queued[1])
        self.assertEqual(rag.added, [1])
        self.assertEqual(indexer._retry_timers, set())
        indexer.stop()

    def test_write_failure_also_retries(self) -> None:
        rag = _FakeRag(fail_writes=1)
        embedder = _FakeEmbedder()
        indexer = _make(rag, embedder)

        indexer._index_one(_row(), attempt=0)
        self.assertEqual(len(indexer._retry_timers), 1)
        self.assertEqual(rag.added, [])
        indexer.stop()


class RetryExhaustionTests(unittest.TestCase):
    def test_exhausting_attempts_warns_and_stops(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder(fail_times=99)
        indexer = _make(rag, embedder)

        # attempt=2 -> next_attempt=3 == _MAX_INDEX_ATTEMPTS -> give up.
        with self.assertLogs("app.message_indexer", level="WARNING") as cm:
            indexer._index_one(_row(7), attempt=2)
        self.assertTrue(any("gave up on msg 7" in line for line in cm.output))
        self.assertEqual(indexer._retry_timers, set())
        with self.assertRaises(Exception):
            indexer._queue.get_nowait()
        indexer.stop()


class StatsTests(unittest.TestCase):
    """P6: lifecycle counters + queue/thread visibility via ``stats()``."""

    def test_clean_success_bumps_indexed(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder()
        indexer = _make(rag, embedder)
        indexer._index_one(_row(3), attempt=0)
        stats = indexer.stats()
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["embed_failures"], 0)
        self.assertEqual(stats["gave_up"], 0)
        self.assertEqual(stats["queue_depth"], 0)
        self.assertEqual(stats["pending_retries"], 0)
        self.assertIsNone(stats["last_give_up"])
        self.assertIsNotNone(stats["last_index_age_seconds"])
        indexer.stop()

    def test_embed_failure_counts_failure_and_retry(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder(fail_times=1)
        indexer = _make(rag, embedder)
        indexer._index_one(_row(), attempt=0)
        stats = indexer.stats()
        self.assertEqual(stats["embed_failures"], 1)
        self.assertEqual(stats["retries_scheduled"], 1)
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["pending_retries"], 1)
        indexer.stop()

    def test_give_up_records_last_give_up(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder(fail_times=99)
        indexer = _make(rag, embedder)
        with self.assertLogs("app.message_indexer", level="WARNING"):
            indexer._index_one(_row(7), attempt=2)
        stats = indexer.stats()
        self.assertEqual(stats["gave_up"], 1)
        self.assertEqual(stats["last_give_up"], {
            "message_id": 7,
            "stage": "embed",
            "attempts": 3,
        })
        indexer.stop()

    def test_short_and_already_present_counters(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder()
        indexer = _make(rag, embedder)
        indexer._index_one(_row(content="hi"), attempt=0)  # below min length
        stats = indexer.stats()
        self.assertEqual(stats["skipped_short"], 1)
        self.assertEqual(stats["indexed"], 0)
        indexer.stop()


class SuccessAndStopTests(unittest.TestCase):
    def test_clean_success_no_retry(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder()
        indexer = _make(rag, embedder)
        indexer._index_one(_row(3), attempt=0)
        self.assertEqual(rag.added, [3])
        self.assertEqual(indexer._retry_timers, set())
        indexer.stop()

    def test_stop_cancels_pending_timers(self) -> None:
        rag = _FakeRag()
        embedder = _FakeEmbedder(fail_times=1)
        indexer = _make(rag, embedder)
        indexer._index_one(_row(), attempt=0)
        self.assertEqual(len(indexer._retry_timers), 1)
        indexer.stop()
        self.assertEqual(indexer._retry_timers, set())


if __name__ == "__main__":
    unittest.main()

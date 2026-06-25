"""Async indexer that embeds chat messages into the :class:`RagStore`.

Hooks :meth:`ChatDatabase.add_message_listener`. Each new message is queued
on a daemon thread that:
  1. Embeds the message text via the shared :class:`Embedder`.
  2. Writes the row to the LanceDB ``messages`` table.

On startup we also kick off a one-shot backfill that scans every session in
:class:`ChatDatabase` and indexes any messages not already present (the
RagStore upserts on ``id`` so re-runs are idempotent). Backfill runs at low
priority and skips messages whose content is empty or trivially short.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Optional

from app.core.infra.chat_database import ChatDatabase, MessageRow

if TYPE_CHECKING:
    from app.core.rag.rag_store import RagStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.message_indexer")


# Skip messages that aren't worth indexing -- pure single-token replies, etc.
_MIN_INDEX_LENGTH = 8

# Retry policy for transient embed / write failures. A flaky embedding
# endpoint used to permanently drop the message from RAG recall (the old
# code logged at DEBUG and returned). We now re-attempt a bounded number
# of times with backoff; if every attempt fails we log at WARNING (so the
# rot is visible in ``tail_logs``) and lean on the idempotent startup
# backfill as the long-term safety net. Bounded so a permanently-broken
# embedder can't build an unbounded backlog of pending timers.
_MAX_INDEX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2.0, 8.0, 30.0)


class MessageIndexer:
    """Embed-and-store pipeline driven off ``ChatDatabase`` writes."""

    def __init__(
        self,
        db: ChatDatabase,
        rag: "RagStore",
        embedder: "Embedder",
    ) -> None:
        self._db = db
        self._rag = rag
        self._embedder = embedder
        # Each queue element is ``(row, attempt)``; the ``None`` sentinel
        # still means "shut down". ``attempt`` is the 0-based retry count.
        self._queue: "queue.Queue[Optional[tuple[MessageRow, int]]]" = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="MessageIndexer", daemon=True
        )
        self._backfill_thread: threading.Thread | None = None
        # Outstanding retry timers, so ``stop()`` can cancel them and the
        # daemon process can exit promptly instead of waiting out a 30 s
        # backoff.
        self._retry_timers: set[threading.Timer] = set()
        self._retry_lock = threading.Lock()
        # P6: lifecycle counters + queue visibility. Incremented from the
        # DB-listener thread (enqueue), the worker thread (index), the
        # backfill thread, and retry timers, so all mutations go through
        # ``_bump`` under ``_stats_lock``. Surfaced via :meth:`stats` and
        # the MCP ``get_message_indexer_stats`` tool.
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {
            "enqueued": 0,
            "indexed": 0,
            "skipped_short": 0,
            "already_present": 0,
            "embed_failures": 0,
            "write_failures": 0,
            "retries_scheduled": 0,
            "gave_up": 0,
            "dropped_queue_full": 0,
            "backfill_walked": 0,
        }
        self._last_index_at: float | None = None  # monotonic seconds
        self._last_give_up: dict[str, object] | None = None
        self._db.add_message_listener(self._enqueue)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def stats(self) -> dict[str, object]:
        """Snapshot of indexer counters + live queue / thread state (P6).

        Cheap to call (one lock acquire + a ``qsize``). The diagnostics
        worth watching: ``queue_depth`` (pending embeds — a sustained
        climb means the embedder can't keep up with chat volume),
        ``pending_retries`` (transient embed/write failures in back-off),
        ``gave_up`` + ``last_give_up`` (rows that fell out of RAG until
        the next startup backfill), and ``last_index_age_seconds``.
        """
        with self._stats_lock:
            snapshot = dict(self._stats)
        with self._retry_lock:
            snapshot["pending_retries"] = len(self._retry_timers)
        snapshot["queue_depth"] = self._queue.qsize()
        snapshot["worker_alive"] = self._worker.is_alive()
        snapshot["backfill_running"] = bool(
            self._backfill_thread is not None
            and self._backfill_thread.is_alive()
        )
        snapshot["last_index_age_seconds"] = (
            None
            if self._last_index_at is None
            else max(0.0, time.monotonic() - self._last_index_at)
        )
        snapshot["last_give_up"] = self._last_give_up
        return snapshot

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self, *, backfill: bool = True) -> None:
        if not self._worker.is_alive():
            self._worker.start()
        if backfill:
            self._backfill_thread = threading.Thread(
                target=self._backfill_safe, name="MessageIndexerBackfill", daemon=True
            )
            self._backfill_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._db.remove_message_listener(self._enqueue)
        except Exception:
            pass
        # Cancel any pending retry timers so a long backoff doesn't keep
        # a thread alive past shutdown.
        with self._retry_lock:
            timers = list(self._retry_timers)
            self._retry_timers.clear()
        for timer in timers:
            try:
                timer.cancel()
            except Exception:
                pass
        # Wake the worker so it can exit.
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

    # ── queue / worker ──────────────────────────────────────────────────

    def _enqueue(self, row: MessageRow) -> None:
        if not _should_index(row):
            return
        try:
            self._queue.put_nowait((row, 0))
            self._bump("enqueued")
        except Exception:
            self._bump("dropped_queue_full")
            log.warning("indexer queue full; dropping message", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            row, attempt = item
            self._index_one(row, attempt)

    def _index_one(self, row: MessageRow, attempt: int = 0) -> None:
        try:
            if self._rag.has_message(row.session_id, row.id):
                self._bump("already_present")
                return
        except Exception:
            pass
        text = (row.content or "").strip()
        if len(text) < _MIN_INDEX_LENGTH:
            self._bump("skipped_short")
            return
        try:
            vec = self._embedder.embed(text)
        except Exception:
            self._bump("embed_failures")
            log.debug("embed failed for msg %d (attempt %d)", row.id, attempt)
            self._handle_failure(row, attempt, stage="embed")
            return
        try:
            self._rag.add_message(
                session_id=row.session_id,
                message_id=row.id,
                role=row.role,
                content=text,
                embedding=vec,
                created_at=row.created_at,
            )
        except Exception:
            self._bump("write_failures")
            log.debug(
                "rag.add_message failed for msg %d (attempt %d)", row.id, attempt
            )
            self._handle_failure(row, attempt, stage="write")
            return
        self._bump("indexed")
        self._last_index_at = time.monotonic()

    # ── retry ───────────────────────────────────────────────────────────

    def _handle_failure(self, row: MessageRow, attempt: int, *, stage: str) -> None:
        """Schedule a bounded retry, or give up loudly on the last attempt."""
        if self._stop.is_set():
            return
        next_attempt = attempt + 1
        if next_attempt >= _MAX_INDEX_ATTEMPTS:
            # Visible at WARNING so silent RAG rot shows up in ``tail_logs``.
            # The startup backfill re-attempts on the next boot.
            self._bump("gave_up")
            self._last_give_up = {
                "message_id": int(row.id),
                "stage": stage,
                "attempts": _MAX_INDEX_ATTEMPTS,
            }
            log.warning(
                "message indexer gave up on msg %d after %d attempts (%s stage); "
                "RAG recall will miss it until the next startup backfill",
                row.id,
                _MAX_INDEX_ATTEMPTS,
                stage,
            )
            return
        self._bump("retries_scheduled")
        delay = _RETRY_BACKOFF_SECONDS[
            min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)
        ]
        log.debug(
            "message indexer retry scheduled msg=%d next_attempt=%d stage=%s delay=%.0fs",
            row.id,
            next_attempt,
            stage,
            delay,
        )
        self._schedule_retry(row, next_attempt, delay)

    def _schedule_retry(self, row: MessageRow, attempt: int, delay: float) -> None:
        def _fire() -> None:
            with self._retry_lock:
                self._retry_timers.discard(timer)
            if self._stop.is_set():
                return
            try:
                self._queue.put_nowait((row, attempt))
            except Exception:
                log.debug(
                    "indexer retry requeue failed msg=%d", row.id, exc_info=True
                )

        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        with self._retry_lock:
            if self._stop.is_set():
                return
            self._retry_timers.add(timer)
        timer.start()

    # ── backfill ────────────────────────────────────────────────────────

    def _backfill_safe(self) -> None:
        try:
            self._backfill()
        except Exception:
            log.warning("message indexer backfill crashed", exc_info=True)

    def _backfill(self) -> None:
        # We slow-walk the history so we don't hammer the embedding endpoint
        # on a fresh boot.
        sessions = self._db.list_sessions()
        if not sessions:
            return
        count_indexed = 0
        for sess in sessions:
            if self._stop.is_set():
                return
            session_id = str(sess.get("session_id") or "")
            if not session_id:
                continue
            messages = self._db.get_messages(session_id)
            for row in messages:
                if self._stop.is_set():
                    return
                if not _should_index(row):
                    continue
                self._index_one(row)
                count_indexed += 1
                self._bump("backfill_walked")
                # Small sleep so the embedder has breathing room and the
                # rest of the app stays responsive on first launch.
                time.sleep(0.01)
        if count_indexed:
            log.info("message indexer backfill walked %d candidate rows", count_indexed)


# ── helpers ─────────────────────────────────────────────────────────────────


def _should_index(row: MessageRow) -> bool:
    if row.role not in ("user", "assistant"):
        return False
    text = (row.content or "").strip()
    if len(text) < _MIN_INDEX_LENGTH:
        return False
    return True

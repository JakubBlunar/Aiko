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
        self._queue: "queue.Queue[Optional[MessageRow]]" = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="MessageIndexer", daemon=True
        )
        self._backfill_thread: threading.Thread | None = None
        self._db.add_message_listener(self._enqueue)

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
            self._queue.put_nowait(row)
        except Exception:
            log.debug("indexer queue full; dropping message", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            self._index_one(item)

    def _index_one(self, row: MessageRow) -> None:
        try:
            if self._rag.has_message(row.session_id, row.id):
                return
        except Exception:
            pass
        text = (row.content or "").strip()
        if len(text) < _MIN_INDEX_LENGTH:
            return
        try:
            vec = self._embedder.embed(text)
        except Exception:
            log.debug("embed failed for msg %d", row.id, exc_info=True)
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
            log.debug("rag.add_message failed for msg %d", row.id, exc_info=True)

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

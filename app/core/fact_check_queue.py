"""Persistent claim queue for the F1 background fact-checker.

The queue is a small JSON list under ``kv_meta`` key
``fact_checker.queue``. We persist (rather than holding entirely in
memory) so a restart mid-session doesn't drop pending checks. The
chosen storage layer is fine because:

* Volume is tiny — bounded at ``max_entries`` (default 50). Each entry
  is ~200 bytes, so the JSON blob stays under 10 KB.
* The reader/writer is :class:`IdleFactChecker` which runs at most every
  ``fact_checker_interval_seconds`` (default 300 s), so contention is
  zero.
* ``ChatDatabase.kv_set`` already gives us atomic INSERT-OR-REPLACE,
  matching the all-or-nothing semantics we want.

Concurrency: each :class:`FactCheckQueue` instance carries a per-process
threading.Lock so a single SessionController writing from multiple
threads (turn runner, REST endpoints) doesn't lose updates. Multi-
process is not supported by design — only one assistant process owns the
DB at a time.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase


log = logging.getLogger("app.fact_check_queue")


_KV_KEY = "fact_checker.queue"
_DEFAULT_MAX_ENTRIES = 50


@dataclass(frozen=True)
class ClaimItem:
    """One claim awaiting verification."""

    memory_id: int
    claim_text: str
    claim_kind: str  # "year" / "measurement" / "date" / "proper_noun" /
    # "knowledge_gap" — set when the queued item is a gap question.
    enqueued_at: str  # ISO-8601 UTC

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ClaimItem":
        return cls(
            memory_id=int(raw.get("memory_id") or 0),
            claim_text=str(raw.get("claim_text") or ""),
            claim_kind=str(raw.get("claim_kind") or "fact"),
            enqueued_at=str(raw.get("enqueued_at") or ""),
        )


class FactCheckQueue:
    """Persistent FIFO of claims for :class:`IdleFactChecker` to process."""

    def __init__(
        self,
        chat_db: "ChatDatabase",
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._chat_db = chat_db
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()

    # ── load / save ──────────────────────────────────────────────────

    def _load(self) -> list[ClaimItem]:
        raw = self._chat_db.kv_get(_KV_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("fact-check queue: corrupt JSON, resetting")
            return []
        if not isinstance(data, list):
            return []
        out: list[ClaimItem] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(ClaimItem.from_dict(entry))
            except (TypeError, ValueError):
                continue
        return out

    def _save(self, items: list[ClaimItem]) -> None:
        # Drop oldest on overflow rather than refusing new entries —
        # the F1 path prioritises fresh claims; stale ones can be
        # re-extracted from the memory store next time the worker
        # sweeps.
        if len(items) > self._max_entries:
            items = items[-self._max_entries :]
        payload = json.dumps([i.to_dict() for i in items])
        self._chat_db.kv_set(_KV_KEY, payload)

    # ── public API ───────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        memory_id: int,
        claim_text: str,
        claim_kind: str,
    ) -> None:
        """Append one claim. No-op on empty text or duplicate (memory_id, text)."""
        cleaned = (claim_text or "").strip()
        if not cleaned:
            return
        from datetime import datetime, timezone

        item = ClaimItem(
            memory_id=int(memory_id),
            claim_text=cleaned,
            claim_kind=str(claim_kind or "fact"),
            enqueued_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            items = self._load()
            # Dedupe on (memory_id, claim_text). A noisy turn might
            # re-enqueue the same claim; we shouldn't waste a slot.
            for existing in items:
                if (
                    existing.memory_id == item.memory_id
                    and existing.claim_text == item.claim_text
                ):
                    return
            items.append(item)
            self._save(items)

    def pop_next(self) -> ClaimItem | None:
        """Remove + return the oldest pending claim, or ``None``."""
        with self._lock:
            items = self._load()
            if not items:
                return None
            head = items[0]
            self._save(items[1:])
            return head

    def requeue_front(self, item: ClaimItem) -> None:
        """Put ``item`` back at the head of the queue (cancellation path)."""
        with self._lock:
            items = self._load()
            items.insert(0, item)
            self._save(items)

    def drop_for_memory(self, memory_id: int) -> int:
        """Remove every queued claim attached to ``memory_id``.

        Returns how many entries were dropped. Used when a memory is
        deleted so we don't waste a fact-check on a row that no longer
        exists.
        """
        with self._lock:
            items = self._load()
            before = len(items)
            kept = [i for i in items if i.memory_id != int(memory_id)]
            if len(kept) == before:
                return 0
            self._save(kept)
            return before - len(kept)

    def peek_all(self) -> list[ClaimItem]:
        with self._lock:
            return list(self._load())

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._load())

    def __len__(self) -> int:  # pragma: no cover -- convenience
        return len(self.peek_all())

    def clear(self) -> None:  # pragma: no cover -- maintenance helper
        with self._lock:
            self._chat_db.kv_set(_KV_KEY, "[]")

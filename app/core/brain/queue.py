"""Priority-ordered, single-consumer event queue for the brain loop.

Thin wrapper around :mod:`heapq` protected by a
:class:`threading.Condition` so producer threads can ``put`` from
anywhere and the consumer thread (``BrainLoop``) can ``get`` with a
timeout that wakes on either a new item or an explicit ``close()``.

Why not :class:`queue.PriorityQueue`?

* We need ``peek(n)`` and ``depth()`` for the
  ``get_brain_queue_state`` MCP debug surface — these require holding
  the internal lock so a single snapshot is consistent.
* We need ``close()`` semantics that *unblocks* every waiter cleanly
  (returns ``None`` rather than spinning). ``PriorityQueue`` doesn't
  expose this without a sentinel-item dance.
* We want the priority + sequence tie-break to live in our own entry
  struct that's easy to inspect from tests.

The queue is **strictly single-consumer**: ``get`` is intended to be
called from one thread only (``BrainLoop``). Multiple consumers would
work correctness-wise (the condition variable + lock are
multi-waiter-safe), but the design assumes one and the loop's
deferral / re-park semantics rely on it.

Logging contract — see ``docs/brain-orchestration.md``:

* DEBUG ``brain-queue put: kind=<kind> priority=<n> seq=<n> depth=<n>``
* DEBUG ``brain-queue pop: kind=<kind> priority=<n> seq=<n> wait_ms=<ms> depth_after=<n>``
* DEBUG ``brain-queue closed: drained=<n>``

INFO-level lines belong on the loop / orchestrator, not here.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.brain.events import BrainEvent


log = logging.getLogger("app.brain_queue")


@dataclass(order=True)
class _QueueEntry:
    """Heap-ordered wrapper.

    Order is (``priority``, ``sequence``) so lower priority wins and
    ties break by enqueue order (FIFO). ``enqueued_at`` is wall time
    for the MCP debug surface; ``event`` is the actual payload and is
    excluded from comparison (``compare=False``) so two heap entries
    with the same priority+sequence never trip ``<`` on incomparable
    events.
    """

    priority: int
    sequence: int
    enqueued_at: float = field(compare=False)
    event: "BrainEvent" = field(compare=False)


class BrainEventQueue:
    """Priority-ordered single-consumer queue.

    Internal structure:

    * ``_heap`` — :mod:`heapq`-managed list of :class:`_QueueEntry`.
    * ``_lock`` — guards every read/write of ``_heap``.
    * ``_not_empty`` — condition variable producers signal on
      ``put`` and ``close``; the consumer waits on it in ``get``.
    * ``_sequence`` — monotonic counter assigned at ``put`` time to
      break priority ties without ever falling back to comparing
      events.
    * ``_closed`` — once set, ``get`` returns ``None`` immediately and
      every blocked ``get`` waiter is released by ``notify_all``.

    The queue does **not** start a thread — that's the loop's job.
    """

    def __init__(self) -> None:
        self._heap: list[_QueueEntry] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._sequence: "itertools.count[int]" = itertools.count()
        self._closed: bool = False
        self._dispatch_count: int = 0
        log.info("brain-queue init: priorities=USER_INPUT..STATE_SYNC")

    # ── producer surface ─────────────────────────────────────────────

    def put(self, event: "BrainEvent") -> None:
        """Enqueue an event. Thread-safe; can be called from any producer.

        After a successful put, ``_not_empty.notify()`` wakes the
        single consumer. If the queue is already closed, this is a
        no-op (we don't raise — producers shouldn't have to know about
        shutdown races).
        """
        priority = int(getattr(event, "priority"))
        kind = str(getattr(event, "kind", "?"))
        with self._not_empty:
            if self._closed:
                log.debug(
                    "brain-queue put dropped (closed): kind=%s priority=%d",
                    kind,
                    priority,
                )
                return
            seq = next(self._sequence)
            entry = _QueueEntry(
                priority=priority,
                sequence=seq,
                enqueued_at=time.monotonic(),
                event=event,
            )
            heapq.heappush(self._heap, entry)
            depth = len(self._heap)
            self._not_empty.notify()
        log.debug(
            "brain-queue put: kind=%s priority=%d seq=%d depth=%d",
            kind,
            priority,
            seq,
            depth,
        )

    # ── consumer surface ─────────────────────────────────────────────

    def get(self, timeout: float | None = None) -> "BrainEvent | None":
        """Pop the highest-priority event, blocking up to ``timeout``.

        Returns ``None`` if the queue is closed, **or** if the timeout
        elapsed with no item. Callers (the brain loop) distinguish
        these by checking ``is_closed()`` after a ``None`` return.

        Logs the dequeue at DEBUG with ``wait_ms`` so a developer can
        see how long a consumer thread sat blocked.
        """
        wait_start = time.monotonic()
        with self._not_empty:
            while not self._heap and not self._closed:
                if timeout is None:
                    self._not_empty.wait()
                else:
                    remaining = timeout - (time.monotonic() - wait_start)
                    if remaining <= 0:
                        return None
                    self._not_empty.wait(timeout=remaining)
            if self._closed and not self._heap:
                return None
            entry = heapq.heappop(self._heap)
            depth_after = len(self._heap)
            self._dispatch_count += 1
        wait_ms = (time.monotonic() - wait_start) * 1000.0
        log.debug(
            "brain-queue pop: kind=%s priority=%d seq=%d wait_ms=%.1f depth_after=%d",
            getattr(entry.event, "kind", "?"),
            entry.priority,
            entry.sequence,
            wait_ms,
            depth_after,
        )
        return entry.event

    def close(self) -> None:
        """Close the queue. All blocked consumers wake up and receive ``None``.

        Idempotent: a second ``close`` is a no-op. Existing queued
        events are **left in the heap** so a final ``get`` drains
        them; once the heap is empty, every subsequent ``get``
        returns ``None``.
        """
        with self._not_empty:
            if self._closed:
                return
            self._closed = True
            remaining = len(self._heap)
            self._not_empty.notify_all()
        log.debug("brain-queue closed: drained=%d", remaining)

    # ── debug surface ────────────────────────────────────────────────

    def depth(self) -> int:
        """Current heap size. Atomic snapshot."""
        with self._lock:
            return len(self._heap)

    def is_closed(self) -> bool:
        """Has :meth:`close` been called?"""
        with self._lock:
            return self._closed

    def dispatch_count(self) -> int:
        """Cumulative number of events successfully popped.

        Used by ``get_brain_queue_state`` MCP tool. Resets only on
        process restart — no zero-the-counter API on purpose.
        """
        with self._lock:
            return int(self._dispatch_count)

    def peek(self, n: int = 10) -> list[tuple[int, int, float, str]]:
        """Snapshot the top ``n`` queued entries without consuming.

        Returns a list of ``(priority, sequence, enqueued_at, kind)``
        tuples ordered the same way the consumer would see them. Used
        by the ``get_brain_queue_state`` MCP debug tool — kept as
        plain tuples (not the events themselves) so the snapshot is
        cheap and serializable.
        """
        with self._lock:
            count = max(0, int(n))
            if count == 0:
                return []
            top = heapq.nsmallest(count, self._heap)
        return [
            (e.priority, e.sequence, e.enqueued_at, str(getattr(e.event, "kind", "?")))
            for e in top
        ]


__all__ = ["BrainEventQueue", "_QueueEntry"]

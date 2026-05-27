"""Idle-time background-worker contract (schema v8 / G1 scaffold).

The :class:`IdleWorkerScheduler` (see :mod:`app.core.idle_worker_scheduler`)
periodically wakes during quiet windows -- no Live mode, no recent
user activity -- and asks each registered worker whether it's due. Due
workers run one at a time so CPU stays predictable and so a slow
worker can't stack on top of the next tick.

Initial workers:
  * :class:`MemoryPromotionWorker` -- promote scratchpad rows to
    long_term, demote stale long_term rows to archive, delete dead
    scratchpad rows. See :mod:`app.core.memory_promotion_worker`.
  * :class:`MemoryDecayWorker` -- wall-clock-driven salience decay
    + revival_score rebate. See :mod:`app.core.memory_decay_worker`.

Future workers (mentioned in the backlog as F1 / G2 / G3 / etc.)
implement the same :class:`IdleWorker` Protocol and register at boot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@runtime_checkable
class IdleWorker(Protocol):
    """Anything the :class:`IdleWorkerScheduler` can run during idle time.

    Implementations supply a stable ``name`` (used for logging,
    ``force_run``, and the :class:`IdleWorkerRecord` key), a target
    ``interval_seconds`` between successful runs, and a ``run()``
    method that does the actual work.

    The default :meth:`is_ready` checks elapsed time since the last
    run; override it for richer gating (e.g. "only run while
    scratchpad has rows").
    """

    @property
    def name(self) -> str:
        ...

    @property
    def interval_seconds(self) -> float:
        ...

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        ...

    def run(self) -> dict[str, Any] | None:
        ...


@dataclass(slots=True)
class IdleWorkerRecord:
    """Per-worker state tracked by the scheduler.

    Persisted to ``kv_meta`` (see :meth:`ChatDatabase.kv_set`) so the
    next process boot doesn't immediately re-fire a worker that just
    completed. ``last_error`` is reset on successful runs and surfaced
    via the ``inspect_idle_workers`` MCP debug tool.
    """

    name: str
    last_run_at: datetime | None = None
    last_error: str | None = None
    run_count: int = 0
    last_result: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "run_count": int(self.run_count),
            "last_result": dict(self.last_result) if self.last_result else None,
        }


def default_is_ready(
    interval_seconds: float,
    *,
    now: datetime,
    last_run_at: datetime | None,
) -> bool:
    """Default readiness predicate: due when ``interval_seconds`` elapsed.

    Workers that never ran (``last_run_at is None``) are always ready
    on first scheduler tick. Negative deltas (clock skew) count as
    ready too -- better to fire once spuriously than to silently
    starve.
    """
    if last_run_at is None:
        return True
    delta = (now - last_run_at).total_seconds()
    return delta >= float(interval_seconds)


__all__ = [
    "IdleWorker",
    "IdleWorkerRecord",
    "default_is_ready",
]

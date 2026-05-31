"""Idle-time background-worker contract (schema v8 / G1 scaffold).

The :class:`IdleWorkerScheduler` (see :mod:`app.core.proactive.idle_worker_scheduler`)
periodically wakes during quiet windows -- no Live mode, no recent
user activity -- and asks each registered worker whether it's due. Due
workers run one at a time so CPU stays predictable and so a slow
worker can't stack on top of the next tick.

Initial workers:
  * :class:`MemoryPromotionWorker` -- promote scratchpad rows to
    long_term, demote stale long_term rows to archive, delete dead
    scratchpad rows. See :mod:`app.core.memory.memory_promotion_worker`.
  * :class:`MemoryDecayWorker` -- wall-clock-driven salience decay
    + revival_score rebate. See :mod:`app.core.memory.memory_decay_worker`.

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


# Smoothing factor for the per-worker rolling average duration. 0.3 means a
# fresh measurement contributes 30% and the existing EMA carries 70%, so
# ~5 runs are enough to converge while a one-off slow run can't
# permanently push a worker over the per-tick budget.
_DURATION_EMA_ALPHA: float = 0.3


@dataclass(slots=True)
class IdleWorkerRecord:
    """Per-worker state tracked by the scheduler.

    Persisted to ``kv_meta`` (see :meth:`ChatDatabase.kv_set`) so the
    next process boot doesn't immediately re-fire a worker that just
    completed. ``last_error`` is reset on successful runs and surfaced
    via the ``inspect_idle_workers`` MCP debug tool.

    Duration accounting (P8): every successful run pushes its wall time
    through an EMA so the scheduler can decide whether the worker fits
    in the remaining per-tick budget. ``error_count`` keeps a separate
    cumulative error counter so a flapping worker is visible in
    ``get_status()`` even after a successful retry clears
    ``last_error``.
    """

    name: str
    last_run_at: datetime | None = None
    last_error: str | None = None
    run_count: int = 0
    last_result: dict[str, Any] | None = field(default=None)
    last_duration_ms: float | None = None
    avg_duration_ms: float | None = None
    total_duration_ms: float = 0.0
    error_count: int = 0

    def update_after_run(self, duration_ms: float) -> None:
        """Fold a successful run's wall time into the EMA + totals."""
        ms = max(0.0, float(duration_ms))
        self.last_duration_ms = ms
        self.total_duration_ms += ms
        prev = self.avg_duration_ms
        if prev is None:
            self.avg_duration_ms = ms
        else:
            self.avg_duration_ms = (
                _DURATION_EMA_ALPHA * ms + (1.0 - _DURATION_EMA_ALPHA) * prev
            )

    def update_after_error(self) -> None:
        """Bump the cumulative error counter (last_error is set elsewhere)."""
        self.error_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "run_count": int(self.run_count),
            "last_result": dict(self.last_result) if self.last_result else None,
            "last_duration_ms": (
                round(self.last_duration_ms, 2)
                if self.last_duration_ms is not None
                else None
            ),
            "avg_duration_ms": (
                round(self.avg_duration_ms, 2)
                if self.avg_duration_ms is not None
                else None
            ),
            "total_duration_ms": round(self.total_duration_ms, 2),
            "error_count": int(self.error_count),
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

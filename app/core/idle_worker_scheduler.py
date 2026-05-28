"""Background scheduler for :class:`IdleWorker` instances (schema v8 / G1).

Runs a single daemon thread that wakes every ``wake_seconds`` (default
60s; configurable for testing), asks each registered worker whether
it's due, and drains as many due workers as fit into a per-tick wall
budget so the typing/speaking gap between turns doesn't go to waste
(P8). Skips entirely when ``is_quiet_callback`` returns ``False`` --
the gate :class:`SessionController` uses to keep workers from
contending with an active conversation.

The drain is sequential (one worker at a time on the scheduler thread)
to keep CPU/memory predictable; multiple workers per tick comes from
fitting them into the budget rather than from added concurrency. An
EMA of each worker's wall time (kept on :class:`IdleWorkerRecord`)
drives the budget check, with an anti-starvation rule that always lets
the most-overdue ready worker fire even if its estimate exceeds the
remaining budget.

Per-worker state lives in :class:`IdleWorkerRecord`. ``last_run_at``
is persisted to the ``kv_meta`` table so an app restart doesn't
re-fire a worker that just completed before the crash.

Public API:
    - :meth:`register` to add a worker at boot.
    - :meth:`start` / :meth:`stop` for lifecycle.
    - :meth:`force_run` to trigger a worker on demand (used by the
      ``force_promotion_sweep`` / ``force_decay_sweep`` MCP debug tools
      and by tests).
    - :meth:`get_records` to inspect each worker's last_run / last_error.
    - :meth:`get_status` to surface the enriched per-worker view used
      by the ``get_idle_workers_status`` MCP tool (next_due_at,
      overdue_seconds, avg_duration_ms, error_count).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.idle_worker import (
    IdleWorker,
    IdleWorkerRecord,
    default_is_ready,
)


log = logging.getLogger("app.idle_worker_scheduler")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Reserved kv_meta key prefix for per-worker bookkeeping.
_KV_PREFIX = "idle_worker."

# When a worker has no average yet (first run) and we still need to estimate
# its cost for budget arithmetic, assume this much. Picked low enough that a
# fresh worker isn't pre-emptively skipped on a small budget but high enough
# to avoid stuffing a tick with a dozen unknown workers at once.
_DEFAULT_ESTIMATE_MS: float = 250.0


class IdleWorkerScheduler:
    """Single-threaded scheduler for the IdleWorker registry."""

    def __init__(
        self,
        *,
        wake_seconds: float = 60.0,
        is_quiet_callback: Callable[[], bool] | None = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        tick_budget_ms: int = 3000,
        max_per_tick: int = 0,
    ) -> None:
        """
        Parameters
        ----------
        wake_seconds:
            How often the scheduler thread wakes to check the registry.
            Drop to a few seconds for active testing.
        is_quiet_callback:
            Optional ``() -> bool``. When provided and it returns
            ``False``, the scheduler skips that tick (no worker runs).
            Used by :class:`SessionController` to gate against Live
            mode + recent user activity.
        kv_get / kv_set:
            Optional ``(key) -> str | None`` / ``(key, str) -> None``
            for persisting ``last_run_at`` across restarts. Pass the
            :class:`ChatDatabase` helpers in production; tests can pass
            ``None`` to use in-memory state only.
        tick_budget_ms:
            Soft wall-time budget per tick (P8). The scheduler runs as
            many due workers as fit into this budget, sorted oldest
            first, using each worker's ``avg_duration_ms`` (EMA) as the
            cost estimate. Anti-starvation: at least one due worker
            always runs per tick, even if its estimate exceeds the
            remaining budget. Raise this for long quiet windows; lower
            it (or set 0 to fall back to one-per-tick) on slow
            machines.
        max_per_tick:
            Optional hard cap on workers per tick (0 = unlimited, the
            default; the budget is the soft cap). Useful when you want
            to keep tick logs concise during heavy backlog.
        """
        self._wake_seconds = max(0.5, float(wake_seconds))
        self._is_quiet_callback = is_quiet_callback
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._tick_budget_ms = max(0, int(tick_budget_ms))
        self._max_per_tick = max(0, int(max_per_tick))
        self._workers: dict[str, IdleWorker] = {}
        self._records: dict[str, IdleWorkerRecord] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── registration ─────────────────────────────────────────────────

    def register(self, worker: IdleWorker) -> None:
        """Add a worker. Idempotent on ``worker.name``."""
        name = str(worker.name).strip()
        if not name:
            raise ValueError("IdleWorker.name must be non-empty")
        with self._lock:
            self._workers[name] = worker
            if name not in self._records:
                self._records[name] = IdleWorkerRecord(
                    name=name,
                    last_run_at=self._restore_last_run_at(name),
                )
        log.info("idle_worker registered: %s (interval=%ss)", name, worker.interval_seconds)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._workers.pop(name, None)

    def update_wake_seconds(self, seconds: float) -> None:
        self._wake_seconds = max(0.5, float(seconds))

    def update_tick_budget(self, *, tick_budget_ms: int | None = None,
                           max_per_tick: int | None = None) -> None:
        """Adjust the per-tick budget knobs at runtime (settings reload)."""
        if tick_budget_ms is not None:
            self._tick_budget_ms = max(0, int(tick_budget_ms))
        if max_per_tick is not None:
            self._max_per_tick = max(0, int(max_per_tick))

    def update_quiet_callback(
        self, is_quiet_callback: Callable[[], bool] | None
    ) -> None:
        self._is_quiet_callback = is_quiet_callback

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="idle-worker-scheduler",
            daemon=True,
        )
        self._thread.start()
        log.info("idle_worker_scheduler started (wake=%ss)", self._wake_seconds)

    def stop(self, *, timeout: float = 1.5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    # ── on-demand ────────────────────────────────────────────────────

    def force_run(self, name: str) -> dict[str, Any] | None:
        """Run a registered worker once, bypassing the readiness check.

        Returns the worker's result dict (or ``None`` if the worker
        returned nothing). Raises ``KeyError`` if no worker with that
        name is registered. Errors raised by the worker are caught and
        recorded on :attr:`IdleWorkerRecord.last_error`, then re-raised
        to the caller.
        """
        with self._lock:
            worker = self._workers.get(name)
            record = self._records.get(name)
        if worker is None or record is None:
            raise KeyError(f"unknown idle worker: {name!r}")
        return self._run_one(worker, record)

    def get_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._records.values()]

    def get_status(self) -> dict[str, Any]:
        """Enriched per-worker view used by ``get_idle_workers_status``.

        Returns a dict with the scheduler-level config (wake_seconds,
        tick_budget_ms, max_per_tick, quiet) and a ``workers`` list
        sorted by ``overdue_seconds`` descending so the most-starved
        worker shows up first. Each row carries:

        - ``name``, ``interval_seconds``
        - ``last_run_at``, ``next_due_at`` (isoformat or None)
        - ``overdue_seconds`` (positive = waiting; negative = not yet
          due; ``None`` if the worker has never run, which counts as
          due)
        - ``avg_duration_ms``, ``last_duration_ms``, ``run_count``,
          ``error_count``, ``last_error``

        This is intentionally a snapshot, not live-streaming -- the
        scheduler thread updates records under ``self._lock`` and the
        snapshot copies them out under the same lock.
        """
        now = _utcnow()
        try:
            quiet = (
                bool(self._is_quiet_callback())
                if self._is_quiet_callback is not None
                else True
            )
        except Exception:
            quiet = False

        rows: list[dict[str, Any]] = []
        with self._lock:
            for name, worker in self._workers.items():
                record = self._records[name]
                interval = float(worker.interval_seconds)
                last_run_at = record.last_run_at
                if last_run_at is None:
                    next_due_at: datetime | None = None
                    overdue_seconds: float | None = None
                else:
                    next_due_at = last_run_at + timedelta(seconds=interval)
                    overdue_seconds = (now - next_due_at).total_seconds()
                rows.append({
                    "name": name,
                    "interval_seconds": interval,
                    "last_run_at": (
                        last_run_at.isoformat() if last_run_at else None
                    ),
                    "next_due_at": (
                        next_due_at.isoformat() if next_due_at else None
                    ),
                    "overdue_seconds": (
                        round(overdue_seconds, 2)
                        if overdue_seconds is not None
                        else None
                    ),
                    "last_duration_ms": (
                        round(record.last_duration_ms, 2)
                        if record.last_duration_ms is not None
                        else None
                    ),
                    "avg_duration_ms": (
                        round(record.avg_duration_ms, 2)
                        if record.avg_duration_ms is not None
                        else None
                    ),
                    "total_duration_ms": round(record.total_duration_ms, 2),
                    "run_count": int(record.run_count),
                    "error_count": int(record.error_count),
                    "last_error": record.last_error,
                })

        # Sort: never-run workers first (overdue_seconds=None), then by
        # most-overdue descending. ``-inf`` sentinel keeps the comparator
        # total without juggling None.
        def _key(row: dict[str, Any]) -> tuple[int, float]:
            ov = row["overdue_seconds"]
            if ov is None:
                return (0, 0.0)
            return (1, -float(ov))

        rows.sort(key=_key)
        return {
            "wake_seconds": self._wake_seconds,
            "tick_budget_ms": self._tick_budget_ms,
            "max_per_tick": self._max_per_tick,
            "quiet": quiet,
            "workers": rows,
        }

    # ── internals ────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(self._wake_seconds):
            try:
                self._tick()
            except Exception:
                log.debug("idle_worker tick failed", exc_info=True)

    def _tick(self) -> None:
        if self._is_quiet_callback is not None:
            try:
                if not bool(self._is_quiet_callback()):
                    return
            except Exception:
                log.debug("is_quiet_callback raised; skipping tick", exc_info=True)
                return
        now = _utcnow()
        # Pick due workers in "most overdue first" order. Oldest
        # last_run_at wins ties so we don't starve any one worker.
        # Workers that have never run (last_run_at is None) sort first.
        with self._lock:
            ranked: list[tuple[str, IdleWorker]] = sorted(
                self._workers.items(),
                key=lambda kv: (
                    self._records[kv[0]].last_run_at
                    or datetime.min.replace(tzinfo=timezone.utc)
                ),
            )

        ran = 0
        skipped_budget = 0
        due_total = 0
        ran_names: list[str] = []
        tick_started_ms = time.monotonic() * 1000.0
        budget_remaining_ms = float(self._tick_budget_ms)
        max_runs = self._max_per_tick if self._max_per_tick > 0 else None

        for name, worker in ranked:
            record = self._records[name]
            try:
                ready = worker.is_ready(now=now, last_run_at=record.last_run_at)
            except Exception:
                ready = default_is_ready(
                    worker.interval_seconds,
                    now=now,
                    last_run_at=record.last_run_at,
                )
            if not ready:
                continue
            due_total += 1

            # Anti-starvation: the most-overdue ready worker always runs,
            # even if its estimated cost exceeds the remaining budget.
            # Subsequent workers must fit. Workers that have never run
            # use a small default estimate so a fresh registry doesn't
            # starve everything past slot 1 on tiny budgets.
            estimate_ms = (
                record.avg_duration_ms
                if record.avg_duration_ms is not None
                else _DEFAULT_ESTIMATE_MS
            )
            if ran >= 1 and estimate_ms > budget_remaining_ms:
                skipped_budget += 1
                continue
            if max_runs is not None and ran >= max_runs:
                # Hit the hard cap: count remaining due workers as deferred.
                skipped_budget += 1
                continue

            self._run_one(worker, record, now=now)
            ran += 1
            ran_names.append(name)
            actual_ms = record.last_duration_ms or 0.0
            budget_remaining_ms = max(0.0, budget_remaining_ms - actual_ms)

        if due_total > 0:
            tick_elapsed_ms = (time.monotonic() * 1000.0) - tick_started_ms
            queue_after = max(0, due_total - ran)
            log.info(
                "idle_workers tick: ran=%d due=%d skipped_budget=%d "
                "queue_after=%d tick_ms=%.0f budget_ms=%d names=%s",
                ran, due_total, skipped_budget, queue_after,
                tick_elapsed_ms, self._tick_budget_ms,
                ",".join(ran_names) if ran_names else "-",
            )

    def _run_one(
        self,
        worker: IdleWorker,
        record: IdleWorkerRecord,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        started_at = now or _utcnow()
        started_ms = time.monotonic() * 1000.0
        log.info("idle_worker run start: %s", worker.name)
        try:
            result = worker.run()
        except Exception as exc:
            elapsed_ms = (time.monotonic() * 1000.0) - started_ms
            log.warning(
                "idle_worker %s failed after %.0fms: %s",
                worker.name, elapsed_ms, exc, exc_info=True,
            )
            with self._lock:
                record.last_error = f"{type(exc).__name__}: {exc}"
                record.update_after_error()
            raise
        finished_at = _utcnow()
        elapsed_ms = (time.monotonic() * 1000.0) - started_ms
        with self._lock:
            record.last_run_at = finished_at
            record.last_error = None
            record.run_count += 1
            record.last_result = dict(result) if isinstance(result, dict) else None
            record.update_after_run(elapsed_ms)
        self._persist_last_run_at(worker.name, finished_at)
        log.info(
            "idle_worker run done: %s (%.0fms, avg=%.0fms) result=%s",
            worker.name,
            elapsed_ms,
            record.avg_duration_ms or 0.0,
            record.last_result,
        )
        return record.last_result

    def _restore_last_run_at(self, name: str) -> datetime | None:
        if self._kv_get is None:
            return None
        try:
            raw = self._kv_get(_KV_PREFIX + name + ".last_run_at")
        except Exception:
            return None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    def _persist_last_run_at(self, name: str, when: datetime) -> None:
        if self._kv_set is None:
            return
        try:
            self._kv_set(_KV_PREFIX + name + ".last_run_at", when.isoformat())
        except Exception:
            log.debug("kv_set last_run_at failed for %s", name, exc_info=True)


__all__ = ["IdleWorkerScheduler"]

"""Background scheduler for :class:`IdleWorker` instances (schema v8 / G1).

Runs a single daemon thread that wakes every ``wake_seconds`` (default
60s; configurable for testing), asks each registered worker whether
it's due, and runs *one* due worker per wake-up so CPU stays
predictable. Skips entirely when ``is_quiet_callback`` returns
``False`` -- the gate :class:`SessionController` uses to keep workers
from contending with an active conversation.

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
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
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


class IdleWorkerScheduler:
    """Single-threaded scheduler for the IdleWorker registry."""

    def __init__(
        self,
        *,
        wake_seconds: float = 60.0,
        is_quiet_callback: Callable[[], bool] | None = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
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
        """
        self._wake_seconds = max(0.5, float(wake_seconds))
        self._is_quiet_callback = is_quiet_callback
        self._kv_get = kv_get
        self._kv_set = kv_set
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
        # Pick the *single* most-overdue ready worker. Oldest last_run
        # wins ties so we don't starve any one worker.
        with self._lock:
            ranked = sorted(
                self._workers.items(),
                key=lambda kv: (
                    self._records[kv[0]].last_run_at or datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
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
            self._run_one(worker, record, now=now)
            # Cap at one worker per tick so heavy workers don't pile.
            return

    def _run_one(
        self,
        worker: IdleWorker,
        record: IdleWorkerRecord,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        started_at = now or _utcnow()
        log.info("idle_worker run start: %s", worker.name)
        try:
            result = worker.run()
        except Exception as exc:
            log.warning("idle_worker %s failed: %s", worker.name, exc, exc_info=True)
            with self._lock:
                record.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finished_at = _utcnow()
        with self._lock:
            record.last_run_at = finished_at
            record.last_error = None
            record.run_count += 1
            record.last_result = dict(result) if isinstance(result, dict) else None
        self._persist_last_run_at(worker.name, finished_at)
        dur = (finished_at - started_at).total_seconds()
        log.info(
            "idle_worker run done: %s (%.2fs) result=%s",
            worker.name,
            dur,
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

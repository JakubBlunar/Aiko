"""Terminal-task pruning worker (schema v17).

The task system never deletes rows during normal operation —
:class:`TaskStore.delete` exists but is reserved for tests + MCP
cleanup. With long-running agentic workloads spawning lots of
short-lived children, terminal rows accumulate indefinitely. This
:class:`IdleWorker` plugs into the existing
:class:`IdleWorkerScheduler` (same pattern as
:class:`MemoryDecayWorker`) and prunes terminal rows older than the
configured retention window, cascade-deleting their event log + input
history at the same time.

Run cadence: ``agent.task_cleanup_interval_seconds`` (default 6h).
Retention: ``agent.task_cleanup_retention_days`` (default 30 days).

Pruning rules:

* Only :data:`TERMINAL_STATUSES` rows are eligible (running /
  awaiting_input / paused stay forever; the heartbeat sweeper handles
  stalled rows).
* ``completed_at`` is the cutoff anchor. A row whose ``completed_at``
  is older than ``retention_days`` ago is eligible.
* Per-tick row cap (``max_rows_per_tick``, default 500) bounds the
  worst-case I/O so a long-deferred cleanup doesn't lock the DB.
* The orchestrator's logger sees one ``task_cleanup sweep:`` INFO
  line per tick with the per-bucket counts.

Cascade order: events + inputs are deleted **before** the task row so
a crash between rows can't leave orphan child rows. The reverse order
would be cleaner if SQL FKs were enabled, but the schema deliberately
avoids them so cascade decisions stay auditable in Python.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.tasks.task_events import TaskEventStore
    from app.core.tasks.task_inputs import TaskInputStore
    from app.core.tasks.task_store import TaskStore


log = logging.getLogger("app.task_cleanup_worker")


# Cap on rows processed per tick. The cleanup is intentionally
# defer-friendly — if a multi-day outage leaves 50k stale rows, we'd
# rather clean them in steady chunks than block the DB for minutes.
DEFAULT_MAX_ROWS_PER_TICK: int = 500


class TaskCleanupWorker:
    """Idle-time pruner for terminal task rows.

    Constructed with the three task stores (tasks, events, inputs)
    so cascade-delete is one atomic enough sequence. ``enabled`` is
    checked at every ``is_ready`` so a runtime settings flip lands
    on the next scheduler tick without an orchestrator restart.
    """

    name: str = "task_cleanup"

    def __init__(
        self,
        store: "TaskStore",
        *,
        event_store: "TaskEventStore | None" = None,
        input_store: "TaskInputStore | None" = None,
        retention_days: int = 30,
        interval_seconds: int = 21600,
        max_rows_per_tick: int = DEFAULT_MAX_ROWS_PER_TICK,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._event_store = event_store
        self._input_store = input_store
        self._retention_days = max(1, int(retention_days))
        self._interval_seconds = max(600, int(interval_seconds))
        self._max_rows_per_tick = max(1, int(max_rows_per_tick))
        self._enabled = bool(enabled)

    @property
    def interval_seconds(self) -> float:
        return float(self._interval_seconds)

    def configure(
        self,
        *,
        retention_days: int | None = None,
        interval_seconds: int | None = None,
        max_rows_per_tick: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Live reconfigure. Called on settings reload."""
        if retention_days is not None:
            self._retention_days = max(1, int(retention_days))
        if interval_seconds is not None:
            self._interval_seconds = max(600, int(interval_seconds))
        if max_rows_per_tick is not None:
            self._max_rows_per_tick = max(1, int(max_rows_per_tick))
        if enabled is not None:
            self._enabled = bool(enabled)

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled:
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    def run(self) -> dict[str, Any]:
        """Prune one batch of terminal rows. Returns per-bucket counts.

        Safe to call directly from the test harness; the scheduler
        invokes this on the idle thread normally.
        """
        if not self._enabled:
            return {"skipped": True, "reason": "disabled"}
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=self._retention_days)
        ).isoformat()
        try:
            stale = self._store.list_terminal_older_than(
                cutoff, limit=self._max_rows_per_tick
            )
        except Exception:
            log.exception("task_cleanup: list_terminal_older_than failed")
            return {"deleted_tasks": 0, "deleted_events": 0, "deleted_inputs": 0}
        if not stale:
            log.debug(
                "task_cleanup: no stale rows (cutoff=%s retention_days=%d)",
                cutoff,
                self._retention_days,
            )
            return {
                "deleted_tasks": 0,
                "deleted_events": 0,
                "deleted_inputs": 0,
                "cutoff": cutoff,
            }
        deleted_tasks = 0
        deleted_events = 0
        deleted_inputs = 0
        for row in stale:
            row_id = int(row.id)
            # Delete events first, then inputs, then the row itself.
            # A crash between sub-deletes leaves orphan events/inputs
            # rather than orphan tasks — the next tick re-runs the
            # event/input deletes via ``list_terminal_older_than``
            # picking up the same row (idempotent).
            if self._event_store is not None:
                try:
                    deleted_events += self._event_store.delete_for_task(row_id)
                except Exception:
                    log.exception(
                        "task_cleanup: event delete failed task=%d", row_id
                    )
            if self._input_store is not None:
                try:
                    deleted_inputs += self._input_store.delete_for_task(row_id)
                except Exception:
                    log.exception(
                        "task_cleanup: input delete failed task=%d", row_id
                    )
            try:
                if self._store.delete(row_id):
                    deleted_tasks += 1
            except Exception:
                log.exception(
                    "task_cleanup: row delete failed task=%d", row_id
                )
        log.info(
            "task_cleanup sweep: deleted_tasks=%d deleted_events=%d "
            "deleted_inputs=%d cutoff=%s retention_days=%d",
            deleted_tasks,
            deleted_events,
            deleted_inputs,
            cutoff,
            self._retention_days,
        )
        return {
            "deleted_tasks": deleted_tasks,
            "deleted_events": deleted_events,
            "deleted_inputs": deleted_inputs,
            "cutoff": cutoff,
            "retention_days": self._retention_days,
        }


__all__ = ["TaskCleanupWorker", "DEFAULT_MAX_ROWS_PER_TICK"]

"""Heartbeat zombie detector for the task orchestrator (schema v17).

Boot recovery ([`app/core/tasks/recovery.py`](recovery.py)) handles
*process-death* zombies — rows that were ``running`` when the
previous process crashed. That works because a fresh boot is a clean
slate.

The harder case is the *in-process* zombie: the handler thread is
alive but stuck on a syscall, network call, or deadlock. The
orchestrator bumps ``tasks.heartbeat_at`` on every emit, so the
signal is already there — this module owns the sweep that looks for
rows whose heartbeat is stale.

Threading model:

* One daemon thread owned by :class:`HeartbeatChecker`, started by
  the orchestrator at construction time and stopped by
  :meth:`TaskOrchestrator.shutdown`.
* Wakes every ``check_interval_seconds`` (default 30s), runs
  :meth:`TaskStore.list_stalled` against the configured
  ``stalled_seconds`` threshold, processes each stalled row according
  to ``action`` (``warn`` logs a WARNING + appends a
  :data:`EVENT_HEARTBEAT_STALLED` event; ``fail`` additionally moves
  the row to ``status='failed'`` with a "stalled" error).
* Exceptions in the sweep are caught + logged so a single bad row
  doesn't poison the loop.

Action semantics:

* ``"warn"`` (default) — the kindest action; the row stays ``running``
  so a handler that eventually unsticks can still emit a terminal
  outcome. The event log carries the stall moment so a developer
  can grep `EVENT_HEARTBEAT_STALLED` to find these.
* ``"fail"`` — promote the WARNING to a real terminal transition.
  The row moves to ``failed`` with ``error="stalled (no heartbeat
  for Xs)"``. Useful in production once we trust the heartbeat
  threshold; risky during development because it kills rows that
  are merely slow.

Each stalled row is processed at most once per sweep, so a row that
gets repeatedly flagged emits one event per sweep cycle (cheap +
greppable).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from app.core.tasks.task_events import EVENT_HEARTBEAT_STALLED
from app.core.tasks.task_handler import STATUS_RUNNING

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.tasks.task_events import TaskEventStore
    from app.core.tasks.task_store import TaskRow, TaskStore


log = logging.getLogger("app.task_heartbeat")


# Valid values for the ``action`` parameter. Mirrored as the allowed
# values for ``agent.task_stalled_action`` in settings.
ACTION_WARN = "warn"
ACTION_FAIL = "fail"
VALID_ACTIONS: frozenset[str] = frozenset((ACTION_WARN, ACTION_FAIL))


class HeartbeatChecker:
    """Daemon-thread sweeper that flags / kills stalled tasks.

    Owned by :class:`TaskOrchestrator`. Stopped via
    :meth:`HeartbeatChecker.stop` (called from
    :meth:`TaskOrchestrator.shutdown`). Safe to construct with
    ``enabled=False`` — the daemon thread starts but exits
    immediately, so feature flagging is just a setting flip.
    """

    def __init__(
        self,
        store: "TaskStore",
        *,
        event_store: "TaskEventStore | None" = None,
        check_interval_seconds: int = 30,
        stalled_seconds: int = 300,
        action: str = ACTION_WARN,
        enabled: bool = True,
        thread_name: str = "task-heartbeat",
    ) -> None:
        self._store = store
        self._event_store = event_store
        self._interval = max(5, int(check_interval_seconds))
        self._stalled_seconds = max(60, int(stalled_seconds))
        action_norm = str(action or ACTION_WARN).strip().lower()
        if action_norm not in VALID_ACTIONS:
            action_norm = ACTION_WARN
        self._action = action_norm
        self._enabled = bool(enabled)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        # Counts exposed to MCP debug / tests; bumped under the GIL.
        self.sweeps_run: int = 0
        self.stalled_total: int = 0
        self.failed_total: int = 0

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the daemon thread. Idempotent.

        Returns immediately when ``enabled=False`` or the thread is
        already running. The daemon flag means the process can exit
        without waiting on this thread — :meth:`stop` is still the
        clean shutdown.
        """
        if not self._enabled:
            log.info(
                "task-heartbeat: disabled (check_interval=%ds stalled=%ds)",
                self._interval,
                self._stalled_seconds,
            )
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._loop,
            name=self._thread_name,
            daemon=True,
        )
        thread.start()
        self._thread = thread
        log.info(
            "task-heartbeat started: check_interval=%ds stalled=%ds action=%s",
            self._interval,
            self._stalled_seconds,
            self._action,
        )

    def stop(self, *, timeout: float = 2.0) -> None:
        """Signal the daemon thread to exit. Idempotent."""
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None
        log.info(
            "task-heartbeat stopped: sweeps_run=%d stalled_total=%d "
            "failed_total=%d",
            self.sweeps_run,
            self.stalled_total,
            self.failed_total,
        )

    # ── sweep ────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # First sweep happens after ``interval`` seconds. This avoids
        # firing immediately on boot when ``heartbeat_at`` is already
        # behind by definition (rows recovered from interrupted state
        # still hold their last pre-crash value).
        while not self._stop_event.wait(self._interval):
            try:
                self.run_once()
            except Exception:  # pragma: no cover - defensive
                log.exception("task-heartbeat sweep raised")

    def run_once(self) -> int:
        """One sweep. Returns the number of stalled rows found.

        Public for the test harness — tests inject a fake clock by
        calling :meth:`run_once` directly without starting the
        daemon thread.
        """
        if not self._enabled:
            return 0
        try:
            stalled = self._store.list_stalled(self._stalled_seconds)
        except Exception:  # pragma: no cover - defensive
            log.exception("task-heartbeat list_stalled raised")
            return 0
        self.sweeps_run += 1
        if not stalled:
            return 0
        self.stalled_total += len(stalled)
        for row in stalled:
            self._process_stalled(row)
        log.info(
            "task-heartbeat sweep: stalled=%d action=%s",
            len(stalled),
            self._action,
        )
        return len(stalled)

    def _process_stalled(self, row: "TaskRow") -> None:
        # Defensive — the SQL filter already excludes non-running
        # rows but a race could in theory let one through.
        if row.status != STATUS_RUNNING:
            return
        stalled_for_ms = self._approx_stalled_ms(row.heartbeat_at)
        log.warning(
            "task stalled: task=%d handler=%s stalled_ms=%s action=%s",
            row.id,
            row.handler_name,
            stalled_for_ms if stalled_for_ms is not None else "?",
            self._action,
        )
        # Audit on event log first; even in "warn" mode this leaves a
        # greppable breadcrumb that a developer can read later.
        if self._event_store is not None:
            try:
                self._event_store.append(
                    int(row.id),
                    type=EVENT_HEARTBEAT_STALLED,
                    data={
                        "stalled_for_seconds": (
                            int(stalled_for_ms / 1000)
                            if stalled_for_ms is not None
                            else None
                        ),
                        "threshold_seconds": self._stalled_seconds,
                        "action": self._action,
                    },
                )
            except Exception:
                log.exception(
                    "task-heartbeat event append failed: task=%d", row.id
                )
        if self._action != ACTION_FAIL:
            return
        # ``fail`` mode: promote the warning to a terminal transition.
        # The orchestrator's emit pipeline isn't used because the
        # handler thread is by definition unresponsive — going
        # through the store directly mirrors what cancel() does.
        seconds_text = (
            str(int(stalled_for_ms / 1000))
            if stalled_for_ms is not None
            else "?"
        )
        try:
            ok = self._store.mark_failed(
                int(row.id),
                error=f"stalled (no heartbeat for {seconds_text}s)",
            )
        except Exception:
            log.exception(
                "task-heartbeat mark_failed raised: task=%d", row.id
            )
            return
        if ok:
            self.failed_total += 1
            log.warning(
                "task-heartbeat killed stalled task: task=%d handler=%s "
                "stalled_for=%ss",
                row.id,
                row.handler_name,
                seconds_text,
            )

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _approx_stalled_ms(heartbeat_at: str | None) -> int | None:
        """Approx ms since ``heartbeat_at``. Returns None on parse error."""
        if not heartbeat_at:
            return None
        try:
            from datetime import datetime
            anchor = datetime.fromisoformat(str(heartbeat_at))
            now = datetime.now(anchor.tzinfo) if anchor.tzinfo else datetime.now()
            delta = now - anchor
            return int(delta.total_seconds() * 1000)
        except Exception:
            return None


__all__ = [
    "HeartbeatChecker",
    "ACTION_WARN",
    "ACTION_FAIL",
    "VALID_ACTIONS",
]

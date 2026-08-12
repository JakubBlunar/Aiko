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

from app.core.proactive.idle_worker import (
    LANE_COMPUTE,
    LANE_LLM,
    Admission,
    IdleWorker,
    IdleWorkerRecord,
    WorkSignal,
    classify_depth,
    default_is_ready,
    derive_min_interval_s,
    evaluate_admission,
)
from app.core.proactive.llm_contention import (
    CONTENTION_QUEUEING,
    llm_lane_multiplier,
)
from app.core.infra import timephrase


log = logging.getLogger("app.idle_worker_scheduler")


def _utcnow() -> datetime:
    # Routed through the timephrase seam because this is the single "now"
    # handed to every registered worker's is_ready(): shifting it moves
    # the whole idle cadence together under the DT1 debug clock.
    return timephrase.utcnow()


# Reserved kv_meta key prefix for per-worker bookkeeping.
_KV_PREFIX = "idle_worker."

# When a worker has no average yet (first run) and we still need to estimate
# its cost for budget arithmetic, assume this much. Picked low enough that a
# fresh worker isn't pre-emptively skipped on a small budget but high enough
# to avoid stuffing a tick with a dozen unknown workers at once.
_DEFAULT_ESTIMATE_MS: float = 250.0

# A demand() probe is supposed to be a COUNT or a kv_meta read. Once its
# EMA passes this, the premise has broken -- probing is no longer much
# cheaper than running -- so the worker drops back to interval
# behaviour rather than paying the probe on every tick.
_PROBE_BUDGET_MS: float = 50.0

# Multiple of a worker's heartbeat past which it is admitted even though
# its estimate does not fit the lane. Without this, a user who returns
# every few minutes pins idle depth at ``just_left`` forever and a long
# worker never gets a tick it fits in.
_FIT_ESCAPE_HEARTBEATS: float = 3.0

# Lane drain order. Compute first, so cheap arithmetic is never stuck
# behind a multi-second generation; urgency orders within a lane.
_LANE_ORDER = (LANE_COMPUTE, LANE_LLM)


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
        compute_budget_ms: int = 6000,
        pressure_enabled: bool = True,
        urgency_threshold: float = 0.35,
        min_interval_ratio: float = 0.1,
        depth_max_multiplier: float = 10.0,
        idle_depth_provider: Callable[[], float] | None = None,
        contention_provider: Callable[[], str] | None = None,
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
            mode + recent user activity. Also re-checked *between*
            workers inside a tick, so a deep-idle tick with a 10x
            budget stops admitting the moment the user comes back.
        kv_get / kv_set:
            Optional ``(key) -> str | None`` / ``(key, str) -> None``
            for persisting ``last_run_at`` across restarts. Pass the
            :class:`ChatDatabase` helpers in production; tests can pass
            ``None`` to use in-memory state only.
        tick_budget_ms:
            Wall-time budget per tick for workers whose run will call
            the worker LLM (P8/P36). Sized for the worst contention
            case: one local Ollama serving both chat and workers, where
            a background generation delays the user's next first token.
            Scaled per tick by idle depth *and* contention grade.
        compute_budget_ms:
            The same, for workers that touch no LLM. Scaled by idle
            depth alone -- there is no GPU to protect -- which is what
            lets pure-arithmetic workers tick freely on a shared Ollama.
        pressure_enabled:
            Master switch for demand-driven scheduling. ``False``
            restores the pre-P36 path exactly: one budget, oldest-first
            ranking, no probes.
        urgency_threshold:
            Minimum blended pressure/staleness for admission ahead of
            the heartbeat.
        min_interval_ratio:
            Feeds :func:`derive_min_interval_s` for the per-worker
            anti-thrash floor.
        depth_max_multiplier:
            Caps budget growth with idle depth. ``1.0`` disables depth
            scaling.
        idle_depth_provider:
            Optional ``() -> float`` returning seconds since the last
            user activity. Absent means "assume the user just left",
            the most conservative reading.
        contention_provider:
            Optional ``() -> str`` returning a grade from
            :mod:`app.core.proactive.llm_contention`. Absent defaults to
            ``queueing``, which matches today's shared-Ollama sizing.
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
        self._compute_budget_ms = max(0, int(compute_budget_ms))
        self._max_per_tick = max(0, int(max_per_tick))
        self._pressure_enabled = bool(pressure_enabled)
        self._urgency_threshold = max(0.0, float(urgency_threshold))
        self._min_interval_ratio = max(0.0, float(min_interval_ratio))
        self._depth_max_multiplier = max(1.0, float(depth_max_multiplier))
        self._idle_depth_provider = idle_depth_provider
        self._contention_provider = contention_provider
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

    def update_demand_settings(
        self,
        *,
        compute_budget_ms: int | None = None,
        pressure_enabled: bool | None = None,
        urgency_threshold: float | None = None,
        min_interval_ratio: float | None = None,
        depth_max_multiplier: float | None = None,
    ) -> None:
        """Adjust the demand-driven knobs at runtime (settings reload)."""
        if compute_budget_ms is not None:
            self._compute_budget_ms = max(0, int(compute_budget_ms))
        if pressure_enabled is not None:
            self._pressure_enabled = bool(pressure_enabled)
        if urgency_threshold is not None:
            self._urgency_threshold = max(0.0, float(urgency_threshold))
        if min_interval_ratio is not None:
            self._min_interval_ratio = max(0.0, float(min_interval_ratio))
        if depth_max_multiplier is not None:
            self._depth_max_multiplier = max(1.0, float(depth_max_multiplier))

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
                    "demand_aware": callable(
                        getattr(worker, "demand", None)
                    ),
                    "last_pressure": (
                        round(record.last_pressure, 4)
                        if record.last_pressure is not None
                        else None
                    ),
                    "last_urgency": (
                        round(record.last_urgency, 4)
                        if record.last_urgency is not None
                        else None
                    ),
                    "last_admit_reason": record.last_admit_reason,
                    "last_lane": record.last_lane,
                    "last_probe_reason": record.last_probe_reason,
                    "avg_probe_ms": (
                        round(record.avg_probe_ms, 3)
                        if record.avg_probe_ms is not None
                        else None
                    ),
                    "min_interval_seconds": round(
                        derive_min_interval_s(
                            interval,
                            wake_seconds=self._wake_seconds,
                            ratio=self._min_interval_ratio,
                        ),
                        2,
                    ),
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
        idle_s = self._idle_seconds()
        depth_name, tier_index, depth_mult = classify_depth(
            idle_s, max_multiplier=self._depth_max_multiplier,
        )
        grade = self._contention()
        llm_mult = llm_lane_multiplier(
            grade, tier_index=tier_index, depth_multiplier=depth_mult,
        )
        return {
            "wake_seconds": self._wake_seconds,
            "tick_budget_ms": self._tick_budget_ms,
            "compute_budget_ms": self._compute_budget_ms,
            "max_per_tick": self._max_per_tick,
            "quiet": quiet,
            "pressure_enabled": self._pressure_enabled,
            "urgency_threshold": self._urgency_threshold,
            "idle_seconds": round(idle_s, 1),
            "idle_depth": depth_name,
            "depth_multiplier": depth_mult,
            "contention": grade,
            "effective_compute_budget_ms": round(
                float(self._compute_budget_ms) * depth_mult, 1,
            ),
            "effective_llm_budget_ms": round(
                float(self._tick_budget_ms) * llm_mult, 1,
            ),
            "workers": rows,
        }

    # ── internals ────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(self._wake_seconds):
            try:
                self._tick()
            except Exception:
                log.debug("idle_worker tick failed", exc_info=True)

    def _quiet(self) -> bool:
        """Is it safe to run a worker right now? Errors read as 'no'."""
        if self._is_quiet_callback is None:
            return True
        try:
            return bool(self._is_quiet_callback())
        except Exception:
            log.debug("is_quiet_callback raised; treating as busy", exc_info=True)
            return False

    def _tick(self) -> None:
        if not self._quiet():
            return
        if self._pressure_enabled:
            self._tick_demand()
        else:
            self._tick_legacy()

    def _is_ready(
        self, worker: IdleWorker, record: IdleWorkerRecord, now: datetime,
    ) -> bool:
        """The worker's own hard veto, with the interval as a fallback."""
        try:
            return bool(
                worker.is_ready(now=now, last_run_at=record.last_run_at)
            )
        except Exception:
            return default_is_ready(
                worker.interval_seconds,
                now=now,
                last_run_at=record.last_run_at,
            )

    def _is_ready_legacy(
        self, worker: IdleWorker, record: IdleWorkerRecord, now: datetime,
    ) -> bool:
        """:meth:`_is_ready`, plus the interval a migrated worker gave up.

        Migrating a worker to ``demand()`` means moving the
        ``default_is_ready(self.interval_seconds, …)`` timing check out
        of ``is_ready()`` and into the probe, leaving ``is_ready()`` as
        hard vetoes only. On the demand path that is exactly right --
        the heartbeat and ``derive_min_interval_s`` floor take over. On
        this path there is no probe, so a migrated worker would be
        "ready" on every wake tick and run bounded only by the budget,
        which is the opposite of what an escape hatch is for.

        So re-impose the interval here, for migrated workers only.
        Unmigrated workers still carry their own timing check inside
        ``is_ready()`` and are untouched.
        """
        if not self._is_ready(worker, record, now):
            return False
        if not callable(getattr(worker, "demand", None)):
            return True
        return default_is_ready(
            worker.interval_seconds,
            now=now,
            last_run_at=record.last_run_at,
        )

    def _tick_legacy(self) -> None:
        """The pre-P36 tick, preserved as the escape hatch.

        Reached when ``memory.idle_worker_pressure_enabled`` is false.
        One budget, oldest-first ranking, slot 1 exempt from the fit
        check at every idle depth.
        """
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
            if not self._is_ready_legacy(worker, record, now):
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

            self._run_one(worker, record)
            ran += 1
            ran_names.append(name)
            actual_ms = record.last_duration_ms or 0.0
            budget_remaining_ms = max(0.0, budget_remaining_ms - actual_ms)

        if due_total > 0:
            tick_elapsed_ms = (time.monotonic() * 1000.0) - tick_started_ms
            queue_after = max(0, due_total - ran)
            emit = log.info if (skipped_budget > 0 or queue_after > 0) else log.debug
            emit(
                "idle_workers tick: ran=%d due=%d skipped_budget=%d "
                "queue_after=%d tick_ms=%.0f budget_ms=%d names=%s",
                ran, due_total, skipped_budget, queue_after,
                tick_elapsed_ms, self._tick_budget_ms,
                ",".join(ran_names) if ran_names else "-",
            )

    # ── demand-driven tick (P36) ─────────────────────────────────────

    def _idle_seconds(self) -> float:
        if self._idle_depth_provider is None:
            return 0.0
        try:
            return max(0.0, float(self._idle_depth_provider()))
        except Exception:
            log.debug("idle_depth_provider raised; assuming 0s", exc_info=True)
            return 0.0

    def _contention(self) -> str:
        if self._contention_provider is None:
            return CONTENTION_QUEUEING
        try:
            return str(self._contention_provider())
        except Exception:
            log.debug(
                "contention_provider raised; assuming queueing", exc_info=True,
            )
            return CONTENTION_QUEUEING

    def _probe(
        self, worker: IdleWorker, record: IdleWorkerRecord, now: datetime,
    ) -> WorkSignal | None:
        """Run a worker's ``demand()`` probe, or ``None`` for legacy workers.

        A probe whose EMA has grown past ``_PROBE_BUDGET_MS`` is skipped:
        the premise of demand-driven scheduling is that asking is much
        cheaper than doing, and a probe that violates it would turn a
        scheduling win into a per-tick tax.
        """
        demand = getattr(worker, "demand", None)
        if not callable(demand):
            return None
        if (
            record.avg_probe_ms is not None
            and record.avg_probe_ms > _PROBE_BUDGET_MS
        ):
            record.last_probe_reason = "probe_too_slow"
            return None
        started_ms = time.monotonic() * 1000.0
        try:
            signal = demand(now=now, last_run_at=record.last_run_at)
        except Exception:
            log.debug(
                "idle_worker %s demand() raised; falling back to interval",
                worker.name, exc_info=True,
            )
            record.last_probe_reason = "probe_error"
            return None
        elapsed_ms = (time.monotonic() * 1000.0) - started_ms
        record.update_after_probe(elapsed_ms)
        if elapsed_ms > _PROBE_BUDGET_MS:
            log.warning(
                "idle_worker %s demand() took %.1fms (budget %.0fms); "
                "it will drop back to interval scheduling if this persists",
                worker.name, elapsed_ms, _PROBE_BUDGET_MS,
            )
        if signal is None:
            record.last_probe_reason = "no_signal"
            return None
        record.last_probe_reason = signal.reason or None
        return signal

    def _fits_lane(
        self,
        *,
        estimate_ms: float,
        remaining_ms: float,
        first_in_lane: bool,
        tier_index: int,
        elapsed_s: float | None,
        heartbeat_s: float,
    ) -> bool:
        """Whether a worker may start given what is left of its lane.

        The pre-P36 rule exempted the first worker of every tick from
        the budget entirely, which meant a worker with a 45s average was
        admitted on every tick and ``tick_budget_ms`` bounded only slots
        two onward. Since a returning message queues behind whatever is
        running rather than cancelling it, that unbounded first run was
        the user's real worst-case wait.

        So the exemption now applies only from the ``away`` tier on,
        where a long run has room. Two escape valves keep the tightened
        rule from becoming a trap at ``just_left``:

        * A worker that has *never* run has no measured cost, and
          ``_DEFAULT_ESTIMATE_MS`` is a guess. Refusing it on that guess
          would mean never measuring it, so it could be excluded
          forever. The first run of anything is exempt.
        * Past three heartbeats, admit regardless. A user who returns
          every few minutes pins depth at ``just_left`` indefinitely,
          and without this a long worker would never see a tick it fits
          in.
        """
        if estimate_ms <= remaining_ms:
            return True
        if not first_in_lane:
            return False
        if tier_index > 0:
            return True
        if elapsed_s is None:
            return True
        return (
            heartbeat_s > 0.0
            and elapsed_s >= _FIT_ESCAPE_HEARTBEATS * heartbeat_s
        )

    def _tick_demand(self) -> None:
        now = _utcnow()
        idle_s = self._idle_seconds()
        depth_name, tier_index, depth_mult = classify_depth(
            idle_s, max_multiplier=self._depth_max_multiplier,
        )
        grade = self._contention()
        llm_mult = llm_lane_multiplier(
            grade, tier_index=tier_index, depth_multiplier=depth_mult,
        )
        lanes = {
            LANE_COMPUTE: float(self._compute_budget_ms) * depth_mult,
            LANE_LLM: float(self._tick_budget_ms) * llm_mult,
        }

        with self._lock:
            items = list(self._workers.items())

        due_total = 0
        candidates: list[
            tuple[int, float, float, str, IdleWorker, IdleWorkerRecord]
        ] = []
        for name, worker in items:
            record = self._records.get(name)
            if record is None:
                continue
            if not self._is_ready(worker, record, now):
                continue
            due_total += 1

            elapsed_s = (
                None if record.last_run_at is None
                else (now - record.last_run_at).total_seconds()
            )
            heartbeat_s = float(worker.interval_seconds)
            signal = self._probe(worker, record, now)
            verdict: Admission = evaluate_admission(
                elapsed_s=elapsed_s,
                heartbeat_s=heartbeat_s,
                min_interval_s=derive_min_interval_s(
                    heartbeat_s,
                    wake_seconds=self._wake_seconds,
                    ratio=self._min_interval_ratio,
                ),
                signal=signal,
                threshold=self._urgency_threshold,
            )
            record.last_pressure = signal.pressure if signal else None
            record.last_urgency = verdict.urgency
            record.last_admit_reason = verdict.reason
            record.last_lane = verdict.lane
            if not verdict.admit:
                continue
            lane_rank = _LANE_ORDER.index(verdict.lane)
            # Oldest-first as the tiebreaker, not the name. Staleness
            # saturates at 1.0, so once several workers are past their
            # heartbeat their urgencies tie exactly -- breaking that tie
            # alphabetically would starve everything after the first
            # name. This is the pre-P36 rotation, kept underneath
            # urgency rather than replaced by it.
            last_run_key = (
                float("-inf") if record.last_run_at is None
                else record.last_run_at.timestamp()
            )
            candidates.append(
                (lane_rank, -verdict.urgency, last_run_key, name, worker, record),
            )

        # Lane-major, urgency descending inside each lane. Compute work
        # is milliseconds, so putting it first costs the LLM lane almost
        # nothing while saving cheap workers from waiting out a
        # multi-second generation (or slipping to a later tick entirely).
        #
        # Deliberately not tie-broken by name: the sort is stable and
        # ``items`` is insertion-ordered, so equal-rank workers keep
        # registration order, which is both deterministic and the
        # pre-P36 behaviour.
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))

        ran = 0
        skipped_budget = 0
        ran_names: list[str] = []
        lane_ran = {LANE_COMPUTE: 0, LANE_LLM: 0}
        tick_started_ms = time.monotonic() * 1000.0
        max_runs = self._max_per_tick if self._max_per_tick > 0 else None
        stopped_early = False

        for lane_rank, _neg_urgency, _last_run, name, worker, record in candidates:
            if max_runs is not None and ran >= max_runs:
                skipped_budget += 1
                continue
            # The user coming back mid-tick must stop us admitting more.
            # The worker already running still finishes -- that wait is
            # what _fits_lane bounds.
            if not self._quiet():
                stopped_early = True
                break

            lane = _LANE_ORDER[lane_rank]
            estimate_ms = (
                record.avg_duration_ms
                if record.avg_duration_ms is not None
                else _DEFAULT_ESTIMATE_MS
            )
            elapsed_s = (
                None if record.last_run_at is None
                else (now - record.last_run_at).total_seconds()
            )
            if not self._fits_lane(
                estimate_ms=estimate_ms,
                remaining_ms=lanes[lane],
                first_in_lane=lane_ran[lane] == 0,
                tier_index=tier_index,
                elapsed_s=elapsed_s,
                heartbeat_s=float(worker.interval_seconds),
            ):
                skipped_budget += 1
                record.last_admit_reason = "lane_full"
                continue

            self._run_one(worker, record)
            ran += 1
            lane_ran[lane] += 1
            ran_names.append(name)
            lanes[lane] = max(0.0, lanes[lane] - (record.last_duration_ms or 0.0))

        if due_total > 0 or ran > 0:
            tick_elapsed_ms = (time.monotonic() * 1000.0) - tick_started_ms
            # A tick that ran nothing, or ran what it was supposed to, is
            # not news once a minute forever. It stays INFO when the
            # budget actually turned work away, which is the state
            # `rules/debugging.md` sends you here to diagnose; otherwise
            # DEBUG, matching that page's "raise the level to watch the
            # drain" instruction.
            emit = (
                log.info
                if (skipped_budget > 0 or stopped_early)
                else log.debug
            )
            emit(
                "idle_workers tick: ran=%d due=%d admitted=%d "
                "skipped_budget=%d tick_ms=%.0f depth=%s(%.0fs) "
                "contention=%s compute_ms=%.0f llm_ms=%.0f%s names=%s",
                ran, due_total, len(candidates), skipped_budget,
                tick_elapsed_ms, depth_name, idle_s, grade,
                float(self._compute_budget_ms) * depth_mult,
                float(self._tick_budget_ms) * llm_mult,
                " stopped_early=1" if stopped_early else "",
                ",".join(ran_names) if ran_names else "-",
            )

    def _run_one(
        self,
        worker: IdleWorker,
        record: IdleWorkerRecord,
    ) -> dict[str, Any] | None:
        # Timestamps here are always real wall-clock: ``last_run_at`` records
        # when the run *finished*, and durations come off the monotonic clock.
        # There is deliberately no injectable ``now`` -- an earlier one only
        # ever fed a start timestamp that nothing read.
        started_ms = time.monotonic() * 1000.0
        # DEBUG, not INFO: this line carries nothing the matching `run
        # done` does not, and paying one line per worker per tick for it
        # made the scheduler 39% of a real log file. `run done` is the
        # documented read (see rules/debugging.md) because it is the one
        # with the result payload.
        log.debug("idle_worker run start: %s", worker.name)
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

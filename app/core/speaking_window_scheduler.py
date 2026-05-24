"""SpeakingWindowScheduler — drains LLM-driven background jobs while Aiko speaks.

The TTS-speaking window (the time between Aiko starting to speak the previous
reply and the user replying) is dead time from the user's POV, often 5-30s
long. This scheduler treats that window as a free budget for background work
that would otherwise either (a) inflate hot-path latency, or (b) miss its
window if scheduled on a fixed timer.

Design:

- Jobs are submitted with a name, priority (lower = sooner), and an estimated
  duration. The drain pops the highest-priority job and runs it to completion
  or until the stop flag fires.
- `on_tts_state("start")` opens a window; `on_tts_state("end")` closes it
  cooperatively (sets a stop flag, gives jobs a small grace window to wrap
  up). Jobs are expected to check the flag periodically.
- `on_user_speech()` is an urgent cancel — the user just started talking, the
  background work needs to step aside immediately.
- An idle fallback drain fires after `idle_seconds` of no TTS events when the
  chat has been quiet (used for catch-up work when no one's speaking).

Threading model: a single drain thread per window keeps things sequential and
easy to reason about. Jobs run on that thread and must be re-entrancy safe
against callers that submit during their own execution.
"""
from __future__ import annotations

import dataclasses
import heapq
import itertools
import logging
import threading
import time
from collections.abc import Callable
from typing import Any


log = logging.getLogger("app.scheduler")


@dataclasses.dataclass(slots=True)
class StopFlag:
    """Cooperative cancellation token passed to each scheduled job.

    Jobs should call `is_set()` between long sub-steps (e.g. between bullet
    items in a JSON output, or before each LLM round) and bail out when it
    flips. ``urgent`` means "the user just started speaking", so jobs that
    have a salvage path (partial JSON parse, etc.) should skip salvage and
    just drop their work.
    """

    _event: threading.Event = dataclasses.field(default_factory=threading.Event)
    _urgent: threading.Event = dataclasses.field(default_factory=threading.Event)

    def is_set(self) -> bool:
        return self._event.is_set()

    def is_urgent(self) -> bool:
        return self._urgent.is_set()

    def set(self, *, urgent: bool = False) -> None:
        if urgent:
            self._urgent.set()
        self._event.set()

    def clear(self) -> None:
        self._event.clear()
        self._urgent.clear()


# Job callable receives the StopFlag so it can check for cancellation mid-run.
JobCallable = Callable[[StopFlag], None]


@dataclasses.dataclass(slots=True)
class ScheduledJob:
    """A single unit of background work the scheduler can drain.

    `priority` is the primary sort key (lower = sooner). `estimated_seconds`
    is an advisory budget the scheduler uses to decide whether a job fits
    in the remaining window. `dedupe_key` lets a caller submit the same logical
    job repeatedly without piling up duplicates (only the most recent wins).
    """

    name: str
    priority: int
    estimated_seconds: float
    callable: JobCallable
    dedupe_key: str | None = None
    enqueued_at: float = dataclasses.field(default_factory=time.monotonic)


# Internal heap entry: (priority, sequence, job). The sequence breaks ties so
# heapq doesn't have to compare ScheduledJob instances (which contain non-
# comparable callables).
_HeapEntry = tuple[int, int, ScheduledJob]


class SpeakingWindowScheduler:
    """Priority queue + drain thread tied to the TTS-speaking window.

    Public API:

    - `submit(job)` — enqueue a job; will run on the next drain.
    - `on_tts_state(event)` — `"start"` opens a window, `"end"` closes it.
    - `on_user_speech()` — urgent cancel of the in-flight job.
    - `start_idle_loop()` — daemon thread that fires a drain after
      `idle_seconds` of no TTS activity (best-effort catch-up).
    - `stop()` — shutdown for tests / app exit.
    """

    def __init__(
        self,
        *,
        speaking_window_grace_ms: int = 200,
        max_job_seconds: float = 8.0,
        idle_seconds: float = 20.0,
        is_quiet: Callable[[], bool] | None = None,
    ) -> None:
        self._grace_ms = max(0, int(speaking_window_grace_ms))
        self._max_job_seconds = max(1.0, float(max_job_seconds))
        self._idle_seconds = max(2.0, float(idle_seconds))
        self._is_quiet = is_quiet or (lambda: True)

        self._lock = threading.Lock()
        self._heap: list[_HeapEntry] = []
        # Map dedupe_key -> latest enqueue time so we can ignore stale entries
        # the heap still holds.
        self._dedupe_latest: dict[str, float] = {}
        self._counter = itertools.count()

        self._stop_flag = StopFlag()
        self._window_open = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._idle_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._last_tts_event_at = time.monotonic()
        self._stats: dict[str, int | float] = {
            "submitted": 0,
            "ran": 0,
            "cancelled": 0,
            "skipped_stale": 0,
            "windows_opened": 0,
            "windows_idle": 0,
        }
        log.info(
            "scheduler init: grace_ms=%d max_job_s=%.1f idle_s=%.1f",
            self._grace_ms, self._max_job_seconds, self._idle_seconds,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._shutdown.set()
        self._stop_flag.set(urgent=True)
        thread = self._drain_thread
        if thread is not None:
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        idle = self._idle_thread
        if idle is not None:
            try:
                idle.join(timeout=1.0)
            except Exception:
                pass
        log.info(
            "scheduler shutdown: submitted=%d ran=%d cancelled=%d skipped_stale=%d "
            "windows_opened=%d windows_idle=%d",
            int(self._stats["submitted"]), int(self._stats["ran"]),
            int(self._stats["cancelled"]), int(self._stats["skipped_stale"]),
            int(self._stats["windows_opened"]), int(self._stats["windows_idle"]),
        )

    # ── submission ────────────────────────────────────────────────────────

    def submit(self, job: ScheduledJob) -> None:
        """Enqueue ``job`` for the next drain.

        Jobs with the same ``dedupe_key`` collapse to the most recent one —
        the older entries stay on the heap but are skipped at pop time.
        """
        if self._shutdown.is_set():
            return
        with self._lock:
            seq = next(self._counter)
            heapq.heappush(self._heap, (int(job.priority), seq, job))
            if job.dedupe_key:
                self._dedupe_latest[job.dedupe_key] = job.enqueued_at
            self._stats["submitted"] = int(self._stats["submitted"]) + 1
        log.debug(
            "scheduler.submit name=%s priority=%d est=%.1fs dedupe=%s",
            job.name, job.priority, job.estimated_seconds, job.dedupe_key,
        )

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._heap)

    # ── TTS-driven drain ──────────────────────────────────────────────────

    def on_tts_state(self, event: str) -> None:
        """Open/close the speaking window based on TTS engine events."""
        self._last_tts_event_at = time.monotonic()
        if event == "start":
            self._open_window()
        elif event == "end":
            self._close_window(grace_ms=self._grace_ms)

    def on_user_speech(self) -> None:
        """User just started talking — urgent cancel of in-flight work."""
        self._close_window(grace_ms=0, urgent=True)

    def _open_window(self) -> None:
        if self._shutdown.is_set():
            return
        with self._lock:
            if self._window_open.is_set():
                # Already draining; the existing thread will pick up new jobs.
                return
            if self._drain_thread is not None and self._drain_thread.is_alive():
                # Previous drain hasn't fully exited yet; let it finish first.
                return
            self._stop_flag.clear()
            self._window_open.set()
            self._stats["windows_opened"] = int(self._stats["windows_opened"]) + 1
            thread = threading.Thread(
                target=self._drain_loop,
                name="speaking-window-drain",
                daemon=True,
            )
            self._drain_thread = thread
        thread.start()
        log.debug("scheduler.window opened")

    def _close_window(self, *, grace_ms: int, urgent: bool = False) -> None:
        if grace_ms > 0 and not urgent:
            # Soft close: let the in-flight job wrap up if it can.
            self._stop_flag.set(urgent=False)
            log.debug("scheduler.window closing soft (grace=%dms)", grace_ms)
        else:
            self._stop_flag.set(urgent=True)
            log.debug("scheduler.window closing hard urgent=%s", urgent)
        self._window_open.clear()

    # ── idle fallback ─────────────────────────────────────────────────────

    def start_idle_loop(self) -> None:
        """Spawn the catch-up drain thread (no-op if already running)."""
        if self._shutdown.is_set():
            return
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._idle_loop, name="scheduler-idle", daemon=True,
        )
        self._idle_thread = thread
        thread.start()

    def _idle_loop(self) -> None:
        while not self._shutdown.is_set():
            # Sleep in chunks so shutdown can interrupt promptly.
            for _ in range(int(self._idle_seconds)):
                if self._shutdown.is_set():
                    return
                time.sleep(1.0)
            if self._window_open.is_set():
                continue
            try:
                quiet = bool(self._is_quiet())
            except Exception:
                quiet = False
            if not quiet:
                continue
            if not self.has_pending():
                continue
            since_tts = time.monotonic() - self._last_tts_event_at
            if since_tts < self._idle_seconds:
                continue
            log.debug("scheduler.idle drain (quiet for %.1fs)", since_tts)
            self._stats["windows_idle"] = int(self._stats["windows_idle"]) + 1
            # Open a synthetic window. The drain thread will finish naturally
            # when the heap empties (no tts=end will ever arrive in idle mode).
            self._stop_flag.clear()
            self._drain_until_empty(idle=True)

    # ── core drain ────────────────────────────────────────────────────────

    def _drain_loop(self) -> None:
        try:
            self._drain_until_empty(idle=False)
        except Exception:
            log.exception("scheduler drain crashed")
        finally:
            self._window_open.clear()
            log.debug("scheduler.window drain finished")

    def _drain_until_empty(self, *, idle: bool) -> None:
        """Pop jobs and run them sequentially until the heap empties or stop fires.

        ``idle=True`` switches the exit condition: idle drains stop only when
        the heap empties or shutdown is requested (no tts=end signal will
        arrive). Non-idle drains exit when the stop flag is set OR the heap
        empties — they're tied to the speaking window.
        """
        drain_t0 = time.monotonic()
        jobs_run = 0
        ran_names: list[str] = []
        while not self._shutdown.is_set():
            if not idle and self._stop_flag.is_set() and self._stop_flag.is_urgent():
                break
            job = self._pop_next_eligible()
            if job is None:
                break
            log.debug(
                "scheduler.run name=%s priority=%d est=%.1fs idle=%s",
                job.name, job.priority, job.estimated_seconds, idle,
            )
            t0 = time.monotonic()
            try:
                job.callable(self._stop_flag)
            except Exception:
                log.exception("scheduled job %s raised", job.name)
            elapsed = time.monotonic() - t0
            self._stats["ran"] = int(self._stats["ran"]) + 1
            jobs_run += 1
            ran_names.append(job.name)
            log.debug(
                "scheduler.done name=%s elapsed=%.2fs cancelled=%s",
                job.name, elapsed, self._stop_flag.is_set(),
            )
            if not idle and self._stop_flag.is_set():
                # Soft close: this was the last job for the window.
                if self._stop_flag.is_urgent():
                    break
                # Soft stop — keep going if more jobs fit.
                # Re-clear so subsequent jobs aren't immediately cancelled.
                # Actually no: a soft close means "wrap up and stop", so we
                # break here too. Let the next window pick up remaining work.
                break

        if jobs_run > 0:
            with self._lock:
                queue_after = len(self._heap)
            elapsed_ms = (time.monotonic() - drain_t0) * 1000.0
            # Single structured summary so a `module_contains="scheduler"` tail
            # gives a readable per-window picture without the per-job churn.
            log.debug(
                "scheduler drain: jobs_run=%d elapsed_ms=%.0f queue_after=%d "
                "idle=%s names=%s",
                jobs_run, elapsed_ms, queue_after,
                "1" if idle else "0",
                ",".join(ran_names) if ran_names else "-",
            )

    def _pop_next_eligible(self) -> ScheduledJob | None:
        """Pop the highest-priority job, skipping stale dedupe entries."""
        with self._lock:
            while self._heap:
                _, _, job = heapq.heappop(self._heap)
                if job.dedupe_key:
                    latest = self._dedupe_latest.get(job.dedupe_key)
                    if latest is not None and latest > job.enqueued_at:
                        # A newer job with the same key will pop later.
                        self._stats["skipped_stale"] = int(
                            self._stats["skipped_stale"],
                        ) + 1
                        continue
                    # Clear the key so the latest one is allowed through.
                    self._dedupe_latest.pop(job.dedupe_key, None)
                return job
        return None

    # ── observability ─────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return a small dict of counters + queue depth (for /metrics)."""
        with self._lock:
            stats = dict(self._stats)
            stats["pending"] = len(self._heap)
            stats["window_open"] = self._window_open.is_set()
        return stats

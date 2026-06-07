"""One-shot task cue parking, stale-sweep, and aggregation.

A "task cue" is the in-memory record of a finished or blocked task
that Aiko hasn't naturally folded into a turn yet. Two cue kinds:

* ``task_result`` — a task reached a terminal state
  (``done`` / ``failed`` / ``cancelled``). The cue carries enough
  context to render a one-line summary into the next-turn prompt
  ("file_search "Q4 report" -- found 3 documents").
* ``task_input_needed`` — a running task is blocked waiting for an
  answer. The cue carries the handler's prompt + optional options
  list. Aiko weaves the question naturally into her next reply.

Lifecycle (sibling to K32 ``_pending_user_reactions``, but more
structured):

1. A ``BrainLoop`` handler — registered by ``SessionController`` in
   chunk 5 — calls :meth:`TaskCueStore.park` when a
   :class:`TaskResultEvent` / :class:`TaskInputNeededEvent` lands.
2. The cue sits in memory with a monotonic + wall-clock timestamp.
3. ``PromptAssembler`` calls :meth:`drain_for_render` on the next
   ``user_message`` turn; cues older than
   ``task_cue_max_age_seconds`` drop silently (one INFO line each);
   the remainder render into a T6 system block via
   :mod:`app.core.tasks.cue_render` and clear from the store.
4. Alternatively, the escalation timer calls
   :meth:`peek_for_escalation` to read parked cues without clearing,
   so a proactive turn can surface them when silence stretches.
5. Hard cap ``task_cue_max_aggregated`` per turn — excess cues stay
   parked and surface on the turn after.

Thread safety: every mutation goes through ``self._lock``.
Producers (the brain-loop consumer thread) park cues; consumers
(the prompt-assembly thread + the escalation timer thread) read +
drain. The store does **not** itself bridge between threads — it
just guarantees its own invariants hold under concurrent access.

Logging contract — see ``docs/brain-orchestration.md``:

* INFO ``task cue parked: task=<id> kind=<kind> aggregated=<n>``
* INFO ``task cue surfaced: count=<n> turn=<turn_id> aggregated=<n>``
* INFO ``task cue stale-dropped: task=<id> age_s=<f>``
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal


log = logging.getLogger("app.task_orchestrator")


# Discriminator constants — kept stable; tests + the render module
# read these. Mirror of :data:`app.core.brain.events.KIND_TASK_RESULT`
# / ``KIND_TASK_INPUT_NEEDED`` so the cue layer doesn't depend on the
# brain-events module (circular-import-friendly).
CUE_KIND_RESULT: Literal["task_result"] = "task_result"
CUE_KIND_INPUT_NEEDED: Literal["task_input_needed"] = "task_input_needed"


@dataclass(frozen=True, slots=True)
class TaskCue:
    """One parked cue.

    Frozen so it's hashable + safe to share across threads. The
    store keeps a list of these and never mutates them in place.

    ``task_id`` and ``session_key`` mirror the originating event so
    a cue can be cancelled by id when a task gets superseded or
    a session boundary clears.

    ``kind`` is one of :data:`CUE_KIND_RESULT` /
    :data:`CUE_KIND_INPUT_NEEDED`. The render module branches on
    this to pick the right sub-header.

    ``status`` is the task's terminal status for result cues
    (``done`` / ``failed`` / ``cancelled``); empty string for
    input-needed cues.

    ``title`` is the human label the task carried (the
    ``tasks.title`` column). For result cues, this is the work that
    finished; for input-needed cues, it's the work that's blocked.

    ``summary`` is the one-line content for result cues — handler-
    formatted (e.g. ``found 3 documents (notes/q4-draft.md, …)``).
    For input-needed cues, this is the handler's question (e.g.
    ``"which one? a / b / c"``).

    ``options`` is a tuple of option labels for input-needed cues
    with structured choices; ``None`` for free-form questions and
    for result cues.

    ``error`` is the failure message for ``status=failed`` cues;
    ``None`` otherwise. Render module uses this to lift failures
    into the "ran into trouble" sub-header.

    ``parked_at`` is the monotonic clock value at park time. Used
    for stale-sweep age computation.

    ``parked_at_wall`` is the corresponding wall-clock seconds-
    since-epoch. Used for human-readable log lines ("age_s=900").
    """

    task_id: str
    session_key: str
    kind: str
    parked_at: float
    parked_at_wall: float
    title: str = ""
    status: str = ""
    summary: str = ""
    options: tuple[str, ...] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CueDrainResult:
    """Outcome of one :meth:`TaskCueStore.drain_for_render` call.

    ``surfaced`` is the list of cues to render (capped at
    ``max_aggregated`` and stripped of stale entries). The store
    has already removed these from its internal list.

    ``stale_dropped`` is the list of cues that exceeded the age
    bound and were silently removed. Surfaced for tests + the
    INFO log line, but generally ignored by callers.

    ``deferred`` is the count of cues that hit the
    ``max_aggregated`` cap and stayed parked for the next turn.
    Non-zero means the next turn will also fold cues in.
    """

    surfaced: list[TaskCue] = field(default_factory=list)
    stale_dropped: list[TaskCue] = field(default_factory=list)
    deferred: int = 0


class TaskCueStore:
    """Thread-safe queue of parked cues with stale-sweep + aggregation cap.

    Sized for one ``SessionController``. Single-user installs hold
    a single store; multi-user installs hold one per user (the
    orchestration design keeps the brain loop per-session, so the
    store is too).

    Listeners (chunk 5+): pass a ``on_park`` / ``on_clear`` callable
    if you want side-effects (e.g. arming the escalation timer when
    a cue parks). Pure cue-state mutations don't need listeners.
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = 1800.0,
        max_aggregated: int = 5,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if max_aggregated <= 0:
            raise ValueError("max_aggregated must be positive")
        self._cues: list[TaskCue] = []
        self._lock = threading.Lock()
        self._max_age_seconds: float = float(max_age_seconds)
        self._max_aggregated: int = int(max_aggregated)
        self._park_count: int = 0
        self._surface_count: int = 0
        self._stale_drop_count: int = 0
        log.info(
            "task-cue-store init: max_age_s=%.0f max_aggregated=%d",
            self._max_age_seconds,
            self._max_aggregated,
        )

    # ── producer surface ─────────────────────────────────────────────

    def park(
        self,
        *,
        task_id: str,
        session_key: str,
        kind: str,
        title: str = "",
        status: str = "",
        summary: str = "",
        options: tuple[str, ...] | None = None,
        error: str | None = None,
    ) -> TaskCue:
        """Park a cue.

        Returns the newly-created :class:`TaskCue` so a caller (the
        escalation manager) can stash the id for later cancel.

        Replaces any existing cue with the same ``task_id`` —
        the latest state for a task wins, since a result cue
        should clobber an earlier input-needed cue if both fire.
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")
        if kind not in (CUE_KIND_RESULT, CUE_KIND_INPUT_NEEDED):
            raise ValueError(f"unknown cue kind: {kind!r}")
        now_mono = time.monotonic()
        now_wall = time.time()
        cue = TaskCue(
            task_id=str(task_id),
            session_key=str(session_key),
            kind=str(kind),
            parked_at=now_mono,
            parked_at_wall=now_wall,
            title=str(title),
            status=str(status),
            summary=str(summary),
            options=tuple(options) if options else None,
            error=str(error) if error is not None else None,
        )
        with self._lock:
            # Drop any prior cue for the same task id; the latest
            # state supersedes (a result clobbers an earlier
            # input-needed for the same task).
            self._cues = [c for c in self._cues if c.task_id != cue.task_id]
            self._cues.append(cue)
            count = len(self._cues)
            self._park_count += 1
        log.info(
            "task cue parked: task=%s kind=%s aggregated=%d",
            cue.task_id,
            cue.kind,
            count,
        )
        return cue

    # ── consumer surface ─────────────────────────────────────────────

    def drain_for_render(
        self, *, turn_id: str | None = None, now: float | None = None
    ) -> CueDrainResult:
        """Pop up to ``max_aggregated`` cues for rendering.

        Stale cues (older than ``max_age_seconds``) drop silently
        with one ``task cue stale-dropped:`` INFO line each. The
        remaining cues are returned in FIFO order, capped at the
        aggregation limit. Excess cues stay parked.

        ``turn_id`` is the active turn's 8-char correlation id (or
        ``None`` for a proactive surface that doesn't have one
        yet). Logged on the surfaced line for grep correlation.

        ``now`` is the monotonic time to compare against
        (defaults to ``time.monotonic()``). Tests override this to
        produce deterministic age computations without sleeps.
        """
        clock_mono = float(now) if now is not None else time.monotonic()
        clock_wall = time.time()
        with self._lock:
            keep: list[TaskCue] = []
            stale: list[TaskCue] = []
            for cue in self._cues:
                age = clock_mono - cue.parked_at
                if age > self._max_age_seconds:
                    stale.append(cue)
                else:
                    keep.append(cue)
            # Apply aggregation cap; FIFO so the oldest cue lands
            # first in the surfaced list.
            surfaced = keep[: self._max_aggregated]
            deferred = keep[self._max_aggregated :]
            self._cues = list(deferred)
            self._surface_count += len(surfaced)
            self._stale_drop_count += len(stale)
        for cue in stale:
            age_s = max(0.0, clock_wall - cue.parked_at_wall)
            log.info(
                "task cue stale-dropped: task=%s age_s=%.1f kind=%s",
                cue.task_id,
                age_s,
                cue.kind,
            )
        if surfaced:
            log.info(
                "task cue surfaced: count=%d turn=%s aggregated=%d",
                len(surfaced),
                turn_id if turn_id else "-",
                len(surfaced),
            )
        return CueDrainResult(
            surfaced=surfaced,
            stale_dropped=stale,
            deferred=len(deferred),
        )

    def peek_for_escalation(self, *, now: float | None = None) -> list[TaskCue]:
        """Read parked cues without clearing.

        Used by the escalation timer to decide whether to enqueue a
        ``ProactiveEvent``. Applies the stale-sweep side-effect so
        a long-quiet session naturally garbage-collects.

        Returns a snapshot (caller-owned list) so the iteration is
        safe across the lock boundary.
        """
        clock_mono = float(now) if now is not None else time.monotonic()
        clock_wall = time.time()
        with self._lock:
            keep: list[TaskCue] = []
            stale: list[TaskCue] = []
            for cue in self._cues:
                age = clock_mono - cue.parked_at
                if age > self._max_age_seconds:
                    stale.append(cue)
                else:
                    keep.append(cue)
            self._cues = list(keep)
            self._stale_drop_count += len(stale)
            snapshot = list(keep)
        for cue in stale:
            age_s = max(0.0, clock_wall - cue.parked_at_wall)
            log.info(
                "task cue stale-dropped: task=%s age_s=%.1f kind=%s",
                cue.task_id,
                age_s,
                cue.kind,
            )
        return snapshot

    def clear(self, task_id: str) -> bool:
        """Remove the cue for a specific task id, if any.

        Used by the escalation manager when a proactive turn folds
        the cue in (so it doesn't double-fire on the next turn),
        and by chunk-5 wiring when a task gets superseded.

        Returns ``True`` if a cue was removed, ``False`` otherwise.
        """
        with self._lock:
            before = len(self._cues)
            self._cues = [c for c in self._cues if c.task_id != str(task_id)]
            removed = before - len(self._cues)
        return removed > 0

    def clear_all(self) -> int:
        """Wipe every parked cue. Returns the count dropped.

        Used in tests + on session reset (chunk 5+).
        """
        with self._lock:
            count = len(self._cues)
            self._cues = []
        return count

    # ── debug surface ────────────────────────────────────────────────

    def pending_count(self) -> int:
        """Number of cues currently parked (live, not stale-swept)."""
        with self._lock:
            return len(self._cues)

    def snapshot(self) -> list[TaskCue]:
        """Read-only snapshot of every parked cue.

        Used by MCP debug tools + tests. Stale-sweep is not applied
        on this path — it's a true point-in-time view of the
        internal state including expired entries (so a test can
        verify the stale-sweep ran exactly as expected on the
        next drain / peek).
        """
        with self._lock:
            return list(self._cues)

    def metrics_snapshot(self) -> dict[str, int | float]:
        """Cumulative counters since construction."""
        with self._lock:
            return {
                "park_count": self._park_count,
                "surface_count": self._surface_count,
                "stale_drop_count": self._stale_drop_count,
                "pending": len(self._cues),
                "max_age_seconds": self._max_age_seconds,
                "max_aggregated": self._max_aggregated,
            }


__all__ = [
    "TaskCue",
    "CueDrainResult",
    "TaskCueStore",
    "CUE_KIND_RESULT",
    "CUE_KIND_INPUT_NEEDED",
]

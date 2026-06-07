"""Escalation timer: turn parked cues into proactive turns when silence stretches.

A parked :class:`TaskCue` sits silently on
:class:`TaskCueStore`. The cue surfaces naturally on Aiko's next
``user_message`` turn via :func:`cue_render.render_cue_block`. But
if no ``user_message`` arrives — the user walked away, went quiet,
got distracted — Aiko should eventually speak up unprompted ("hey,
the search I started 45 seconds ago found nothing — want me to
broaden it?").

This module owns that escalation logic. One
:class:`TaskEscalationManager` per :class:`SessionController` (chunk
5 wiring), holding one :class:`threading.Timer` per parked
``task_id``. When a cue parks:

1. The wiring (chunk 5) calls :meth:`arm` with the cue + the
   appropriate silence window — short for
   ``task_input_needed`` cues
   (:attr:`EscalationConfig.input_needed_after_seconds`), longer
   for ``task_result`` cues
   (:attr:`EscalationConfig.completion_after_seconds`).
2. A ``threading.Timer`` ticks down. If a
   ``user_message`` arrives first, the wiring calls
   :meth:`cancel_for_task` (the cue surfaced naturally — no need
   to escalate). If the cue gets superseded (replaced by a newer
   cue for the same task id), :meth:`arm` cancels the old timer
   and installs a fresh one.
3. When the timer fires, the manager checks three preconditions
   before enqueuing a :class:`ProactiveEvent`:

   * The cue is STILL parked
     (``cue_store.peek_for_escalation`` returns it).
   * The free-to-speak gate is clear
     (``free_to_speak_predicate`` returns ``True``).
   * No ``user_message`` has arrived since the cue parked
     (``last_user_message_at`` is older than ``cue.parked_at``).

4. If any precondition fails, the manager re-arms the timer for a
   shorter retry window (1.0 s by default) up to a cap so a
   long-blocked gate doesn't pin the timer thread forever.

5. On successful fire, the manager enqueues a ``ProactiveEvent``
   with ``source="task_escalation"`` and the parked cue's id in
   ``parked_cue_ids``. The chunk-5 ``ProactiveDirector`` handler
   reads the cues and renders them into the proactive turn's
   prompt.

The manager does NOT clear the cue itself — that's the
prompt-assembly layer's responsibility (on the next turn,
``drain_for_render`` consumes it). This separation means a flapping
timer can't double-fire while the cue is sitting on the store.

Thread safety: every mutation to ``_timers`` goes through
``self._lock``. Timer callbacks run on Python's daemon timer
thread; they acquire the lock for the check + dispatch path.

Logging contract — see ``docs/brain-orchestration.md``:

* INFO ``brain-loop escalated: task=<id> silence_s=<f> cue_kind=<kind>``
  — fired when a proactive event was enqueued.
* DEBUG ``escalation arm: task=<id> after_seconds=<f>``
* DEBUG ``escalation cancel: task=<id> reason=<reason>``
* DEBUG ``escalation rearm: task=<id> reason=<reason>``
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.tasks.task_cue_store import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    TaskCue,
    TaskCueStore,
)


log = logging.getLogger("app.brain_loop")


# Type aliases for the wiring hooks. Kept as plain Callables (not
# Protocols) because the contract is one-method-each and the wiring
# code passes bound methods directly.
FreeToSpeakPredicate = Callable[[], bool]
LastUserMessageAt = Callable[[], float]  # monotonic seconds; -inf if never
EnqueueProactive = Callable[[str, tuple[str, ...]], None]
# ``EnqueueProactive`` takes (session_key, parked_cue_ids) and
# enqueues a ProactiveEvent. Kept as a callable so the manager
# doesn't import BrainLoop / events directly — chunk 5 wires the
# real handler.


# Default retry interval when a fire was blocked by gate / silence
# preconditions. Picked to be short enough that a 5-second TTS
# clip is followed by an immediate escalation when it ends, but
# long enough that a flapping predicate doesn't burn the timer
# thread on a tight loop.
_DEFAULT_RETRY_SECONDS: float = 1.0
# Cap on retry attempts before giving up. A cue that's still
# parked + still gate-blocked + still racing user input after 60s
# of polling is almost certainly stuck; better to log and drop
# than to leak a timer.
_DEFAULT_RETRY_LIMIT: int = 60


@dataclass(frozen=True, slots=True)
class EscalationConfig:
    """Tunables for the escalation timer.

    Mirrors three settings fields:

    * ``completion_after_seconds`` →
      ``agent.task_completion_proactive_after_seconds`` (default
      45).
    * ``input_needed_after_seconds`` →
      ``agent.task_input_needed_proactive_after_seconds`` (default
      20, shorter because a blocked task is more pressing).
    * ``retry_seconds`` — internal poll cadence when a fire was
      pre-condition-blocked. Not exposed as a setting; tests
      override.
    * ``retry_limit`` — internal poll attempt cap. Not exposed as
      a setting; tests override.
    """

    completion_after_seconds: float = 45.0
    input_needed_after_seconds: float = 20.0
    retry_seconds: float = _DEFAULT_RETRY_SECONDS
    retry_limit: int = _DEFAULT_RETRY_LIMIT

    def window_for_kind(self, kind: str) -> float:
        """The first-fire delay for a given cue kind.

        Unknown kinds fall back to ``completion_after_seconds`` —
        the conservative default (longer wait).
        """
        if kind == CUE_KIND_INPUT_NEEDED:
            return float(self.input_needed_after_seconds)
        if kind == CUE_KIND_RESULT:
            return float(self.completion_after_seconds)
        return float(self.completion_after_seconds)


@dataclass(slots=True)
class _ActiveEscalation:
    """In-memory bookkeeping for one armed timer.

    ``timer`` is the live ``threading.Timer`` instance.
    ``cue_kind`` is captured at arm time so retries don't need to
    re-read the cue store. ``attempts`` increments on every retry;
    once it exceeds ``retry_limit`` the manager logs WARNING and
    drops.
    """

    timer: threading.Timer
    cue_kind: str
    armed_at: float
    attempts: int = 0
    cancelled: bool = field(default=False, init=False)


class TaskEscalationManager:
    """Per-session manager for cue-escalation timers.

    Construct one alongside the :class:`TaskCueStore` and the
    :class:`BrainLoop`; chunk-5 wiring registers
    :meth:`arm` as a side-effect from the brain-loop's
    ``task_result`` / ``task_input_needed`` handlers, and
    :meth:`cancel_for_task` from the post-turn cue-drain.

    The manager owns no event-loop thread of its own — every fire
    runs on Python's stdlib timer thread (one per ``Timer`` until
    it fires or gets cancelled). On shutdown,
    :meth:`shutdown` cancels every outstanding timer and the
    manager is permanently disarmed (further :meth:`arm` calls
    silently drop).
    """

    def __init__(
        self,
        *,
        cue_store: TaskCueStore,
        free_to_speak: FreeToSpeakPredicate,
        last_user_message_at: LastUserMessageAt,
        enqueue_proactive: EnqueueProactive,
        config: EscalationConfig | None = None,
    ) -> None:
        self._cue_store = cue_store
        self._free_to_speak = free_to_speak
        self._last_user_message_at = last_user_message_at
        self._enqueue_proactive = enqueue_proactive
        self._config: EscalationConfig = (
            config if config is not None else EscalationConfig()
        )
        self._timers: dict[str, _ActiveEscalation] = {}
        self._lock = threading.Lock()
        self._shutdown: bool = False
        log.info(
            "task-escalation init: completion_after_s=%.1f "
            "input_needed_after_s=%.1f",
            self._config.completion_after_seconds,
            self._config.input_needed_after_seconds,
        )

    # ── public surface ───────────────────────────────────────────────

    def arm(self, cue: TaskCue, *, after_seconds: float | None = None) -> None:
        """Arm an escalation timer for the given parked cue.

        If a timer for the same ``task_id`` already exists, cancels
        it first and installs a fresh one — the newer cue's window
        wins. Idempotent on the timer state.

        ``after_seconds`` overrides the kind-derived first-fire delay.
        The wiring passes a near-zero value for ``reply_when_done``
        tasks so they surface as a proactive reply the moment the
        free-to-speak gate clears (the duration-hybrid "slow" half),
        rather than waiting out the full completion window.

        Silent no-op after :meth:`shutdown` so a late park doesn't
        leak a timer thread past process shutdown.
        """
        if not cue.task_id:
            raise ValueError("cue.task_id must be non-empty")
        if after_seconds is not None:
            after_s = max(0.0, float(after_seconds))
        else:
            after_s = self._config.window_for_kind(cue.kind)
        with self._lock:
            if self._shutdown:
                log.debug(
                    "escalation arm dropped (shutdown): task=%s",
                    cue.task_id,
                )
                return
            existing = self._timers.pop(cue.task_id, None)
            if existing is not None:
                existing.cancelled = True
                existing.timer.cancel()
            timer = threading.Timer(
                after_s,
                self._on_fire,
                args=(cue.task_id, cue.session_key, cue.kind),
            )
            timer.daemon = True
            self._timers[cue.task_id] = _ActiveEscalation(
                timer=timer,
                cue_kind=cue.kind,
                armed_at=time.monotonic(),
            )
            timer.start()
        log.debug(
            "escalation arm: task=%s after_seconds=%.1f kind=%s",
            cue.task_id,
            after_s,
            cue.kind,
        )

    def cancel_for_task(self, task_id: str, *, reason: str = "user_input") -> bool:
        """Cancel the escalation timer for a given task id, if any.

        Called by the cue-drain path (chunk 5) on every
        ``user_message`` turn so a cue that just surfaced
        naturally doesn't also escalate. Also called when a task
        is superseded by a fresher state.

        Returns ``True`` if a timer was cancelled, ``False`` if
        nothing was armed.
        """
        with self._lock:
            entry = self._timers.pop(str(task_id), None)
            if entry is None:
                return False
            entry.cancelled = True
            entry.timer.cancel()
        log.debug(
            "escalation cancel: task=%s reason=%s", task_id, reason,
        )
        return True

    def cancel_all(self, *, reason: str = "shutdown") -> int:
        """Cancel every outstanding timer.

        Returns the count cancelled. Used by :meth:`shutdown` and
        in tests for a clean reset between scenarios.
        """
        with self._lock:
            entries = list(self._timers.items())
            self._timers.clear()
        for task_id, entry in entries:
            entry.cancelled = True
            entry.timer.cancel()
            log.debug(
                "escalation cancel: task=%s reason=%s",
                task_id,
                reason,
            )
        return len(entries)

    def shutdown(self) -> None:
        """Disarm every timer and refuse further arms.

        Idempotent. Safe to call from any thread. Process exit
        will eventually clean up daemon timer threads anyway, but
        an explicit shutdown is preferred so the WARNING-on-leak
        invariant in tests stays clean.
        """
        with self._lock:
            self._shutdown = True
        self.cancel_all(reason="shutdown")

    # ── debug surface ────────────────────────────────────────────────

    def pending_count(self) -> int:
        """Number of timers currently armed."""
        with self._lock:
            return len(self._timers)

    def snapshot(self) -> list[tuple[str, str, float]]:
        """``(task_id, cue_kind, age_seconds)`` triples for every armed timer.

        Used by MCP debug + tests. The ``age_seconds`` value is the
        wall-clock time since the timer was armed (i.e. how long
        the cue has been waiting for its first fire window).
        """
        now = time.monotonic()
        with self._lock:
            return [
                (tid, entry.cue_kind, now - entry.armed_at)
                for tid, entry in self._timers.items()
            ]

    # ── internal: fire path ──────────────────────────────────────────

    def _on_fire(self, task_id: str, session_key: str, cue_kind: str) -> None:
        """Timer callback. Runs on the stdlib timer thread.

        Checks the three preconditions and either fires (enqueue
        ``ProactiveEvent``) or re-arms with the retry interval.
        """
        with self._lock:
            entry = self._timers.get(task_id)
            if entry is None or entry.cancelled:
                # Cancelled or replaced between schedule + fire —
                # silent drop.
                return
            if self._shutdown:
                return
        # Read precondition values OUTSIDE the lock to keep the lock
        # window tight (the predicates might do work — e.g. read
        # SessionController flags — and we don't want to hold the
        # timer-dict lock during that).
        cues = self._cue_store.peek_for_escalation()
        still_parked = any(c.task_id == task_id for c in cues)
        if not still_parked:
            # Cue was already surfaced by a natural turn — clean
            # up the timer entry silently.
            with self._lock:
                self._timers.pop(task_id, None)
            log.debug(
                "escalation cancel: task=%s reason=cue_already_cleared",
                task_id,
            )
            return
        try:
            free = bool(self._free_to_speak())
        except Exception as exc:
            # Predicate raised; treat as gate-closed and retry.
            log.exception(
                "escalation free_to_speak raised: task=%s exc=%r",
                task_id,
                exc,
            )
            free = False
        last_user_at = -float("inf")
        try:
            last_user_at = float(self._last_user_message_at())
        except Exception as exc:
            log.exception(
                "escalation last_user_message_at raised: task=%s exc=%r",
                task_id,
                exc,
            )
        # The cue's parked_at is monotonic; if last_user_message_at
        # is more recent than that, the user already talked since
        # the cue parked — the cue will surface naturally on that
        # turn, no need to escalate.
        cue_parked_at = next(
            (c.parked_at for c in cues if c.task_id == task_id), 0.0
        )
        user_spoke_recently = last_user_at >= cue_parked_at
        if not free or user_spoke_recently:
            self._rearm(task_id, session_key, cue_kind, free, user_spoke_recently)
            return
        # All preconditions clear — fire.
        silence_s = max(0.0, time.monotonic() - cue_parked_at)
        with self._lock:
            self._timers.pop(task_id, None)
        try:
            self._enqueue_proactive(session_key, (task_id,))
        except Exception as exc:
            log.exception(
                "escalation enqueue raised: task=%s exc=%r",
                task_id,
                exc,
            )
            return
        log.info(
            "brain-loop escalated: task=%s silence_s=%.1f cue_kind=%s",
            task_id,
            silence_s,
            cue_kind,
        )

    def _rearm(
        self,
        task_id: str,
        session_key: str,
        cue_kind: str,
        gate_clear: bool,
        user_spoke_recently: bool,
    ) -> None:
        """Re-arm a short retry timer when a fire was preempted.

        Reasons for re-arm:

        * Gate closed (``not gate_clear``) — likely TTS mid-play or
          a turn in flight; retry shortly so we fire the moment the
          gate clears.
        * User spoke recently — the natural-turn path will surface
          the cue; retry to confirm the cue cleared (it should
          have, but this is the safety belt).

        Bumps ``attempts`` and gives up after ``retry_limit``.
        """
        with self._lock:
            entry = self._timers.get(task_id)
            if entry is None or entry.cancelled or self._shutdown:
                return
            entry.attempts += 1
            if entry.attempts > self._config.retry_limit:
                # Give up — the timer thread has been polling for
                # ages and conditions never cleared. Drop the
                # entry so the cue can still be surfaced later by
                # a real user message (the cue store still has
                # the cue parked).
                self._timers.pop(task_id, None)
                log.warning(
                    "escalation give-up: task=%s attempts=%d kind=%s",
                    task_id,
                    entry.attempts,
                    cue_kind,
                )
                return
            timer = threading.Timer(
                self._config.retry_seconds,
                self._on_fire,
                args=(task_id, session_key, cue_kind),
            )
            timer.daemon = True
            # Replace the (now-fired) timer with the retry timer.
            # ``armed_at`` stays on the original arm so silence_s
            # in the eventual fire-log is honest.
            entry.timer = timer
            entry.cancelled = False
            timer.start()
        reason = (
            "user_spoke_recently"
            if user_spoke_recently
            else "gate_closed"
        )
        log.debug(
            "escalation rearm: task=%s reason=%s attempts=%d",
            task_id,
            reason,
            entry.attempts,
        )


__all__ = [
    "EscalationConfig",
    "TaskEscalationManager",
    "FreeToSpeakPredicate",
    "LastUserMessageAt",
    "EnqueueProactive",
]

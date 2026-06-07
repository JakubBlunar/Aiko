"""Brain orchestration loop — chunk 3.

The :class:`BrainLoop` is the single consumer of
:class:`BrainEventQueue`. Chunk 1 shipped the skeleton (handler
registry, ``start()``/``stop()`` plumbing). This file replaces the
inert ``start()`` body with the real daemon-thread consumer, the
free-to-speak gate, deferred re-park, and exception isolation.

Consumer model
--------------

Single daemon thread named ``brain-loop``. Loop body:

1. **Drain deferred first.** If anything sits in ``_deferred`` and the
   gate is now clear, dispatch those FIFO before pulling from the
   queue. Deferred events were already gate-rejected once — they
   earned their head-of-line spot. Each deferred record tracks
   ``first_deferred_at`` so the ``gate_waited_ms`` field on the
   dispatched-INFO line is accurate.

2. **Pop one event** off the queue with a short timeout so the loop
   wakes regularly enough to retry deferred items even when no new
   producer fires. The default timeout is
   ``brain_loop_deferred_grace_ms`` (100 ms by config), which is the
   cheapest "tick" we can run without burning CPU.

3. **Gate check.** Events whose kind sits in :data:`_GATED_KINDS`
   pass through ``free_to_speak`` first. If the gate is closed, the
   event lands on ``_deferred`` with the current monotonic time and
   the loop emits one ``brain-loop deferred:`` INFO line. The
   ``user_message`` kind explicitly bypasses the gate — barge-in is
   real intent, and TurnRunner handles the race against the
   in-flight turn internally.

4. **Route to handler.** Look up by ``event.kind`` in the handler
   registry. Missing handler → one WARNING and drop (events without a
   home are a developer bug — phase 1 ships handlers for every kind
   that's actually emitted). Handler raised → one ERROR with the
   traceback, loop continues. Handler succeeded → one
   ``brain-loop dispatched:`` INFO line carrying ``kind=``,
   ``route=``, ``elapsed_ms=``, ``gate_waited_ms=``.

5. **Cue parking + escalation** belong on the consumer side (the
   handlers registered by :class:`SessionController` in chunk 4+).
   The loop is a pure dispatcher — it does not own ``_pending_task_cues``
   nor the escalation timer.

The loop does **not** restart itself on a handler crash because the
exception is caught + logged before it can propagate. The thread
itself outlives every individual dispatch.

Free-to-speak gate
------------------

A nullary callable returning ``bool``. Provided at construction time
or via :meth:`set_free_to_speak` (so wiring code can hot-swap the
predicate without rebuilding the loop). Default is "always True" —
useful for unit tests that don't care about turn-in-flight semantics.

When the gate is closed:

* ``user_message`` → still dispatches (barge-in).
* ``task_progress`` → still dispatches (UI-only, never blocks).
* ``state_sync`` → still dispatches (no LLM call).
* ``task_input_needed`` / ``task_result`` → still dispatches because
  the handler for these is a cue-park, which is non-speech.
* ``proactive`` / ``speaking_window_job`` / ``maintenance_due`` →
  deferred.

Stop semantics
--------------

``stop(timeout=…)`` is idempotent and safe to call from any thread:

1. Set ``_running=False`` so the consumer breaks out of its dispatch
   inner-block after the current dispatch completes.
2. ``self._queue.close()`` — unblocks the ``get(timeout=…)`` wait so
   the consumer doesn't sit idle waiting for the next event.
3. Join the consumer thread up to ``timeout`` seconds. If it doesn't
   exit (a runaway handler), log a WARNING and let the daemon-thread
   die at process exit.
4. Log a ``brain-loop stop:`` line with ``drained=`` (queued count
   left undispatched) and ``deferred=`` (items still gate-blocked).

Logging contract — see ``docs/brain-orchestration.md`` *Logging*:

* INFO ``brain-loop init: handlers=<n>``
* INFO ``brain-loop start: consumer_active=True``
* INFO ``brain-loop stop: drained=<n> deferred=<n> total_dispatched=<n>``
* INFO ``brain-loop dispatched: kind=<kind> route=<handler> elapsed_ms=<f> gate_waited_ms=<f>``
* INFO ``brain-loop deferred: kind=<kind> reason=<reason> deferred_count=<n>``
* WARNING ``brain-loop no handler: kind=<kind>``
* ERROR ``brain-loop handler error: kind=<kind> exc=<repr>``
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from app.core.brain.events import (
    KIND_MAINTENANCE_DUE,
    KIND_PROACTIVE,
    KIND_SPEAKING_WINDOW_JOB,
)
from app.core.brain.queue import BrainEventQueue


log = logging.getLogger("app.brain_loop")


EventHandler = Callable[[object], None]
FreeToSpeakPredicate = Callable[[], bool]


# ── gate policy ─────────────────────────────────────────────────────
#
# Kinds in :data:`_GATED_KINDS` wait behind the free-to-speak
# predicate. Every other kind dispatches the moment the consumer pops
# it off the queue. Source of truth — mirrors the doc's event-taxonomy
# table. Adding a new event kind that needs gating means appending it
# here; new non-gated kinds are zero-config.
_GATED_KINDS: frozenset[str] = frozenset(
    (KIND_PROACTIVE, KIND_SPEAKING_WINDOW_JOB, KIND_MAINTENANCE_DUE)
)


# Default queue poll interval. Small so the loop can retry deferred
# events promptly when the gate clears, but not so small that an idle
# session burns CPU on no-op wakes. Matches the default for
# ``agent.brain_loop_deferred_grace_ms``.
_DEFAULT_POLL_SECONDS: float = 0.1


@dataclass(slots=True)
class _DeferredEntry:
    """Event sitting in the deferred lane after a gate rejection.

    ``first_deferred_at`` is the monotonic clock value at the moment
    the event first failed the gate. Used to compute
    ``gate_waited_ms`` on the eventual dispatch INFO line.
    ``last_reason`` is the gate-state at the time of the most recent
    deferral, so re-deferred events get an updated reason without
    forgetting how long they've been waiting.
    """

    event: object
    first_deferred_at: float
    last_reason: str


def _kind_of(event: object) -> str:
    """Read the discriminator off a frozen event.

    Defensive — the events ship with ``ClassVar[str]`` discriminators,
    but a producer that somehow ships a raw object should still log
    cleanly rather than crash the loop.
    """
    return str(getattr(event, "kind", "?"))


class BrainLoop:
    """Single-consumer event loop with a free-to-speak gate.

    Construct one per :class:`SessionController`. Producers call
    :meth:`enqueue` (or push to ``loop.queue`` directly); the daemon
    thread launched by :meth:`start` drains the queue and routes each
    event through its registered handler.

    Thread safety: ``register_handler`` / ``enqueue`` /
    ``set_free_to_speak`` / ``start`` / ``stop`` / ``is_running``
    are all safe to call from any thread. Handler callables run on
    the consumer thread.
    """

    def __init__(
        self,
        queue: BrainEventQueue | None = None,
        *,
        free_to_speak: FreeToSpeakPredicate | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_SECONDS,
        gated_kinds: frozenset[str] = _GATED_KINDS,
    ) -> None:
        """Construct the loop.

        ``queue``: shared :class:`BrainEventQueue`. Defaults to a
        fresh one so tests can spin a loop up without arguments.

        ``free_to_speak``: nullary callable returning ``True`` when
        gated events may dispatch. Defaults to "always True" — fine
        for unit tests that don't model turn-in-flight semantics.
        Production wiring (chunk 4) passes a predicate that returns
        ``not (turn_in_progress or tts_active)``.

        ``poll_interval_seconds``: queue ``get`` timeout. The loop
        wakes this often even when no new event arrived, so deferred
        items get retried promptly when the gate clears. 100 ms is
        the doc's documented default.

        ``gated_kinds``: which event kinds wait behind the gate.
        Tests can override to exercise edge cases (e.g. gate
        every kind to stress the deferred path).
        """
        self._queue: BrainEventQueue = (
            queue if queue is not None else BrainEventQueue()
        )
        self._handlers: dict[str, EventHandler] = {}
        self._handler_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._gated_kinds: frozenset[str] = frozenset(gated_kinds)
        self._free_to_speak: FreeToSpeakPredicate = (
            free_to_speak if free_to_speak is not None else (lambda: True)
        )
        self._poll_interval: float = max(0.001, float(poll_interval_seconds))
        self._deferred: list[_DeferredEntry] = []
        self._dispatched_count: int = 0
        self._deferred_count: int = 0
        self._error_count: int = 0
        log.info("brain-loop init: handlers=0")

    # ── public surface ───────────────────────────────────────────────

    @property
    def queue(self) -> BrainEventQueue:
        """The underlying event queue. Producers may call ``put`` directly."""
        return self._queue

    def register_handler(self, kind: str, handler: EventHandler) -> None:
        """Register a handler for an event kind.

        ``kind`` must match the ``kind`` ClassVar of one of the
        concrete event classes in :mod:`app.core.brain.events`.
        Re-registering the same kind overwrites — the loop has one
        canonical owner per kind (e.g. ``TurnRunner.run`` for
        ``user_message``). Re-registration is logged at INFO so a
        late-binding wiring change is visible in the boot log.
        """
        if not kind:
            raise ValueError("kind must be a non-empty string")
        with self._handler_lock:
            previous = self._handlers.get(str(kind))
            self._handlers[str(kind)] = handler
            handlers_after = len(self._handlers)
        if previous is None:
            log.info(
                "brain-loop register: kind=%s handlers=%d",
                kind,
                handlers_after,
            )
        else:
            log.info(
                "brain-loop register replaced: kind=%s handlers=%d",
                kind,
                handlers_after,
            )

    def handler_for(self, kind: str) -> EventHandler | None:
        """Lookup helper. Returns ``None`` for unknown kinds."""
        with self._handler_lock:
            return self._handlers.get(str(kind))

    def list_handlers(self) -> list[str]:
        """Snapshot of registered handler kinds. Used by MCP debug."""
        with self._handler_lock:
            return sorted(self._handlers.keys())

    def set_free_to_speak(self, predicate: FreeToSpeakPredicate) -> None:
        """Replace the gate predicate at runtime.

        Wiring code (chunk 4) sets this once after
        :class:`SessionController` has its ``turn_in_progress`` /
        ``tts_active`` flags in place. Tests use it to toggle the
        gate between fixture phases.
        """
        if predicate is None:
            raise ValueError("predicate must be a non-empty callable")
        self._free_to_speak = predicate

    def enqueue(self, event: object) -> None:
        """Pass-through to :meth:`BrainEventQueue.put`.

        Producers can call either ``loop.enqueue(event)`` or
        ``loop.queue.put(event)``. ``enqueue`` is the canonical
        surface so the loop can later add side-effects (metrics,
        dedup) without touching every producer.
        """
        self._queue.put(event)  # type: ignore[arg-type]

    def start(self) -> None:
        """Spawn the daemon consumer thread.

        Idempotent — a second ``start()`` while running is a WARNING
        no-op, not a crash. Safe to call before any handler is
        registered; the consumer will pop events and emit
        ``brain-loop no handler:`` until handlers wire up.
        """
        if self._running:
            log.warning("brain-loop start ignored: already running")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._consume,
            name="brain-loop",
            daemon=True,
        )
        self._thread.start()
        log.info("brain-loop start: consumer_active=True")

    def stop(self, *, timeout: float = 1.5) -> None:
        """Stop the consumer + close the queue.

        Sets ``_running=False``, closes the queue (so the consumer
        wakes from any pending ``get``), and joins the daemon thread
        for up to ``timeout`` seconds. If the join times out (e.g. a
        handler is stuck), a WARNING fires and the thread is left to
        die at process exit.

        Idempotent: a second ``stop()`` is silently ignored. Safe
        to call from any thread.
        """
        if not self._running:
            return
        self._running = False
        drained = self._queue.depth()
        deferred_count = len(self._deferred)
        dispatched = self._dispatched_count
        self._queue.close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
            if thread.is_alive():
                log.warning(
                    "brain-loop stop: thread did not exit within %.2fs",
                    timeout,
                )
        log.info(
            "brain-loop stop: drained=%d deferred=%d total_dispatched=%d",
            drained,
            deferred_count,
            dispatched,
        )

    def is_running(self) -> bool:
        """``True`` between :meth:`start` and :meth:`stop`."""
        return self._running

    # ── debug surface ────────────────────────────────────────────────

    def pending_deferred_count(self) -> int:
        """Number of events currently sitting in the deferred lane.

        Used by ``get_brain_loop_state`` MCP tool. Returns 0 on a
        healthy free-to-speak session.
        """
        return len(self._deferred)

    def deferred_snapshot(self) -> list[tuple[str, float, str]]:
        """Read-only view of the deferred queue.

        Each tuple is ``(kind, first_deferred_at, last_reason)``,
        ordered FIFO. Plain tuples (not events) so the snapshot is
        cheap + serialisable for MCP debug.
        """
        return [
            (_kind_of(e.event), e.first_deferred_at, e.last_reason)
            for e in self._deferred
        ]

    def metrics_snapshot(self) -> dict[str, int | float]:
        """Cumulative counters since process start.

        ``dispatched`` / ``deferred`` / ``errors`` are simple counters;
        ``queue_depth`` and ``deferred_count`` are live gauges.
        """
        return {
            "dispatched": self._dispatched_count,
            "deferred": self._deferred_count,
            "errors": self._error_count,
            "queue_depth": self._queue.depth(),
            "deferred_count": len(self._deferred),
        }

    # ── consumer thread body ─────────────────────────────────────────

    def _consume(self) -> None:
        """Daemon-thread loop. One iteration = drain-then-pop-then-dispatch.

        Runs until :meth:`stop` flips ``_running=False`` AND the
        queue is closed AND drained. Exceptions inside the loop body
        are caught and logged — the consumer survives any single
        bad iteration.
        """
        try:
            while self._running:
                # Phase 1: retry deferred work first if the gate cleared.
                self._drain_deferred_if_ready()
                # Phase 2: pop one new event with a short timeout so
                # deferred items get reconsidered on idle ticks.
                try:
                    event = self._queue.get(timeout=self._poll_interval)
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception(
                        "brain-loop queue.get raised: exc=%r", exc
                    )
                    continue
                if event is None:
                    # Timeout or closed; continue to top so deferred
                    # work + running check fire again. The next
                    # `while` check exits if stop() fired.
                    if self._queue.is_closed():
                        break
                    continue
                self._handle_event(event, first_deferred_at=None)
        except Exception:
            # Last-ditch — should be unreachable because every inner
            # block catches its own. Log and exit so the thread dies
            # cleanly rather than spinning.
            log.error(
                "brain-loop consumer crashed: %s", traceback.format_exc()
            )

    def _handle_event(
        self, event: object, *, first_deferred_at: float | None
    ) -> None:
        """Apply gate + route a single event.

        ``first_deferred_at=None`` means the event came straight off
        the queue (so ``gate_waited_ms=0``). A non-None value is the
        monotonic time at which the event first failed the gate;
        used to compute ``gate_waited_ms`` for the dispatched log line.

        A re-deferral preserves the original ``first_deferred_at`` so
        a long-blocked event reports the *total* wait time at final
        dispatch, not the duration of the most recent re-attempt.
        """
        kind = _kind_of(event)
        if kind in self._gated_kinds:
            free = False
            try:
                free = bool(self._free_to_speak())
            except Exception as exc:
                # If the predicate raises, fail-closed — defer rather
                # than risk speaking over an in-flight turn.
                log.exception(
                    "brain-loop free_to_speak predicate raised: exc=%r", exc
                )
                free = False
            if not free:
                reason = self._gate_reason()
                self._defer_event(
                    event,
                    reason=reason,
                    first_deferred_at=first_deferred_at,
                )
                return
        gate_waited_ms = 0.0
        if first_deferred_at is not None:
            gate_waited_ms = max(
                0.0, (time.monotonic() - first_deferred_at) * 1000.0
            )
        self._dispatch(event, kind=kind, gate_waited_ms=gate_waited_ms)

    def _dispatch(
        self, event: object, *, kind: str, gate_waited_ms: float
    ) -> None:
        """Look up the handler + invoke it under exception isolation.

        Records elapsed_ms even for handlers that raise (cost of a
        crash counts toward latency budget too). Updates
        ``_dispatched_count`` only on successful return.
        """
        with self._handler_lock:
            handler = self._handlers.get(kind)
        if handler is None:
            log.warning("brain-loop no handler: kind=%s", kind)
            return
        route_name = getattr(handler, "__name__", None) or repr(handler)
        t0 = time.monotonic()
        try:
            handler(event)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self._dispatched_count += 1
            log.info(
                "brain-loop dispatched: kind=%s route=%s elapsed_ms=%.1f "
                "gate_waited_ms=%.1f",
                kind,
                route_name,
                elapsed_ms,
                gate_waited_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self._error_count += 1
            log.error(
                "brain-loop handler error: kind=%s route=%s elapsed_ms=%.1f "
                "exc=%r\n%s",
                kind,
                route_name,
                elapsed_ms,
                exc,
                traceback.format_exc(),
            )

    def _defer_event(
        self,
        event: object,
        *,
        reason: str,
        first_deferred_at: float | None = None,
    ) -> None:
        """Re-park a gate-rejected event onto the deferred lane.

        Appends FIFO so older deferrals dispatch first when the
        gate clears. Emits one ``brain-loop deferred:`` INFO line so
        the no-interrupt invariant is observable from logs alone.

        ``first_deferred_at`` carries the original wait-start time
        across re-deferrals so a deeply-blocked event reports the
        cumulative wait, not just the most recent retry's duration.
        ``None`` means "this is the first defer" → stamp now.
        """
        kind = _kind_of(event)
        wait_start = (
            first_deferred_at
            if first_deferred_at is not None
            else time.monotonic()
        )
        self._deferred.append(
            _DeferredEntry(
                event=event,
                first_deferred_at=wait_start,
                last_reason=reason,
            )
        )
        # Only count fresh deferrals against the cumulative metric;
        # a re-defer is the same event, not a new bottleneck.
        if first_deferred_at is None:
            self._deferred_count += 1
        log.info(
            "brain-loop deferred: kind=%s reason=%s deferred_count=%d",
            kind,
            reason,
            len(self._deferred),
        )

    def _drain_deferred_if_ready(self) -> None:
        """Re-process the deferred lane if the gate is now clear.

        Called at the top of every consumer iteration. Walks the
        list in FIFO order; each entry that's still gated re-defers
        with an updated reason (so a long-blocked event eventually
        surfaces with the latest gate state in its log line).
        """
        if not self._deferred:
            return
        try:
            free = bool(self._free_to_speak())
        except Exception:
            # Predicate raised — keep deferring. Already logged in
            # _handle_event the first time it raised.
            return
        if not free:
            return
        # Pull everything out; dispatch one at a time. Anything still
        # gated will land back in _deferred via _handle_event (with
        # the original ``first_deferred_at`` preserved).
        snapshot = list(self._deferred)
        self._deferred.clear()
        for entry in snapshot:
            if not self._running:
                # We're being asked to stop; preserve the deferral
                # state by writing the remaining tail back.
                self._deferred.append(entry)
                continue
            self._handle_event(
                entry.event, first_deferred_at=entry.first_deferred_at
            )

    def _gate_reason(self) -> str:
        """Hook for chunk-4 wiring to publish the rich reason
        (``turn_in_progress`` / ``tts_active`` / ``both``).

        Chunk 3 just knows the gate is closed; the predicate doesn't
        say *why*. Returns ``"gate_closed"`` as a plain default;
        :meth:`set_free_to_speak` callers may pass a richer predicate
        + override this method on a subclass to publish granular
        reasons.
        """
        return "gate_closed"


__all__ = ["BrainLoop", "EventHandler", "FreeToSpeakPredicate"]

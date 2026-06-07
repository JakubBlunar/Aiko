"""Brain-orchestration subsystem mounted onto :class:`SessionController`.

Phase 1 / chunk 5 of the brain-orchestration refactor. This mixin
owns six new components:

* :class:`BrainEventQueue` — the priority queue.
* :class:`BrainLoop` — the single-consumer daemon thread.
* :class:`TaskStore` — SQLite facade over the schema-v16 ``tasks``
  table (lives on the shared :class:`ChatDatabase`).
* :class:`TaskOrchestrator` — the handler registry + lifecycle.
* :class:`TaskCueStore` — parked cues waiting to land in a turn.
* :class:`TaskEscalationManager` — per-cue timer that arms a
  proactive when silence stretches.

Layered on top of the existing :class:`SessionController` via the
mixin pattern documented in ``AGENTS.md``. The host class supplies
five attributes the mixin reads:

* ``_chat_db`` — the shared :class:`ChatDatabase`.
* ``_user_id`` — the active user's id (for per-user cap + cue keying).
* ``_session_id`` — the active session id (for session-key on
  enqueued events). Optional; the mixin falls back to ``_user_id``.
* ``_turn_in_progress`` — bool flag flipped True during the
  ``chat_once_streaming`` body.
* ``_tts`` — an object with ``.is_active() -> bool``. Together with
  ``_turn_in_progress`` it feeds the free-to-speak predicate.
* ``_settings`` — :class:`AppSettings`. The mixin reads
  ``agent.tasks_enabled`` / ``agent.tasks_per_user_cap`` /
  ``agent.task_completion_proactive_after_seconds`` / etc.
* ``_proactive`` — :class:`ProactiveDirector` (optional). When
  wired, the ``proactive`` brain-event handler calls
  :meth:`ProactiveDirector.notify_task_escalation` to dispatch a
  task-driven speaking turn. When missing (early boot, partial
  init, or a unit-test stub host) the handler logs at INFO and
  leaves the cue parked for a natural surface on the next user
  turn.

What chunk 5 wires (the minimum useful integration):

* The brain loop starts at init, gated on ``agent.tasks_enabled``.
* Handlers for ``task_result`` + ``task_input_needed`` park cues on
  :class:`TaskCueStore` and arm escalation timers.
* Handler for ``task_progress`` is registered but a no-op — chunks
  7+ will plug in the WS broadcast.
* Handler for ``proactive`` (with ``source=task_escalation``) is
  registered but for now just logs at INFO — chunk 6 wires it to
  :class:`ProactiveDirector` so Aiko actually speaks.
* User-message events still flow through the existing direct
  :class:`TurnRunner` path; the brain loop only handles task-side
  events for now. Chunk 7 swaps the user-message path onto the
  queue (with a future for MCP's blocking ``send_message``).

What chunk 5 does NOT wire:
* User messages onto the queue (chunk 7).
* The maintenance / state-sync paths onto the queue (chunk 8).
* The real proactive escalation path (chunk 6).
* REST / WS broadcasts for tasks (chunk 9).
* Concrete handlers like ``file_search`` / ``file_read`` (chunk 10).

The mixin is wired into :class:`SessionController` in a separate,
minimal step so this big internal contract change ships without
also reshuffling the ``__init__`` block layout.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import TYPE_CHECKING, Any

from app.core.brain import (
    BrainEventQueue,
    BrainLoop,
    KIND_PROACTIVE,
    KIND_TASK_INPUT_NEEDED,
    KIND_TASK_PROGRESS,
    KIND_TASK_RESULT,
    KIND_USER_MESSAGE,
    ProactiveEvent,
    ProducerCallbacks,
    TaskInputNeededEvent,
    TaskProgressEvent,
    TaskResultEvent,
    UserMessageEvent,
)
from app.core.tasks import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    EscalationConfig,
    TaskCleanupWorker,
    TaskCueStore,
    TaskEscalationManager,
    TaskEventStore,
    TaskInputStore,
    TaskOrchestrator,
    TaskStore,
    recover_interrupted_tasks,
)
from app.core.tasks.handlers import FileReadHandler, FileSearchHandler
from app.core.tasks.sandbox import FileTaskRoot, validate_roots


# Chunk 7: mapping from the event's user-facing ``mode`` to the
# ``mode`` keyword :meth:`SessionController.chat_once_streaming`
# expects. The event taxonomy is producer-shaped ("did this come
# from a typed keyboard / voice mic / MCP tool?") while
# ``chat_once_streaming`` is consumer-shaped ("how should the turn
# behave?" — typed re-arms the silence timer, live merges with
# voice phrase B, record is one-shot mic capture). MCP currently
# routes through typed mode just like the old ``chat_once`` did,
# so ``mcp -> typed`` keeps the existing behaviour byte-for-byte.
_USER_MESSAGE_MODE_MAP: dict[str, str] = {
    "typed": "typed",
    "mcp": "typed",
    "voice": "live",
}


if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.tasks.task_cue_store import TaskCue


log = logging.getLogger("app.session")


class TaskOrchestrationMixin:
    """Wires the brain-orchestration subsystem onto its host class.

    The host calls :meth:`_init_task_orchestration` once during
    boot (after ``_chat_db`` is ready) and
    :meth:`_shutdown_task_orchestration` once during teardown
    (before ``_chat_db`` closes). Everything else hangs off the
    mixin's own attributes (``_task_*``) so the host class doesn't
    need to know the wiring details.

    All attributes are set up lazily — the mixin can be a no-op
    when ``agent.tasks_enabled`` is ``False``, so an existing
    install can opt out without code changes. The pattern matches
    other recent mixins (avatar, world, post-turn, …): cheap when
    off, fully testable in isolation, registers no global state.
    """

    # ── lifecycle ────────────────────────────────────────────────────

    def _init_task_orchestration(self) -> None:
        """Build + wire the orchestration subsystem.

        Idempotent: a second call is a no-op (logged at DEBUG).
        Safe to call before any other mixin's init because the
        subsystem only depends on ``_chat_db``, ``_user_id``, and
        ``_settings`` (all populated by ``SessionController.__init__``
        in its first 30 lines).
        """
        if getattr(self, "_task_orchestration_inited", False):
            log.debug("task-orchestration init ignored: already inited")
            return
        agent = self._settings.agent  # type: ignore[attr-defined]
        if not bool(getattr(agent, "tasks_enabled", True)):
            # Master-switch off: install a thin "disabled" stub so
            # callers can still read the public properties without
            # ``None`` checks everywhere.
            self._task_orchestration_inited = True
            self._task_orchestration_enabled = False
            self._brain_queue = None
            self._brain_loop = None
            self._task_store = None
            self._task_orchestrator = None
            self._task_cue_store = None
            self._task_escalation_manager = None
            log.info("task-orchestration init: disabled (agent.tasks_enabled=False)")
            return

        # 1. Queue + loop. The loop's free-to-speak predicate reads
        #    the host's ``_turn_in_progress`` flag and ``_tts``
        #    activity flag. The predicate runs on the brain-loop
        #    thread so it must be cheap and side-effect-free.
        self._brain_queue = BrainEventQueue()
        self._brain_loop = BrainLoop(
            queue=self._brain_queue,
            free_to_speak=self._brain_loop_free_to_speak,
            poll_interval_seconds=max(
                0.01,
                float(getattr(agent, "brain_loop_deferred_grace_ms", 100)) / 1000.0,
            ),
        )

        # 2. Task store + orchestrator. The orchestrator owns its
        #    own thread pool (see TaskOrchestrator.__init__). We
        #    wire the queue so emits land on the loop.
        # Schema v17: also wire the sibling event log + input history
        #    stores + heartbeat config. Both stores share the same
        #    chat DB connection pool as the main task store, so
        #    creating them is cheap (no extra connections).
        self._task_store = TaskStore(self._chat_db)  # type: ignore[attr-defined]
        self._task_event_store = TaskEventStore(self._chat_db)  # type: ignore[attr-defined]
        self._task_input_store = TaskInputStore(self._chat_db)  # type: ignore[attr-defined]
        self._task_orchestrator = TaskOrchestrator(
            store=self._task_store,
            queue=self._brain_queue,
            per_user_cap=int(getattr(agent, "tasks_per_user_cap", 8)),
            session_key_resolver=self._task_session_key_for_user,
            event_store=self._task_event_store,
            input_store=self._task_input_store,
            cascade_cancel_children=bool(
                getattr(agent, "task_cascade_cancel_children", True)
            ),
            heartbeat_enabled=True,
            heartbeat_check_interval_seconds=int(
                getattr(agent, "task_heartbeat_check_interval_seconds", 30)
            ),
            heartbeat_stalled_seconds=int(
                getattr(agent, "task_stalled_seconds", 300)
            ),
            heartbeat_action=str(
                getattr(agent, "task_stalled_action", "warn")
            ),
        )
        # Schema v17: pruning worker for terminal task rows. Built
        # here and registered with the idle scheduler below (the
        # scheduler is constructed earlier by other mixins). Disabled
        # when ``tasks_enabled=False`` because the whole subsystem is
        # off; otherwise reads its cadence + retention from agent
        # settings.
        self._task_cleanup_worker = TaskCleanupWorker(
            self._task_store,
            event_store=self._task_event_store,
            input_store=self._task_input_store,
            retention_days=int(
                getattr(agent, "task_cleanup_retention_days", 30)
            ),
            interval_seconds=int(
                getattr(agent, "task_cleanup_interval_seconds", 21600)
            ),
            enabled=bool(getattr(agent, "tasks_enabled", True)),
        )

        # 3. Cue store + escalation manager. The escalation manager
        #    takes three callable hooks: free_to_speak (shared with
        #    the brain loop), last_user_message_at (we expose this
        #    via the mixin), and enqueue_proactive (constructs the
        #    ProactiveEvent and puts it on the queue).
        self._task_cue_store = TaskCueStore(
            max_age_seconds=float(getattr(agent, "task_cue_max_age_seconds", 1800)),
            max_aggregated=int(getattr(agent, "task_cue_max_aggregated", 5)),
        )
        self._task_escalation_manager = TaskEscalationManager(
            cue_store=self._task_cue_store,
            free_to_speak=self._brain_loop_free_to_speak,
            last_user_message_at=self._task_last_user_message_at,
            enqueue_proactive=self._task_enqueue_escalation_proactive,
            config=EscalationConfig(
                completion_after_seconds=float(
                    getattr(agent, "task_completion_proactive_after_seconds", 45)
                ),
                input_needed_after_seconds=float(
                    getattr(agent, "task_input_needed_proactive_after_seconds", 20)
                ),
            ),
        )

        # 3b. Register built-in task handlers. Chunk 9 ships the
        #     first reference handler: a read-only filesystem
        #     substring search sandboxed to
        #     ``agent.task_file_allowed_roots``. The validated root
        #     list is held by the handler so each ``start`` call
        #     doesn't redo the existence/type checks. If the user
        #     edits the roots at runtime, ``reconfigure_*`` would
        #     rebuild the handler and re-register (re-registration
        #     overwrites the same name slot).
        self._register_builtin_task_handlers(agent)

        # 4. Register brain-loop handlers.
        # Chunks 5-6 wired the task-side kinds (results, input-needed,
        # progress, proactive). Chunk 7 adds the user_message handler;
        # chunk 8 wires the WS chat handler's streaming callbacks
        # through the queue (via :class:`ProducerCallbacks`).
        # ``maintenance_due`` / ``speaking_window_job`` /
        # ``state_sync`` wait for a later chunk.
        self._brain_loop.register_handler(
            KIND_USER_MESSAGE, self._on_user_message_event
        )
        self._brain_loop.register_handler(
            KIND_TASK_RESULT, self._on_task_result_event
        )
        self._brain_loop.register_handler(
            KIND_TASK_INPUT_NEEDED, self._on_task_input_needed_event
        )
        self._brain_loop.register_handler(
            KIND_TASK_PROGRESS, self._on_task_progress_event
        )
        self._brain_loop.register_handler(
            KIND_PROACTIVE, self._on_task_proactive_event
        )

        # 5. Start the consumer thread.
        self._brain_loop.start()

        # 6. Boot recovery: scan non-terminal rows surviving a
        #    restart. Demote ``running`` → ``interrupted`` and (if
        #    the resume-on-boot flag is on) push a cue onto the
        #    queue so Aiko mentions it on her next turn.
        resume_on_boot = bool(getattr(agent, "tasks_resume_on_boot", True))
        try:
            report = recover_interrupted_tasks(
                self._task_store,
                orchestrator=self._task_orchestrator if resume_on_boot else None,
                resume_on_boot=resume_on_boot,
            )
        except Exception as exc:
            log.exception(
                "task-orchestration boot recovery failed: exc=%r", exc
            )
        else:
            log.info(
                "task-orchestration init: scanned=%d interrupted=%d "
                "preserved=%d failed=%d resume_on_boot=%d",
                report.total_scanned,
                len(report.interrupted),
                len(report.preserved),
                len(report.failed),
                int(resume_on_boot),
            )

        # 7. Schema v17: register the cleanup worker with the idle
        #    scheduler if one exists (the scheduler is constructed
        #    by ``SessionController.__init__`` before this mixin
        #    runs). Failures here drop the worker but don't break
        #    the rest of the task subsystem.
        idle_sched = getattr(self, "_idle_scheduler", None)
        if idle_sched is not None and self._task_cleanup_worker is not None:
            try:
                idle_sched.register(self._task_cleanup_worker)
                log.info(
                    "task-cleanup worker registered: interval_s=%d "
                    "retention_days=%d",
                    int(self._task_cleanup_worker.interval_seconds),
                    int(
                        getattr(agent, "task_cleanup_retention_days", 30)
                    ),
                )
            except Exception:
                log.warning(
                    "task-cleanup worker registration failed",
                    exc_info=True,
                )

        self._task_orchestration_inited = True
        self._task_orchestration_enabled = True
        log.info(
            "task-orchestration ready: cap=%d completion_after_s=%.0f "
            "input_after_s=%.0f",
            int(getattr(agent, "tasks_per_user_cap", 8)),
            float(getattr(agent, "task_completion_proactive_after_seconds", 45)),
            float(getattr(agent, "task_input_needed_proactive_after_seconds", 20)),
        )

    def _register_builtin_task_handlers(self, agent: Any) -> None:
        """Build + register the phase-1 reference handlers.

        Currently:

        * :class:`FileSearchHandler` — read-only filename substring
          search sandboxed to ``agent.task_file_allowed_roots``.
        * :class:`FileReadHandler` — read-only file content fetch
          with multi-root disambiguation via ``TaskInputNeeded``.

        Future siblings land here too — keep the method small and
        let each handler take care of its own construction logic.
        The orchestrator's :meth:`register_handler` uses the
        handler's ``name`` attribute as the slot key; re-registering
        with the same name overwrites, which is the contract a
        future hot-reload path can rely on.
        """
        roots_raw = getattr(agent, "task_file_allowed_roots", ()) or ()
        roots: list[FileTaskRoot] = []
        for entry in roots_raw:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label", "")).strip()
            path = str(entry.get("path", "")).strip()
            if not label or not path:
                continue
            roots.append(
                FileTaskRoot(
                    label=label,
                    path=path,
                    read_only=bool(entry.get("read_only", True)),
                )
            )
        # Boot-time validation. ``validate_roots`` already emits the
        # per-root WARNING lines so the user sees what's inactive
        # without an extra log call here. We still summarise the
        # final tally because that's the easy grep target for
        # "did my file roots register?".
        validated = validate_roots(roots)
        active = [vr for vr in validated if vr.active]
        log.info(
            "task-handlers: file roots configured=%d active=%d labels=%s",
            len(validated),
            len(active),
            [vr.root.label for vr in active],
        )
        try:
            self._task_orchestrator.register_handler(
                FileSearchHandler(roots=roots)
            )
        except Exception as exc:
            log.warning(
                "task-handlers: failed to register file_search handler: %r",
                exc,
            )
        # Chunk 12: file_read handler. Reads live caps off the agent
        # settings block so a hot reconfig that flips
        # ``task_file_read_max_bytes`` lands on the next
        # ``rebuild_tool_registry``-equivalent path (currently a full
        # restart in phase 1, but the construction shape is ready).
        try:
            self._task_orchestrator.register_handler(
                FileReadHandler(
                    roots=roots,
                    max_bytes=int(
                        getattr(agent, "task_file_read_max_bytes", 262144)
                    ),
                    max_lines=int(
                        getattr(agent, "task_file_read_max_lines", 2000)
                    ),
                    allowed_extensions=tuple(
                        getattr(agent, "task_file_read_allowed_extensions", ())
                        or ()
                    ),
                )
            )
        except Exception as exc:
            log.warning(
                "task-handlers: failed to register file_read handler: %r",
                exc,
            )

    def _shutdown_task_orchestration(self) -> None:
        """Tear down the orchestration subsystem in safe order.

        Order matters: escalation timers first (so a fire can't race
        a half-torn-down loop), then the brain loop (closes the
        queue + joins the thread), then the orchestrator (drains its
        executor). The task store is database-backed and follows
        the shared ``_chat_db`` close path in
        :class:`SessionController.shutdown`.

        Idempotent + exception-safe — every component's stop call
        is wrapped so a misbehaving one can't block the rest.
        """
        if not getattr(self, "_task_orchestration_inited", False):
            return
        if not getattr(self, "_task_orchestration_enabled", False):
            self._task_orchestration_inited = False
            return
        if getattr(self, "_task_escalation_manager", None) is not None:
            try:
                self._task_escalation_manager.shutdown()
            except Exception:
                log.debug(
                    "task-escalation shutdown failed", exc_info=True
                )
        if getattr(self, "_brain_loop", None) is not None:
            try:
                self._brain_loop.stop(timeout=1.5)
            except Exception:
                log.debug("brain-loop stop failed", exc_info=True)
        if getattr(self, "_task_orchestrator", None) is not None:
            try:
                self._task_orchestrator.shutdown(wait=False)
            except Exception:
                log.debug(
                    "task-orchestrator shutdown failed", exc_info=True
                )
        self._task_orchestration_inited = False
        log.info("task-orchestration shutdown: done")

    # ── public surface ───────────────────────────────────────────────

    @property
    def task_orchestrator(self) -> TaskOrchestrator | None:
        """The :class:`TaskOrchestrator`, or ``None`` when disabled.

        Returned for MCP debug tools + tests; production code paths
        that need it should go through ``self._task_orchestrator``
        directly (cheaper, no property indirection in hot loops).
        """
        return getattr(self, "_task_orchestrator", None)

    @property
    def task_cue_store(self) -> TaskCueStore | None:
        """The :class:`TaskCueStore`, or ``None`` when disabled."""
        return getattr(self, "_task_cue_store", None)

    @property
    def brain_loop(self) -> BrainLoop | None:
        """The :class:`BrainLoop`, or ``None`` when disabled.

        Producers (chunk 7+) push events to ``brain_loop.queue``
        or ``brain_loop.enqueue(event)``.
        """
        return getattr(self, "_brain_loop", None)

    def drain_task_cues_for_render(
        self, *, turn_id: str | None = None
    ) -> str:
        """Drain parked cues + render the T6 prompt block.

        Called by :class:`PromptAssembler` (installed as a provider
        via :meth:`set_providers`). Returns the rendered block, or
        the empty string when nothing is parked.

        Also cancels any escalation timer for the surfaced cues —
        the cue is about to land in the next turn naturally, so we
        don't want it to also escalate as a proactive event a few
        seconds later.
        """
        cue_store = getattr(self, "_task_cue_store", None)
        if cue_store is None:
            return ""
        result = cue_store.drain_for_render(turn_id=turn_id)
        if not result.surfaced:
            return ""
        escalation = getattr(self, "_task_escalation_manager", None)
        if escalation is not None:
            for cue in result.surfaced:
                escalation.cancel_for_task(
                    cue.task_id, reason="surfaced_in_turn",
                )
        # Render via the pure cue_render module. Importing here
        # keeps the mixin's import cost minimal at boot — the
        # render function is only needed at turn-assembly time.
        from app.core.tasks.cue_render import render_cue_block

        agent = self._settings.agent  # type: ignore[attr-defined]
        return render_cue_block(
            result.surfaced,
            max_aggregated=int(getattr(agent, "task_cue_max_aggregated", 5)),
        )

    # ── internal: brain-loop predicates + helpers ───────────────────

    def _brain_loop_free_to_speak(self) -> bool:
        """Free-to-speak predicate for both the brain loop's gate
        and the escalation manager's fire path.

        Returns ``True`` iff Aiko is neither mid-turn nor mid-TTS.
        Wraps every attribute read in ``getattr`` so a partially-
        initialised host doesn't crash the predicate. The brain
        loop catches exceptions from this and defers anyway, so
        ``True`` on missing state is the safe default — but
        defending against `AttributeError` keeps the DEBUG log
        clean.
        """
        if bool(getattr(self, "_turn_in_progress", False)):
            return False
        tts = getattr(self, "_tts", None)
        if tts is not None:
            try:
                if bool(tts.is_active()):
                    return False
            except Exception:
                pass
        return True

    def _task_last_user_message_at(self) -> float:
        """Monotonic timestamp of the most recent user activity.

        Used by the escalation manager to suppress a cue that's
        already going to surface naturally in the next turn (user
        spoke after the cue parked → next turn's prompt picks it
        up).

        Reads the host's ``_last_user_activity_at`` field — the same
        timestamp ``_is_user_idle`` uses, so a "fresh user touch"
        means the same thing across both code paths. ``-inf`` on a
        partially-initialised host (the field isn't set yet) tells
        the escalation manager "no recent activity" — i.e. the
        proactive fire is safe.
        """
        anchor = getattr(self, "_last_user_activity_at", None)
        if anchor is None:
            return -float("inf")
        try:
            return float(anchor)
        except (TypeError, ValueError):
            return -float("inf")

    def _task_session_key_for_user(self, user_id: str) -> str:
        """Resolve the session key for a task's user.

        The :class:`TaskOrchestrator` uses this when emitting
        events so the brain loop's downstream consumers know which
        session the task belongs to. In phase 1 a session is just
        ``user_id`` — multi-user installs slot it into the
        existing session key scheme.
        """
        return str(user_id)

    def _task_enqueue_escalation_proactive(
        self, session_key: str, parked_cue_ids: tuple[str, ...]
    ) -> None:
        """Construct + enqueue a :class:`ProactiveEvent` from the
        escalation manager.

        The brain-loop handler for ``proactive`` (registered as
        :meth:`_on_task_proactive_event` in this mixin) picks it
        up and (chunk 6+) routes it through
        :class:`ProactiveDirector`. For chunk 5 the handler just
        logs the receipt — the cue stays on the store, so a real
        user message will still surface it normally.
        """
        loop = getattr(self, "_brain_loop", None)
        if loop is None:
            return
        loop.enqueue(
            ProactiveEvent(
                session_key=str(session_key),
                source="task_escalation",
                parked_cue_ids=tuple(parked_cue_ids),
            )
        )

    # ── brain-loop handlers ─────────────────────────────────────────

    def _on_user_message_event(self, event: Any) -> None:
        """Handle a ``user_message`` brain event (chunk 7).

        Runs the existing :meth:`SessionController.chat_once_streaming`
        path on the brain-loop thread. ``user_message`` events bypass
        the free-to-speak gate (barge-in is real intent) so the loop
        dispatches them immediately; the runner's own merge-buffer +
        ``_turn_in_progress`` flag handle the race against any
        in-flight turn from the legacy direct paths.

        ``event.reply_future`` is filled with the assistant's reply
        text on success and with the raised exception on failure.
        MCP ``send_message`` blocks on this future to return the
        reply synchronously to its caller; producers that don't
        need the reply (typed WS push, voice live mode) pass
        ``None`` and the handler just runs the turn.

        For chunk 7 the only callbacks threaded into
        ``chat_once_streaming`` are the implicit ones the
        controller already wires (TTS dispatch, message
        broadcast). Streaming token callbacks (``on_token`` /
        ``on_generation_status`` / ``stop_requested``) stay on the
        producer side — chunk 8 will extend
        :class:`UserMessageEvent` with optional callable fields
        when the WS handler swaps over.
        """
        if not isinstance(event, UserMessageEvent):
            log.debug(
                "user_message handler received wrong type: %r",
                type(event).__name__,
            )
            return
        text = event.text or ""
        if not text.strip():
            # ``chat_once_streaming`` would early-out anyway, but we
            # still need to resolve the future so a producer doesn't
            # block forever on whitespace-only input.
            if event.reply_future is not None:
                try:
                    event.reply_future.set_result("")
                except Exception:
                    log.debug(
                        "user_message empty-text future already set",
                        exc_info=True,
                    )
            return
        chat_mode = _USER_MESSAGE_MODE_MAP.get(event.mode, "typed")
        # ``skip_tts`` is a per-call override that has to land on the
        # settings.tts.enabled flag because ``chat_once_streaming``
        # reads that flag (not a per-call arg). Save + restore so a
        # one-off skip never leaks past this turn. The settings
        # object is mutated under a lock-free convention shared with
        # the legacy MCP path — :class:`SessionController` callers
        # are expected to be on the brain-loop thread (us) or the
        # main thread, never both at once for this flag.
        tts_settings = getattr(self._settings, "tts", None)  # type: ignore[attr-defined]
        previous_tts_enabled: Any = None
        if event.skip_tts and tts_settings is not None:
            previous_tts_enabled = getattr(tts_settings, "enabled", None)
            try:
                tts_settings.enabled = False
            except Exception:
                log.debug(
                    "user_message skip_tts: failed to disable TTS flag",
                    exc_info=True,
                )
                previous_tts_enabled = None
        # Chunk 8: thread streaming callbacks into ``chat_once_streaming``
        # when the producer attached a :class:`ProducerCallbacks`
        # bundle. WS chat handler relies on these for per-token
        # broadcast + stop-button support. The callbacks execute on
        # the brain-loop thread inline with the turn — producers
        # must keep them lightweight and thread-safe with respect to
        # their own consumers (the WS hub broadcast is already
        # thread-safe so this is free for the WS case).
        cb = event.callbacks
        on_token = getattr(cb, "on_token", None) if cb is not None else None
        on_generation_status = (
            getattr(cb, "on_generation_status", None) if cb is not None else None
        )
        stop_requested = (
            getattr(cb, "stop_requested", None) if cb is not None else None
        )
        # Chunk 11: voice-only metadata that used to ride
        # ``chat_once_streaming`` kwargs directly. The merge-buffer
        # decision still happens on the audio thread inside
        # ``process_live_capture`` BEFORE the event lands here, so
        # ``resume_message_id`` arrives pre-resolved.
        resume_message_id = getattr(event, "resume_message_id", None)
        capture_ms = float(getattr(event, "capture_ms", 0.0) or 0.0)
        stt_ms = float(getattr(event, "stt_ms", 0.0) or 0.0)
        try:
            reply = self.chat_once_streaming(  # type: ignore[attr-defined]
                user_text=text,
                mode=chat_mode,
                on_token=on_token,
                on_generation_status=on_generation_status,
                stop_requested=stop_requested,
                capture_ms=capture_ms,
                stt_ms=stt_ms,
                _resume_message_id=resume_message_id,
            )
        except Exception as exc:
            log.exception(
                "user_message handler chat_once_streaming failed: "
                "session=%s mode=%s text_chars=%d",
                event.session_key,
                event.mode,
                len(text),
            )
            if event.reply_future is not None:
                try:
                    event.reply_future.set_exception(exc)
                except Exception:
                    log.debug(
                        "user_message exception future already set",
                        exc_info=True,
                    )
            return
        finally:
            if previous_tts_enabled is not None and tts_settings is not None:
                try:
                    tts_settings.enabled = previous_tts_enabled
                except Exception:
                    log.debug(
                        "user_message skip_tts: failed to restore TTS flag",
                        exc_info=True,
                    )
        if event.reply_future is not None:
            try:
                event.reply_future.set_result(reply or "")
            except Exception:
                log.debug(
                    "user_message reply future already set", exc_info=True,
                )

    def enqueue_user_message(
        self,
        *,
        text: str,
        mode: str = "mcp",
        skip_tts: bool = False,
        wait_for_reply: bool = False,
        timeout: float | None = 120.0,
        on_token: Any = None,
        on_generation_status: Any = None,
        stop_requested: Any = None,
        callbacks: ProducerCallbacks | None = None,
        resume_message_id: int | None = None,
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
    ) -> str | None:
        """Producer-side entry point for the brain-queue user-message path.

        Builds a :class:`UserMessageEvent`, puts it on the queue, and
        either returns immediately (``wait_for_reply=False``) or
        blocks on a :class:`concurrent.futures.Future` until the
        brain loop's handler resolves it
        (``wait_for_reply=True``). The future is also attached when
        ``wait_for_reply`` is False **iff** ``mode == "mcp"`` so the
        MCP path keeps its blocking contract even when callers
        forget the kwarg — defensive default since MCP is the only
        chunk-7 producer that actually uses the queue path.

        Returns the assistant's reply text on success
        (``wait_for_reply=True``), or ``None`` otherwise. Raises if
        ``wait_for_reply=True`` and the handler raised — the
        producer is expected to wrap MCP-style errors in a friendly
        message at its boundary.

        Falls back to the legacy direct ``chat_once_streaming``
        path when the task subsystem is disabled
        (``agent.tasks_enabled = False``). Producers can call this
        unconditionally; the mixin picks the right path.

        Streaming callbacks (chunk 8): producers can pass any of
        ``on_token`` / ``on_generation_status`` / ``stop_requested``
        as keyword args **or** bundle them into a
        :class:`ProducerCallbacks` and pass that as ``callbacks``.
        The keyword form is the WS-handler convenience shape;
        ``callbacks=`` is what the brain-loop handler reads off the
        event after the queue hop. When both shapes are passed the
        explicit ``callbacks`` argument wins.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            # Empty / whitespace-only input never reaches the queue.
            # Voice producers that wait on the reply still need a
            # value back; MCP / typed producers that don't wait get
            # ``None`` (matches the legacy direct-call shape).
            return "" if wait_for_reply else None

        # Reconcile the two callback-passing conventions. Producers
        # using the loose-kwarg shape (WS handler) get a synthesised
        # bundle; producers passing ``callbacks=`` win outright. We
        # only allocate when at least one callback is set so the
        # MCP / fire-and-forget path stays zero-cost.
        effective_callbacks: ProducerCallbacks | None = callbacks
        if effective_callbacks is None and (
            on_token is not None
            or on_generation_status is not None
            or stop_requested is not None
        ):
            effective_callbacks = ProducerCallbacks(
                on_token=on_token,
                on_generation_status=on_generation_status,
                stop_requested=stop_requested,
            )

        loop = getattr(self, "_brain_loop", None)
        if loop is None or not getattr(self, "_task_orchestration_enabled", False):
            # Master switch off / partial init: degrade to the legacy
            # direct path so producers don't have to special-case the
            # disabled state. Mode mapping mirrors the handler.
            chat_mode = _USER_MESSAGE_MODE_MAP.get(mode, "typed")
            log.debug(
                "enqueue_user_message: task subsystem disabled, "
                "falling back to direct chat_once_streaming "
                "(mode=%s text_chars=%d)",
                mode,
                len(cleaned),
            )
            tts_settings = getattr(self._settings, "tts", None)  # type: ignore[attr-defined]
            previous_tts_enabled: Any = None
            if skip_tts and tts_settings is not None:
                previous_tts_enabled = getattr(tts_settings, "enabled", None)
                try:
                    tts_settings.enabled = False
                except Exception:
                    previous_tts_enabled = None
            try:
                # Direct fallback also threads the streaming
                # callbacks so the WS handler keeps working when
                # tasks are disabled. Chunk 11: voice metadata
                # (resume_message_id / capture_ms / stt_ms) rides
                # alongside so the merge / metrics paths stay correct
                # for voice producers even with tasks turned off.
                direct_kwargs: dict[str, Any] = {
                    "user_text": cleaned,
                    "mode": chat_mode,
                }
                if effective_callbacks is not None:
                    if effective_callbacks.on_token is not None:
                        direct_kwargs["on_token"] = effective_callbacks.on_token
                    if effective_callbacks.on_generation_status is not None:
                        direct_kwargs["on_generation_status"] = (
                            effective_callbacks.on_generation_status
                        )
                    if effective_callbacks.stop_requested is not None:
                        direct_kwargs["stop_requested"] = (
                            effective_callbacks.stop_requested
                        )
                if resume_message_id is not None:
                    direct_kwargs["_resume_message_id"] = int(resume_message_id)
                if capture_ms:
                    direct_kwargs["capture_ms"] = float(capture_ms)
                if stt_ms:
                    direct_kwargs["stt_ms"] = float(stt_ms)
                reply = self.chat_once_streaming(**direct_kwargs)  # type: ignore[attr-defined]
            finally:
                if previous_tts_enabled is not None and tts_settings is not None:
                    try:
                        tts_settings.enabled = previous_tts_enabled
                    except Exception:
                        pass
            return reply if wait_for_reply else None

        future: concurrent.futures.Future[str] | None = None
        attach_future = wait_for_reply or mode == "mcp"
        if attach_future:
            future = concurrent.futures.Future()

        session_key = getattr(self, "session_key", "") or ""
        if not session_key:
            session_key = str(getattr(self, "_user_id", "default"))

        event = UserMessageEvent(
            session_key=session_key,
            text=cleaned,
            mode=mode,  # type: ignore[arg-type]
            reply_future=future,
            skip_tts=bool(skip_tts),
            callbacks=effective_callbacks,
            resume_message_id=int(resume_message_id) if resume_message_id is not None else None,
            capture_ms=float(capture_ms or 0.0),
            stt_ms=float(stt_ms or 0.0),
        )
        loop.enqueue(event)
        log.info(
            "user_message enqueued: mode=%s session=%s text_chars=%d "
            "wait_for_reply=%s callbacks=%s resume=%s",
            mode,
            session_key,
            len(cleaned),
            wait_for_reply or attach_future,
            effective_callbacks is not None,
            resume_message_id,
        )

        if not wait_for_reply or future is None:
            return None
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning(
                "user_message handler timed out after %.1fs "
                "(session=%s mode=%s)",
                float(timeout or 0.0),
                session_key,
                mode,
            )
            raise

    def _on_task_result_event(self, event: Any) -> None:
        """Handle a ``task_result`` brain event.

        Parks the result as a cue on :class:`TaskCueStore`, arms
        the escalation timer (45 s by default), and is done.

        Cues for ``notify_aiko=False`` tasks (internal Aiko-brain
        work) silently drop — they get persisted in the store but
        never appear in the prompt. The
        :class:`TaskOrchestrator` already gates emission on
        ``notify_aiko``, but the mixin double-checks to keep the
        contract local.
        """
        if not isinstance(event, TaskResultEvent):
            log.debug(
                "task_result handler received wrong type: %r",
                type(event).__name__,
            )
            return
        if not bool(getattr(event, "notify_aiko", True)):
            return
        cue_store = getattr(self, "_task_cue_store", None)
        escalation = getattr(self, "_task_escalation_manager", None)
        if cue_store is None or escalation is None:
            return
        cue = cue_store.park(
            task_id=event.task_id,
            session_key=event.session_key,
            kind=CUE_KIND_RESULT,
            title=event.title,
            status=event.status,
            summary=event.result_summary,
            error=event.error,
        )
        escalation.arm(cue)

    def _on_task_input_needed_event(self, event: Any) -> None:
        """Handle a ``task_input_needed`` brain event.

        Same shape as :meth:`_on_task_result_event` but uses the
        shorter escalation window (20 s by default) since a
        blocked task is more pressing than a finished one.
        """
        if not isinstance(event, TaskInputNeededEvent):
            log.debug(
                "task_input_needed handler received wrong type: %r",
                type(event).__name__,
            )
            return
        cue_store = getattr(self, "_task_cue_store", None)
        escalation = getattr(self, "_task_escalation_manager", None)
        if cue_store is None or escalation is None:
            return
        cue = cue_store.park(
            task_id=event.task_id,
            session_key=event.session_key,
            kind=CUE_KIND_INPUT_NEEDED,
            title="",  # TaskInputNeededEvent doesn't carry a title yet
            summary=event.prompt,
            options=event.options,
        )
        escalation.arm(cue)

    def _on_task_progress_event(self, event: Any) -> None:
        """Handle a ``task_progress`` brain event.

        Chunk 5: no-op. The UI's TaskStrip + the running-tasks
        inner-life provider both read the store directly, so a
        progress percent bump doesn't need to do anything here.

        Chunks 7+ wire the WS broadcast (so the strip updates live
        without a poll). For now we just acknowledge the event
        existed so the brain loop's ``dispatched`` counter ticks.
        """
        if not isinstance(event, TaskProgressEvent):
            log.debug(
                "task_progress handler received wrong type: %r",
                type(event).__name__,
            )
            return
        # Intentionally no work. The brain loop logs the dispatch
        # at INFO so this still appears in ``tail_logs``.

    def _on_task_proactive_event(self, event: Any) -> None:
        """Handle a ``proactive`` brain event.

        Chunk 6: route ``source=task_escalation`` events into
        :class:`ProactiveDirector.notify_task_escalation`. The
        director picks voice vs typed mode internally and dispatches
        the speaking thread; the parked cues land in the new
        proactive turn's prompt via the existing T6 task-cues
        provider (drained on assembly, which also cancels the
        matching escalation timer).

        When the host hasn't wired a proactive director (early
        boot, partial init, or a unit test using the stub host),
        the handler logs at INFO and leaves the cue parked — a
        future user message will still surface it through the
        natural prompt path.

        Events with other ``source`` values (``voice_silence`` /
        ``typed_silence``) are NOT this handler's responsibility in
        phase 1 — they flow through the legacy direct ``notify_*``
        path on :class:`SessionController`. Chunk 8 will swap them
        onto the queue and into this handler.
        """
        if not isinstance(event, ProactiveEvent):
            log.debug(
                "proactive handler received wrong type: %r",
                type(event).__name__,
            )
            return
        source = getattr(event, "source", "")
        if source != "task_escalation":
            log.debug(
                "proactive handler ignored: source=%s (chunk-6 only "
                "routes task_escalation)",
                source,
            )
            return
        director = getattr(self, "_proactive", None)
        if director is None:
            log.info(
                "task-escalation proactive skipped: no proactive "
                "director wired (cues=%d session=%s)",
                len(event.parked_cue_ids),
                event.session_key,
            )
            return
        try:
            director.notify_task_escalation(event.session_key)
        except Exception:
            log.exception(
                "task-escalation proactive dispatch failed: cues=%d "
                "session=%s",
                len(event.parked_cue_ids),
                event.session_key,
            )

    # ── debug surface ────────────────────────────────────────────────

    def task_orchestration_state(self) -> dict[str, Any]:
        """Diagnostic dump for MCP debug tools.

        Single dict that snapshots every interesting counter +
        gauge of the subsystem in one call so an operator can grep
        the JSON output rather than calling six separate getters.
        """
        if not getattr(self, "_task_orchestration_enabled", False):
            return {"enabled": False}
        loop = self._brain_loop
        cue_store = self._task_cue_store
        escalation = self._task_escalation_manager
        return {
            "enabled": True,
            "queue_depth": loop.queue.depth() if loop is not None else 0,
            "loop_metrics": loop.metrics_snapshot() if loop is not None else {},
            "cue_metrics": (
                cue_store.metrics_snapshot() if cue_store is not None else {}
            ),
            "cue_snapshot": (
                [
                    {
                        "task_id": c.task_id,
                        "kind": c.kind,
                        "age_s": max(0.0, time.monotonic() - c.parked_at),
                        "title": c.title,
                    }
                    for c in cue_store.snapshot()
                ]
                if cue_store is not None
                else []
            ),
            "escalation_pending": (
                escalation.pending_count() if escalation is not None else 0
            ),
            "escalation_snapshot": (
                [
                    {"task_id": tid, "kind": kind, "age_s": age}
                    for tid, kind, age in escalation.snapshot()
                ]
                if escalation is not None
                else []
            ),
            "free_to_speak": self._brain_loop_free_to_speak(),
        }


__all__ = ["TaskOrchestrationMixin"]

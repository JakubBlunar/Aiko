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
  ``agent.task_cue_max_age_seconds`` / etc.
* ``_proactive`` — :class:`ProactiveDirector` (optional). When
  wired, the ``proactive`` brain-event handler calls
  :meth:`ProactiveDirector.notify_task_escalation` to dispatch a
  task-driven speaking turn. When missing (early boot, partial
  init, or a unit-test stub host) the handler logs at INFO and
  leaves the cue parked for a natural surface on the next user
  turn.

What chunk 5 wires (the minimum useful integration):

* The brain loop starts at init, gated on ``agent.tasks_enabled``.
* The ``task_result`` handler routes through the C6 report decision,
  parking a cue on :class:`TaskCueStore` and arming the escalation
  timer (fires when Aiko is free) for ``surface_now`` / floor tasks.
  ``task_input_needed`` is UI-only (the TaskStrip surfaces it).
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
from app.core.tasks.handlers import (
    VisionDescribeHandler,
)
from app.core.tasks.report_decision import (
    ACTION_DROP,
    ACTION_PARK,
    ACTION_SURFACE,
    PROVENANCE_SELF,
    PROVENANCE_USER,
    decide_task_report,
)
from app.core.tasks.sandbox import FileTaskRoot, validate_roots
from app.core.tasks.task_handler import INITIATED_BY_AIKO


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
        # Ids of tasks whose result was folded into the spawning turn
        # (inline fast path); consumed by ``_on_task_result_event`` to
        # suppress the duplicate proactive reply. Lives regardless of
        # the master switch so the tool helpers never hit a missing attr.
        self._task_inline_resolved_ids: set[int] = set()
        # Session-scoped "approve all" set for destructive task
        # capabilities. Populated when the user clicks "approve all" on
        # an approval prompt (or answers a workflow approval with an
        # approve-all decision). Holds capability ids (e.g. "file_write")
        # or the ``"all"`` sentinel. Never persisted — cleared on
        # restart, so a blanket approval can't silently outlive the
        # session. Lives regardless of the master switch so the handler
        # callbacks never hit a missing attr.
        self._approved_capabilities: set[str] = set()
        # Capability gaps recorded by the goal-workflow handler when the
        # planner declares "missing_capability". Bounded ring so the
        # MCP debug surface + ``check_my_work`` can surface "things I
        # couldn't do yet" without unbounded growth. Always present so
        # the workflow sink + tools never hit a missing attr.
        import collections as _collections

        agent_cfg = self._settings.agent  # type: ignore[attr-defined]
        gap_max = max(1, int(getattr(agent_cfg, "workflow_capability_gap_log_max", 50)))
        self._workflow_capability_gaps: "_collections.deque[dict[str, Any]]" = (
            _collections.deque(maxlen=gap_max)
        )
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
            self._external_mcp_manager = None
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
            config=EscalationConfig(),
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
            "task-orchestration ready: cap=%d (escalation fires when free)",
            int(getattr(agent, "tasks_per_user_cap", 8)),
        )

    def _register_builtin_task_handlers(self, agent: Any) -> None:
        """Build + register the built-in task handlers.

        Currently:

        * :class:`VisionDescribeHandler` — describe an image (local
          multimodal worker model) resolved against
          ``agent.task_file_allowed_roots`` (+ the managed
          ``Attachments`` root).
        * :class:`WebSearchHandler` — background web search.
        * :class:`GoalWorkflowHandler` — the multi-step planner.

        File read / search / write are NOT built in — they come from a
        filesystem MCP server (the ``filesystem`` plugin). The file
        roots below still back vision + chat attachments.

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
        # D2 Part B — managed Attachments root. A fixed
        # ``data/attachments/`` dir, auto-created + appended as a
        # read-only sandbox root so in-chat attachments resolve as
        # ``Attachments:<file>`` through the vision describe_image
        # skill with zero extra path plumbing.
        # Never overrides a user-configured ``Attachments`` label.
        try:
            from app.core.tasks.attachments import (
                ATTACHMENTS_LABEL,
                attachments_root,
                ensure_attachments_dir,
            )

            if not any(r.label == ATTACHMENTS_LABEL for r in roots):
                ensure_attachments_dir()
                roots.append(attachments_root())
        except Exception as exc:
            log.warning(
                "task-handlers: failed to register Attachments root: %r", exc
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
        # Vision (describe_image) handler — read-only, reuses the already-
        # loaded worker Ollama client + model (no second model in VRAM).
        # Only registered when ``agent.vision.enabled`` is on AND at least
        # one active root exists. The providers re-read the live worker
        # client / model so a reconfigure that rebuilds them is picked up.
        vision_cfg = getattr(agent, "vision", None)
        vision_enabled = bool(getattr(vision_cfg, "enabled", False))
        if vision_enabled and active:
            try:
                self._task_orchestrator.register_handler(
                    VisionDescribeHandler(
                        roots=roots,
                        client_provider=lambda: getattr(
                            self, "_worker_client_inner", None
                        ),
                        model_provider=lambda cfg=vision_cfg: (
                            (str(getattr(cfg, "model", "") or "").strip())
                            or str(getattr(self, "_effective_worker_model", "") or "")
                        ),
                        max_bytes=int(
                            getattr(vision_cfg, "max_bytes", 8 * 1024 * 1024)
                        ),
                        allowed_extensions=tuple(
                            getattr(vision_cfg, "allowed_extensions", ()) or ()
                        ),
                        default_prompt=str(
                            getattr(vision_cfg, "default_prompt", "") or ""
                        ),
                    )
                )
                log.info(
                    "task-handlers: vision_describe handler registered "
                    "(active_roots=%d model=%s)",
                    len(active),
                    (str(getattr(vision_cfg, "model", "") or "") or "(worker default)"),
                )
            except Exception as exc:
                log.warning(
                    "task-handlers: failed to register vision_describe handler: %r",
                    exc,
                )
        elif vision_enabled and not active:
            log.info(
                "task-handlers: vision enabled but no active file root "
                "configured -- describe_image skill not offered"
            )

        # ── nested goal workflows ────────────────────────────────────
        # web_search runs as a background task handler (it's too slow
        # for the conversational lane). Register it whenever the dep is
        # present so the workflow ``web_search`` skill can spawn it.
        try:
            from app.core.tasks.handlers.web_search import (
                DEFAULT_MAX_RESULTS,
                WebSearchHandler,
            )

            self._web_search_handler = WebSearchHandler(
                max_results=int(
                    getattr(agent, "workflow_web_search_max_results", DEFAULT_MAX_RESULTS)
                ),
                provider=self._get_search_provider(),
            )
            self._register_search_consumer(self._web_search_handler)
            self._task_orchestrator.register_handler(self._web_search_handler)
        except Exception as exc:
            log.warning(
                "task-handlers: failed to register web_search handler: %r",
                exc,
            )

        # The parent GoalWorkflowHandler, gated on ``agent.workflow_enabled``.
        if not bool(getattr(agent, "workflow_enabled", True)):
            log.info(
                "task-handlers: goal workflow disabled "
                "(agent.workflow_enabled=False)"
            )
            self._workflow_skill_registry = None
            return
        try:
            from app.core.tasks.workflow import (
                GoalWorkflowHandler,
                build_builtin_skill_registry,
            )

            tools_cfg = getattr(self._settings, "tools", None)  # type: ignore[attr-defined]
            web_enabled = bool(getattr(tools_cfg, "web_search", True))
            # File operations are provided exclusively by a filesystem MCP
            # server (the ``filesystem`` plugin), registered onto this
            # registry by ``_init_external_mcp``. The built-in registry
            # ships only web search + vision + the terminal ``finish``.
            skill_registry = build_builtin_skill_registry(
                web_search_enabled=web_enabled,
                vision_enabled=vision_enabled and bool(active),
            )
            # Future MCP-provided skills register onto this same object.
            self._workflow_skill_registry = skill_registry
            handler = GoalWorkflowHandler(
                orchestrator=self._task_orchestrator,
                skill_registry=skill_registry,
                # Providers so a reconfigure that rebuilds the worker
                # client / model is picked up on the next workflow.
                worker_client_provider=lambda: getattr(
                    self, "_workflow_client", None
                ),
                model_provider=lambda: getattr(
                    self, "_effective_worker_model", None
                ),
                user_name_provider=lambda: getattr(
                    self, "user_display_name", "the user"
                ),
                on_capability_gap=self._record_workflow_capability_gap,
                max_iterations=int(
                    getattr(agent, "workflow_max_iterations", 12)
                ),
                max_children=int(getattr(agent, "workflow_max_children", 8)),
                child_wait_timeout_seconds=float(
                    getattr(agent, "workflow_child_wait_timeout_seconds", 120.0)
                ),
                planner_history_budget_chars=int(
                    getattr(agent, "workflow_planner_history_budget_chars", 4000)
                ),
                planner_max_tokens=int(
                    getattr(agent, "workflow_planner_max_tokens", 512)
                ),
                # Worker-lane skill router: read live so a settings change
                # is picked up on the next workflow without a rebuild.
                skill_router_enabled_provider=lambda: bool(
                    getattr(
                        getattr(self._settings, "agent", None),
                        "workflow_skill_router_enabled",
                        False,
                    )
                ),
                # Robustness limits so an offline service (e.g. browser
                # with Chrome closed) fails fast with a clear message
                # instead of grinding through every iteration.
                max_consecutive_failures=int(
                    getattr(agent, "workflow_max_consecutive_failures", 2)
                ),
                max_wall_seconds=float(
                    getattr(agent, "workflow_max_wall_seconds", 300)
                ),
                # No-progress / loop detector.
                loop_detection_enabled=bool(
                    getattr(agent, "workflow_loop_detection_enabled", True)
                ),
                loop_window=int(getattr(agent, "workflow_loop_window", 4)),
                loop_repeat_threshold=int(
                    getattr(agent, "workflow_loop_repeat_threshold", 3)
                ),
                # Plugin / runtime-captured guidance, read live so plugin
                # SKILL.md + connected-server instructions reach the planner
                # per mcp:* group.
                group_guidance_provider=self._mcp_group_guidance_live,
            )
            self._task_orchestrator.register_handler(handler)
            log.info(
                "task-handlers: goal workflow registered (skills=%s)",
                skill_registry.names(),
            )
        except Exception as exc:
            log.warning(
                "task-handlers: failed to register goal workflow: %r",
                exc,
            )
            self._workflow_skill_registry = None
            return

        # External MCP servers → background-lane skills. Gated on the
        # master switch + a non-empty server list; only meaningful when
        # the workflow handler above is present (MCP tools are surfaced
        # to the background planner only, never to the brain's fast tools).
        self._init_external_mcp(agent, skill_registry)

    def _init_external_mcp(self, agent: Any, skill_registry: Any) -> None:
        """Build + start the external MCP manager and register its skills.

        Best-effort: any failure here downgrades to "no MCP tools" and
        leaves the built-in workflow skills fully working. The manager's
        ``tools_changed`` callback re-runs ``register_mcp_skills`` so a
        server that finishes connecting after boot (``npx`` cold start,
        slow handshake) lands its tools without a restart.
        """
        self._external_mcp_manager = None
        self._browser_perception = None
        # Plugin-provided planner guidance keyed by ``mcp:<id>`` group.
        # Merged live (plugin wins) with captured server instructions in
        # ``_mcp_group_guidance_live``.
        self._mcp_plugin_guidance: dict[str, str] = {}
        self._loaded_plugins = []
        # Tool-result middlewares registered by code plugins (fed to the
        # McpToolHandler chain alongside the legacy global browser_perception).
        self._plugin_middlewares: list[Any] = []
        # Brain-lane fast tools contributed by code plugins + their P14 gate
        # maps. Consumed by ``rebuild_tool_registry`` (registry) and pushed
        # to ``TurnRunner.set_plugin_tool_gate`` (gate families / patterns).
        self._plugin_fast_tools: list[Any] = []
        self._plugin_tool_families: dict[str, str] = {}
        self._plugin_family_patterns: dict[str, Any] = {}
        if not bool(getattr(agent, "mcp_clients_enabled", True)):
            return
        mcp_clients = getattr(self._settings, "mcp_clients", None)  # type: ignore[attr-defined]
        servers = list(getattr(mcp_clients, "servers", []) or [])
        enabled_servers = [s for s in servers if getattr(s, "enabled", True)]

        # SDK-primary plugin bundles: the loader reads only the JSON stub +
        # plugin-local config (no code); the runtime then imports entry.py and
        # runs define_plugin(api) for enabled plugins, which register their MCP
        # server, planner guidance, and any tool-result middleware. A plugin's
        # server id shadows a same-id config server (plugin wins).
        try:
            plugins_cfg = getattr(self._settings, "plugins", None)  # type: ignore[attr-defined]
            if plugins_cfg is None or bool(getattr(plugins_cfg, "enabled", True)):
                from app.plugins.loader import (
                    default_plugin_roots,
                    discover_plugins,
                )
                from app.plugins.runtime import activate_all

                extra_paths = list(getattr(plugins_cfg, "paths", []) or []) if plugins_cfg else []
                roots = default_plugin_roots() + extra_paths
                entries_cfg = (
                    getattr(plugins_cfg, "entries", {}) or {} if plugins_cfg else {}
                )
                entries = {
                    pid: {
                        "enabled": getattr(entry, "enabled", None),
                        "config": dict(getattr(entry, "config", {}) or {}),
                    }
                    for pid, entry in entries_cfg.items()
                }
                stubs = discover_plugins(roots, entries=entries)
                activated = activate_all(stubs)
                self._loaded_plugins = activated
                plugin_servers = []
                for plugin in activated:
                    if plugin.status != "active":
                        continue
                    self._mcp_plugin_guidance.update(plugin.group_guidance)
                    if plugin.middlewares:
                        self._plugin_middlewares.extend(plugin.middlewares)
                    if getattr(plugin, "fast_tools", None):
                        self._collect_plugin_fast_tools(plugin.fast_tools)
                    if plugin.server is not None:
                        plugin_servers.append(plugin.server)
                if plugin_servers:
                    plugin_ids = {s.id for s in plugin_servers}
                    # Plugin shadows a same-id config server (plugin wins).
                    enabled_servers = [
                        s
                        for s in enabled_servers
                        if getattr(s, "id", None) not in plugin_ids
                    ] + plugin_servers
                    log.info(
                        "external-mcp: %d plugin server(s) loaded: %s",
                        len(plugin_ids),
                        sorted(plugin_ids),
                    )
        except Exception as exc:
            log.warning("external-mcp: plugin discovery failed: %r", exc)

        # Remember the final server set so playbook root-lookup can consult
        # plugin-synthesised servers (not just mcp_clients.servers).
        self._active_mcp_servers = {
            getattr(s, "id", ""): s for s in enabled_servers
        }

        if not enabled_servers:
            log.debug("external-mcp: no enabled servers configured")
            return
        try:
            from app.core.tasks.handlers.mcp_tool import McpToolHandler
            from app.core.tasks.workflow.mcp_skills import register_mcp_skills
            from app.mcp.client.manager import ExternalMcpManager

            # Tool-result middleware chain: registered entirely by plugins
            # (e.g. the browser plugin's BrowserPerception). Core no longer
            # builds any perception itself.
            middlewares = list(self._plugin_middlewares)

            # Expose a perception-style middleware (one with ``debug_state``)
            # to the browser debug MCP tools.
            for mw in middlewares:
                if hasattr(mw, "debug_state"):
                    self._browser_perception = mw
                    break

            manager = ExternalMcpManager(enabled_servers)
            # Re-register skills whenever a server's catalogue changes
            # (initial connect or reconnect). Fired on the manager loop
            # thread; ``register`` is dict-write cheap + GIL-atomic.
            manager.set_tools_changed_callback(
                lambda: register_mcp_skills(skill_registry, manager)
            )
            self._task_orchestrator.register_handler(
                McpToolHandler(manager=manager, middlewares=middlewares)
            )
            manager.start()
            # Best-effort immediate pass (usually 0 — connect is async);
            # the callback above lands the real catalogue once connected.
            register_mcp_skills(skill_registry, manager)
            self._external_mcp_manager = manager
            log.info(
                "external-mcp: manager started servers=%d",
                len(enabled_servers),
            )
        except Exception as exc:
            log.warning("external-mcp: manager init failed: %r", exc)
            self._external_mcp_manager = None

    def _collect_plugin_fast_tools(self, specs: list[Any]) -> None:
        """Collect plugin fast-tool specs + fold their P14 gate maps.

        Each spec becomes a brain ``Tool`` at ``rebuild_tool_registry`` time.
        A spec with both a ``family`` and ``gate_patterns`` also wires the
        gate: the tool name maps to its family, and the family's regexes are
        merged (compiled as one alternation) so the P14 gate / skill router
        can skip / narrow like the builtins. A spec with no family (or a
        family with no patterns) is left unmapped -- the gate then degrades
        to always-run for it (safe, just un-optimized).
        """
        from app.core.session.tool_pass_gate import _compile

        # Accumulate raw pattern strings per family across plugins, then
        # (re)compile the affected families into one alternation each.
        pattern_strs: dict[str, list[str]] = getattr(
            self, "_plugin_family_pattern_strs", {}
        )
        for spec in specs or []:
            name = str(getattr(spec, "name", "") or "").strip()
            if not name:
                continue
            self._plugin_fast_tools.append(spec)
            family = getattr(spec, "family", None)
            patterns = tuple(getattr(spec, "gate_patterns", ()) or ())
            if family and patterns:
                self._plugin_tool_families[name] = str(family)
                pattern_strs.setdefault(str(family), []).extend(patterns)
        self._plugin_family_pattern_strs = pattern_strs
        for family, words in pattern_strs.items():
            if words:
                try:
                    self._plugin_family_patterns[family] = _compile(words)
                except Exception:
                    log.warning(
                        "plugin fast-tool gate patterns invalid for family %s",
                        family,
                        exc_info=True,
                    )

    def _mcp_group_guidance_live(self) -> dict[str, str]:
        """Live merged planner guidance keyed by ``mcp:<id>`` group.

        Precedence: **plugin SKILL.md > runtime-captured server
        instructions**. The captured layer is read from the live manager
        each call (so a server connecting after boot starts contributing
        without a rebuild); the plugin layer is stamped at load time. Both
        override the hardcoded playbooks inside ``guidance_for_skills``.
        """
        merged: dict[str, str] = {}
        manager = getattr(self, "_external_mcp_manager", None)
        if manager is not None:
            try:
                merged.update(manager.captured_group_guidance())
            except Exception:
                log.debug("captured guidance read failed", exc_info=True)
        merged.update(getattr(self, "_mcp_plugin_guidance", {}) or {})
        return merged

    def loaded_plugins(self) -> list[Any]:
        """Snapshot of discovered plugin bundles (for the MCP debug tools)."""
        return list(getattr(self, "_loaded_plugins", []) or [])

    def reload_plugin_guidance(self) -> dict[str, Any]:
        """Re-run plugin discovery and refresh planner guidance live.

        Guidance only — the planner reads ``_mcp_plugin_guidance`` live via
        ``_mcp_group_guidance_live``, so an edited ``SKILL.md`` lands on the
        next workflow. Adding/removing an MCP SERVER still needs a restart
        (the manager's connections are built once at boot).
        """
        plugins_cfg = getattr(getattr(self, "_settings", None), "plugins", None)
        summary: dict[str, Any] = {
            "active": [], "gated_out": [], "invalid": [], "disabled": [],
        }
        try:
            from app.plugins.loader import (
                default_plugin_roots,
                discover_plugins,
            )
            from app.plugins.runtime import activate_all

            if plugins_cfg is not None and not bool(
                getattr(plugins_cfg, "enabled", True)
            ):
                return {"enabled": False, "reason": "plugins disabled"}
            extra_paths = (
                list(getattr(plugins_cfg, "paths", []) or []) if plugins_cfg else []
            )
            entries_cfg = (
                getattr(plugins_cfg, "entries", {}) or {} if plugins_cfg else {}
            )
            entries = {
                pid: {
                    "enabled": getattr(entry, "enabled", None),
                    "config": dict(getattr(entry, "config", {}) or {}),
                }
                for pid, entry in entries_cfg.items()
            }
            stubs = discover_plugins(
                default_plugin_roots() + extra_paths, entries=entries
            )
            activated = activate_all(stubs)
            self._loaded_plugins = activated
            new_guidance: dict[str, str] = {}
            for plugin in activated:
                bucket = summary.get(plugin.status)
                if bucket is not None:
                    bucket.append(plugin.id)
                if plugin.status == "active":
                    new_guidance.update(plugin.group_guidance)
            self._mcp_plugin_guidance = new_guidance
            log.info(
                "plugins reloaded: active=%s gated=%s invalid=%s",
                summary["active"],
                summary["gated_out"],
                summary["invalid"],
            )
        except Exception as exc:
            summary["error"] = repr(exc)
            log.warning("plugin guidance reload failed: %r", exc)
        return summary

    def _record_workflow_capability_gap(self, gap: dict[str, Any]) -> None:
        """Sink for goal-workflow capability gaps (bounded ring).

        Called from the :class:`GoalWorkflowHandler` daemon thread when
        the planner declares a ``missing_capability``. Stored so the MCP
        ``list_capability_gaps`` tool + the ``check_my_work`` brain tool
        can report "things I couldn't do yet". Best-effort + thread-safe
        enough for a deque append (CPython GIL makes the append atomic).
        """
        ring = getattr(self, "_workflow_capability_gaps", None)
        if ring is None:
            return
        try:
            ring.append(dict(gap))
        except Exception:
            log.debug("capability gap append failed", exc_info=True)

    def workflow_capability_gaps(self) -> list[dict[str, Any]]:
        """Snapshot of recorded capability gaps (most-recent-last)."""
        ring = getattr(self, "_workflow_capability_gaps", None)
        if ring is None:
            return []
        return list(ring)

    # ── approval policy (reusable across destructive capabilities) ────

    def _resolve_task_approval(self, capability_id: str) -> str:
        """Resolve the effective approval mode for ``capability_id``.

        Injected into every destructive handler. Reads the persistent
        policy (``agent.task_approval_mode`` + ``task_approval_overrides``)
        and layers the in-memory session approve-all set on top. Returns
        ``"auto"`` (proceed silently) or ``"ask"`` (gate). Best-effort:
        any failure reads as ``"ask"`` so a config glitch never silently
        skips an approval.
        """
        from app.core.tasks.approval import MODE_ASK, resolve_approval

        try:
            agent = self._settings.agent  # type: ignore[attr-defined]
            mode = str(getattr(agent, "task_approval_mode", "ask") or "ask")
            overrides = dict(getattr(agent, "task_approval_overrides", {}) or {})
            session_approved = getattr(self, "_approved_capabilities", None)
            return resolve_approval(
                capability_id,
                mode=mode,
                overrides=overrides,
                session_approved=session_approved or (),
            )
        except Exception:
            log.debug("approval resolve failed; defaulting to ask", exc_info=True)
            return MODE_ASK

    def _mark_capability_session_approved(self, capability_id: str) -> None:
        """Record an 'approve all' click for ``capability_id`` this session.

        Adds the capability id to the in-memory set so
        :meth:`_resolve_task_approval` returns ``"auto"`` for it for the
        rest of the session. Never persisted.
        """
        approved = getattr(self, "_approved_capabilities", None)
        if approved is None:
            approved = set()
            self._approved_capabilities = approved  # type: ignore[attr-defined]
        try:
            approved.add(str(capability_id))
            log.info(
                "task approval: session approve-all set for capability=%s",
                capability_id,
            )
        except Exception:
            log.debug("mark session approved failed", exc_info=True)

    def approvals_state(self) -> dict[str, Any]:
        """Diagnostic snapshot for the MCP ``get_approvals_state`` tool."""
        from app.core.tasks.capabilities import all_capabilities

        agent = getattr(self._settings, "agent", None)  # type: ignore[attr-defined]
        return {
            "mode": str(getattr(agent, "task_approval_mode", "ask") or "ask"),
            "overrides": dict(
                getattr(agent, "task_approval_overrides", {}) or {}
            ),
            "session_approved": sorted(
                getattr(self, "_approved_capabilities", set()) or set()
            ),
            "capabilities": [
                {
                    "id": cap.id,
                    "label": cap.label,
                    "destructive": cap.destructive,
                    "effective_mode": self._resolve_task_approval(cap.id),
                }
                for cap in all_capabilities()
            ],
        }

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
        # External MCP manager last: closes sessions + terminates the
        # child processes (stdio servers). After the orchestrator stops
        # so no handler thread is mid-``call_tool``.
        if getattr(self, "_external_mcp_manager", None) is not None:
            try:
                self._external_mcp_manager.stop()
            except Exception:
                log.debug("external-mcp manager stop failed", exc_info=True)
            self._external_mcp_manager = None
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

    def _any_tasks_active(self) -> bool:
        """True when any task is running / awaiting_input / paused.

        P14 continuity hook for the tool-pass gate: while a task is
        live, the user's next message may be the answer a pending
        ``answer_file_task`` is waiting for (or a cancel / status
        request), so the forced tool-decision pass must always run.
        Best-effort — any failure reads as "no active tasks" so the
        gate's text heuristic still applies.
        """
        store = getattr(self, "_task_store", None)
        if store is None:
            return False
        try:
            return bool(store.list_running())
        except Exception:
            log.debug("_any_tasks_active failed", exc_info=True)
            return False

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
        from app.core.tasks.cue_render import (
            render_cue_block,
            render_reply_block,
        )
        from app.core.tasks.task_cue_store import CUE_KIND_RESULT

        agent = self._settings.agent  # type: ignore[attr-defined]
        # Split finished ``reply_when_done`` result cues (rendered with
        # their FULL content so the turn can answer the user directly)
        # from everything else (terse bullet cues). This is what stops
        # the "task done but no content -> re-run the task" failure.
        reply_items: list[dict[str, str]] = []
        terse_cues = []
        for cue in result.surfaced:
            full = None
            if cue.kind == CUE_KIND_RESULT and cue.status == "done":
                full = self._reply_item_for_cue(cue)
            if full is not None:
                reply_items.append(full)
            else:
                terse_cues.append(cue)
        parts: list[str] = []
        if reply_items:
            parts.append(render_reply_block(reply_items))
        if terse_cues:
            parts.append(
                render_cue_block(
                    terse_cues,
                    max_aggregated=int(
                        getattr(agent, "task_cue_max_aggregated", 5)
                    ),
                )
            )
        return "\n\n".join(p for p in parts if p)

    def _reply_item_for_cue(self, cue: Any) -> dict[str, str] | None:
        """Build a full-content reply item for a finished cue.

        Returns ``None`` when the cue's task is not ``reply_when_done``
        (so it falls back to the terse bullet render) or when the row /
        result can't be loaded. The ``content`` prefers the result's
        ``content`` field (file_read) and falls back to ``summary``
        (file_search) so both handlers narrate naturally.
        """
        orch = getattr(self, "_task_orchestrator", None)
        if orch is None:
            return None
        try:
            # Event/cue task ids are the 8-char hex render from
            # TaskOrchestrator._format_task_id (e.g. "0000000a" for row
            # 10) — parse base-16, NOT base-10. Base-10 silently worked
            # for ids 1-9 and broke at 10 (first hex letter).
            row = orch.get(int(str(cue.task_id), 16))
        except Exception:
            return None
        meta = getattr(row, "metadata", None) if row is not None else None
        if not (isinstance(meta, dict) and meta.get("reply_when_done")):
            return None
        result = getattr(row, "result", None)
        if not isinstance(result, dict):
            return None
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            content = result.get("summary")
        if not isinstance(content, str) or not content.strip():
            content = cue.summary
        return {
            "title": cue.title or "",
            "origin_prompt": str(meta.get("origin_prompt", "") or ""),
            "content": str(content or ""),
        }

    # ── inline-resolution suppression ────────────────────────────────

    def mark_task_inline_resolved(self, task_id: int) -> None:
        """Record that a task already reported its result inline.

        Called from the file-task tools when the inline grace window
        caught a terminal status: the result was folded into the same
        turn, so the later ``task_result`` brain event must NOT park a
        duplicate cue or fire a second proactive reply. The set is
        consumed (popped) by :meth:`_on_task_result_event`.
        """
        ids = getattr(self, "_task_inline_resolved_ids", None)
        if ids is None:
            ids = set()
            self._task_inline_resolved_ids = ids  # type: ignore[attr-defined]
        try:
            ids.add(int(task_id))
        except (TypeError, ValueError):
            pass

    def _consume_task_inline_resolved(self, task_id: Any) -> bool:
        """Return True (and forget) if ``task_id`` was inline-resolved."""
        ids = getattr(self, "_task_inline_resolved_ids", None)
        if not ids:
            return False
        try:
            tid = int(str(task_id), 16)  # hex event id -> row id (see _format_task_id)
        except (TypeError, ValueError):
            return False
        if tid in ids:
            ids.discard(tid)
            return True
        return False

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

        The :class:`TaskOrchestrator` uses this when emitting events so
        the brain loop's downstream consumers know which session the
        task belongs to. The active chat session key is
        ``"{user_id}:{session_id}"`` (see ``SessionController.session_key``),
        NOT bare ``user_id`` — a finished-task proactive turn dispatched
        against bare ``user_id`` lands on an empty history, so
        ``ProactiveDirector._run_typed`` reads zero messages and silently
        no-ops ("no history yet" at DEBUG). When the task's user matches
        the controller's active user (the single-user install case) we
        return the live session key; otherwise fall back to bare
        ``user_id`` as a best-effort default.
        """
        try:
            if str(getattr(self, "_user_id", "") or "") == str(user_id):
                key = self.session_key
                if key:
                    return str(key)
        except Exception:
            log.debug("task session-key resolve failed", exc_info=True)
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
        attachments = list(getattr(event, "attachments", ()) or ())
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
                attachments=attachments,
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
        attachments: "list[dict] | tuple[dict, ...] | None" = None,
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
        attachment_tuple: tuple[dict, ...] = tuple(attachments or ())
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
                if attachment_tuple:
                    direct_kwargs["attachments"] = list(attachment_tuple)
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
            attachments=attachment_tuple,
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

        Routes the finished task through the C6 report decision
        (:meth:`_dispatch_task_report`): ``surface_now`` parks a cue +
        arms the escalation timer so it fires the moment Aiko is free,
        ``park`` parks silently for the next natural turn, ``drop`` does
        nothing. Floor (user-requested) tasks always surface.

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
        # K43 — auto-fulfil matching promises: "I'll look into X" followed
        # by a finished background task about X closes the loop without
        # waiting for the next reply to mention it. Best-effort; lexical
        # match runs in promise_lifecycle.find_fulfilled.
        if str(getattr(event, "status", "") or "") == "done":
            try:
                self._maybe_resolve_promises(
                    f"{event.title or ''} {event.result_summary or ''}",
                    source="task",
                )
            except Exception:
                log.debug(
                    "task-completion promise resolution failed", exc_info=True,
                )
        # Suppress the duplicate report when the spawning tool already
        # folded this task's result into the same turn (inline fast
        # path). The id was stashed by ``mark_task_inline_resolved``.
        if self._consume_task_inline_resolved(event.task_id):
            log.info(
                "task reply suppressed (already reported inline): task=%s",
                event.task_id,
            )
            return
        cue_store = getattr(self, "_task_cue_store", None)
        escalation = getattr(self, "_task_escalation_manager", None)
        if cue_store is None or escalation is None:
            return
        self._dispatch_task_report(event, cue_store, escalation)

    # ── C6: worker-model report decision ─────────────────────────────

    def _dispatch_task_report(
        self, event: Any, cue_store: Any, escalation: Any,
    ) -> None:
        """Route a finished, reportable task through the C6 decision.

        Three tiers (see ``docs/personality-backlog`` C6):

        * **floor** — the user explicitly asked for this
          (``initiated_by == 'aiko'`` and not ``metadata.self_initiated``).
          Always reports: park + arm immediately (latency unchanged).
          When the decision is enabled, an async worker pass *also* runs
          to (a) shadow-log the verdict it *would* have produced and
          (b) best-effort enrich the parked cue with the drafted angle.
          Set ``task_report_decision_floor_mode='enforce'`` to make the
          verdict authoritative for the floor too.
        * **discretionary** — self/background-initiated. The async
          worker decides: ``surface_now`` -> park(angle) + arm;
          ``park_for_natural_opening`` -> park(angle), no escalation;
          ``drop`` -> nothing.
        * Decision disabled -> behaves exactly as before (park + arm).
        """
        enabled = bool(
            getattr(self._settings.agent, "task_report_decision_enabled", True)
        )
        worker_available = bool(
            getattr(self, "_maintenance_client", None) is not None
            and getattr(self, "_effective_worker_model", None)
        )
        provenance, is_floor = self._task_report_provenance(event.task_id)

        # Decision off, no worker to run it, or a floor task we won't
        # gate -> the legacy park+arm path.
        floor_mode = str(
            getattr(
                self._settings.agent,
                "task_report_decision_floor_mode",
                "shadow",
            )
        ).strip().lower()
        legacy_park = (
            (not enabled)
            or (not worker_available)
            or (is_floor and floor_mode != "enforce")
        )

        if legacy_park:
            cue = cue_store.park(
                task_id=event.task_id,
                session_key=event.session_key,
                kind=CUE_KIND_RESULT,
                title=event.title,
                status=event.status,
                summary=event.result_summary,
                error=event.error,
            )
            # Floor (user-requested) tasks always report: arm so the cue
            # surfaces the moment Aiko is free. When the decision is
            # disabled / has no worker, a non-floor (self-initiated) task
            # parks WITHOUT arming — it folds into the next natural turn
            # rather than interrupting off the back of a missing verdict.
            if is_floor:
                escalation.arm(cue)
            if enabled and worker_available:
                # Floor stays forced; run the worker pass async to
                # shadow-log + enrich the cue with the drafted angle.
                self._spawn_task_report_decision(
                    event, cue_store, escalation,
                    provenance=provenance, shadow=True,
                )
            return

        # Enforced path (discretionary tasks always, floor when
        # floor_mode='enforce'): the worker decides whether/how to park.
        self._spawn_task_report_decision(
            event, cue_store, escalation,
            provenance=provenance, shadow=False,
        )

    def _task_report_provenance(self, task_id: Any) -> tuple[str, bool]:
        """Return ``(provenance_label, is_floor)`` for a task id.

        ``is_floor`` is True for user-requested work (the hard
        always-report tier). Today every ``initiated_by='aiko'`` spawn
        is user-requested; a future Aiko-self-trigger path sets
        ``metadata.self_initiated=True`` to opt into the discretionary
        tier without touching this gate.
        """
        orch = getattr(self, "_task_orchestrator", None)
        if orch is None:
            return PROVENANCE_USER, True
        try:
            row = orch.get(int(str(task_id), 16))
        except Exception:
            row = None
        if row is None:
            return PROVENANCE_USER, True
        initiated_by = str(getattr(row, "initiated_by", INITIATED_BY_AIKO))
        meta = getattr(row, "metadata", None)
        self_initiated = bool(
            isinstance(meta, dict) and meta.get("self_initiated")
        )
        is_floor = initiated_by == INITIATED_BY_AIKO and not self_initiated
        provenance = PROVENANCE_USER if is_floor else PROVENANCE_SELF
        return provenance, is_floor

    def _spawn_task_report_decision(
        self,
        event: Any,
        cue_store: Any,
        escalation: Any,
        *,
        provenance: str,
        shadow: bool,
    ) -> None:
        """Run the worker decision off-thread, then act on the verdict.

        Never blocks the brain-loop consumer (same daemon-thread pattern
        as ``ProactiveDirector.notify_task_escalation``).
        """
        import threading

        threading.Thread(
            target=self._run_task_report_decision_safe,
            args=(event, cue_store, escalation, provenance, shadow),
            daemon=True,
            name=f"task-report-decision-{event.task_id}",
        ).start()

    def _run_task_report_decision_safe(
        self,
        event: Any,
        cue_store: Any,
        escalation: Any,
        provenance: str,
        shadow: bool,
    ) -> None:
        try:
            self._run_task_report_decision(
                event, cue_store, escalation, provenance, shadow,
            )
        except Exception:
            log.debug("task-report decision thread raised", exc_info=True)
            # Conservative recovery: in the enforced path a crash must
            # not silently swallow a result, so park (no escalation) so
            # it can still fold into the next natural turn.
            if not shadow:
                try:
                    cue_store.park(
                        task_id=event.task_id,
                        session_key=event.session_key,
                        kind=CUE_KIND_RESULT,
                        title=event.title,
                        status=event.status,
                        summary=event.result_summary,
                        error=event.error,
                    )
                except Exception:
                    log.debug("task-report fallback park failed", exc_info=True)

    def _run_task_report_decision(
        self,
        event: Any,
        cue_store: Any,
        escalation: Any,
        provenance: str,
        shadow: bool,
    ) -> None:
        angle_enabled = bool(
            getattr(self._settings.agent, "task_report_angle_enabled", True)
        )
        origin_prompt = self._task_origin_prompt(event.task_id)
        arc, idle_seconds, gist = self._report_decision_context()
        verdict = decide_task_report(
            ollama=getattr(self, "_maintenance_client", None),
            model=getattr(self, "_effective_worker_model", None),
            title=event.title or "",
            summary=event.result_summary or "",
            status=event.status or "done",
            provenance=provenance,
            origin_prompt=origin_prompt,
            user_display_name=getattr(self, "user_display_name", "the user"),
            arc=arc,
            idle_seconds=idle_seconds,
            recent_assistant_gist=gist,
        )
        self._record_task_report_verdict(event, provenance, shadow, verdict)
        log.info(
            "task-report-decision%s: task=%s provenance=%s action=%s reason=%s",
            " (shadow)" if shadow else "",
            event.task_id,
            provenance,
            verdict.action,
            verdict.reason,
        )

        if shadow:
            # Floor cue is already parked + armed; just enrich the angle.
            if angle_enabled and verdict.angle:
                try:
                    cue_store.set_angle(str(event.task_id), verdict.angle)
                except Exception:
                    log.debug("task-report angle enrich failed", exc_info=True)
            return

        # Enforced path: act on the verdict.
        if verdict.action == ACTION_DROP:
            return
        cue = cue_store.park(
            task_id=event.task_id,
            session_key=event.session_key,
            kind=CUE_KIND_RESULT,
            title=event.title,
            status=event.status,
            summary=event.result_summary,
            error=event.error,
            angle=verdict.angle if angle_enabled else "",
        )
        if verdict.action == ACTION_SURFACE:
            # surface_now: fire the moment Aiko is free (no fixed window).
            escalation.arm(cue)
        # ACTION_PARK: parked above, no escalation -> folds into the
        # next natural turn via drain_for_render.

    def _task_origin_prompt(self, task_id: Any) -> str:
        """Best-effort original request text from the task metadata."""
        orch = getattr(self, "_task_orchestrator", None)
        if orch is None:
            return ""
        try:
            row = orch.get(int(str(task_id), 16))
        except Exception:
            return ""
        meta = getattr(row, "metadata", None) if row is not None else None
        if isinstance(meta, dict):
            return str(meta.get("origin_prompt", "") or "")
        return ""

    def _report_decision_context(self) -> tuple[str, float | None, str]:
        """Best-effort live signals for the report decision.

        Returns ``(arc_label, idle_seconds, recent_assistant_gist)``;
        each component degrades to a neutral default on any failure so
        the decision never crashes on a missing dependency.
        """
        arc = ""
        try:
            store = getattr(self, "_arc_store", None)
            if store is not None:
                state = store.get(self._user_id)
                arc = str(getattr(state, "arc", "") or "")
        except Exception:
            arc = ""

        idle_seconds: float | None = None
        gist = ""
        try:
            messages = self._chat_db.get_messages(self.session_key)
        except Exception:
            messages = []
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            for row in reversed(messages):
                role = (getattr(row, "role", "") or "").lower()
                if idle_seconds is None and role == "user":
                    ts_raw = getattr(row, "created_at", None)
                    if ts_raw:
                        ts = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00"),
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        idle_seconds = max(0.0, (now - ts).total_seconds())
                if not gist and role == "assistant":
                    gist = str(getattr(row, "content", "") or "")
                if idle_seconds is not None and gist:
                    break
        except Exception:
            pass
        return arc, idle_seconds, gist

    def _record_task_report_verdict(
        self, event: Any, provenance: str, shadow: bool, verdict: Any,
    ) -> None:
        """Append the verdict to a small in-memory ring for MCP debug."""
        ring = getattr(self, "_task_report_verdicts", None)
        if ring is None:
            import collections

            ring = collections.deque(maxlen=20)
            self._task_report_verdicts = ring  # type: ignore[attr-defined]
        ring.append(
            {
                "task_id": str(event.task_id),
                "title": str(event.title or ""),
                "provenance": provenance,
                "shadow": bool(shadow),
                "action": verdict.action,
                "angle": verdict.angle,
                "reason": verdict.reason,
            }
        )

    def _on_task_input_needed_event(self, event: Any) -> None:
        """Handle a ``task_input_needed`` brain event — UI-only.

        A task that needs clarifying input (e.g. the file_read
        multi-root disambiguation, or a workflow's destructive-write
        approval) surfaces as a clickable ``awaiting_input`` chip in the
        TaskStrip — wired by the orchestrator's input-needed listener
        straight to the WS ``tasksView`` slice. The chip is non-terminal,
        so it stays visible until the user answers or cancels: they can
        resolve it whenever they like.

        Aiko does NOT speak the question. Verbal in-conversation asking
        is a deferred, opt-in addition (backlog); for now this handler
        deliberately parks no chat cue and arms no escalation timer — the
        strip owns the whole surface.
        """
        if not isinstance(event, TaskInputNeededEvent):
            log.debug(
                "task_input_needed handler received wrong type: %r",
                type(event).__name__,
            )
            return
        log.info(
            "task_input_needed UI-only (TaskStrip surfaces it): task=%s",
            event.task_id,
        )

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

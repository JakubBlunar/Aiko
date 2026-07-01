"""GoalWorkflowHandler — the parent task that runs a nested workflow.

This is the multi-step orchestrator the user asked for: "search for new
files → decide what to do → read them → reply about what I found." It's
a single :class:`TaskHandler` whose ``start`` launches a daemon thread
running a plan→act→observe loop:

1. **Plan** — ask the workflow planner (worker LLM, TASK priority) for
   the next action given the goal + everything observed so far.
2. **Act** — if the planner picked a skill, spawn it as a CHILD task
   under this workflow via the :class:`WorkflowSkillRegistry`, then
   block on the child's terminal status.
3. **Observe** — fold a short summary of the child's result onto the
   blackboard and loop.

The loop ends when the planner picks ``finish`` (success / partial /
nothing_found), declares a ``missing_capability`` (Aiko then says "I
don't know how to do that yet" and names the gap), or a cap is hit
(max iterations / max children / repeat guard).

Why a self-spawned daemon thread rather than running inline on the
orchestrator's worker thread? Because the loop *waits on child tasks*,
which themselves run on the orchestrator's pool. Blocking a pool worker
for the whole multi-minute workflow would starve the pool. The handler
returns immediately (leaving the row ``running``) and emits the terminal
outcome from the daemon thread — the documented "handler spawns its own
threads and emits later" pattern.

Cancellation is cooperative: the loop polls its own row status each
iteration (and right after each child wait), so a user
``cancel_work`` / cascade-cancel stops it at the next boundary. The
orchestrator's cascade-cancel takes care of any in-flight child, which
unblocks the parent's wait promptly.

Gate behaviour: the planner calls go through the injected ``worker LLM``
client (the TASK-tier gated proxy), and the proxy acquires the gate
**per call** — so while the workflow is blocked waiting on a child, it
holds NO gate slot and never inverts priority against the conversation
workers.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from collections import Counter, deque
from typing import Any, Callable

from app.core.infra.log_context import get_task_id
from app.core.tasks.handler_names import HANDLER_GOAL_WORKFLOW
from app.core.tasks.task_handler import (
    STATUS_AWAITING_INPUT,
    TERMINAL_STATUSES,
    TaskCompleted,
    TaskEmitFn,
    TaskEventEmit,
    TaskFailed,
    TaskProgress,
    TaskState,
)
from app.core.tasks.workflow.skill_registry import (
    SpawnContext,
    WorkflowSkillRegistry,
)
from app.core.tasks.workflow.skill_guidance import guidance_for_skills
from app.core.tasks.workflow.workflow_skill_router import select_skill_groups
from app.core.tasks.workflow.workflow_planner import (
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    PlannerInput,
    PlannerStep,
    decide_next_action,
)


log = logging.getLogger("app.tasks.workflow.handler")


# Sentinel outcome stamped on the result when the workflow stopped
# because it needs a capability no skill provides.
OUTCOME_MISSING_CAPABILITY = "missing_capability"

# Sentinel outcome stamped when the workflow stopped because it got
# STUCK — it kept repeating a step, looping on the same result, or a
# tool kept failing — as distinct from cleanly running out of the
# iteration / child / wall-clock budget (which stays ``partial``).
# ``blocked`` is the "I couldn't finish and I need help" signal: the
# findings ask the user for a hand rather than pretending progress.
OUTCOME_BLOCKED = "blocked"

# Audit-event types appended to ``task_events`` (schema v17) when the
# loop stops itself. Pure audit — they don't change the row's hot state.
EVENT_WORKFLOW_LOOP_DETECTED = "workflow_loop_detected"
EVENT_WORKFLOW_BLOCKED = "workflow_blocked"

_CHILD_OBS_CAP = 500

# Signature normalisation cap for the no-progress detector.
_SIGNATURE_OBS_CAP = 200
_WS_RE = re.compile(r"\s+")


def _observation_signature(status: str, observation: str) -> str:
    """Stable signature of a completed step's *result* (not its args).

    The exact-``(skill, args)`` repeat guard catches a planner that
    re-issues an identical call; this signature is coarser — it folds a
    step down to ``status|normalised-observation`` so the no-progress
    detector can catch a planner that keeps issuing *different* calls
    that all land on the same result (e.g. varied browser clicks that
    each report the same failure, or repeated reads returning the same
    body). Args are intentionally excluded.
    """
    obs = _WS_RE.sub(" ", (observation or "").strip().lower())[:_SIGNATURE_OBS_CAP]
    return f"{status or 'unknown'}|{obs}"


def _task_id_from_context() -> int | None:
    """Recover the integer task id from the log-context contextvar.

    The orchestrator sets it as an 8-char hex string before invoking
    ``start``; we parse it back. Returns ``None`` when not running under
    a task context (shouldn't happen in production, but keeps tests that
    call ``start`` directly from crashing).
    """
    hex_id = get_task_id()
    if not hex_id:
        return None
    try:
        return int(str(hex_id), 16)
    except (TypeError, ValueError):
        return None


def _summarize_child(row: Any, status: str) -> str:
    """Build a short observation string from a finished child row."""
    if row is None:
        return f"[{status}] (no result row)"
    result = getattr(row, "result", None)
    if isinstance(result, dict):
        # Prefer the actual returned ``content`` over the terse
        # ``summary``: the planner *reasons* over this observation and
        # writes the final findings from it, so it needs the real data
        # (a full directory listing, a file's text). Some handlers pack a
        # one-line ``summary`` that collapses a multi-line result to its
        # first line (the MCP tool handler did exactly this) — using that
        # here made the planner see only the FIRST entry of a listing and
        # report just that. ``content`` is bounded by ``_CHILD_OBS_CAP``.
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:_CHILD_OBS_CAP]
        summary = result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()[:_CHILD_OBS_CAP]
        # Generic: list the result keys.
        keys = ", ".join(map(str, list(result.keys())[:8]))
        return f"result keys: {keys}"[:_CHILD_OBS_CAP]
    error = getattr(row, "error", None)
    if isinstance(error, str) and error.strip():
        return f"error: {error.strip()}"[:_CHILD_OBS_CAP]
    return f"[{status}]"


def _canonical_args(args: dict[str, Any]) -> str:
    """Stable string key for the repeat guard."""
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(sorted((args or {}).items()))


class GoalWorkflowHandler:
    """Parent task handler driving the plan→act→observe loop.

    Dependencies are injected as *providers* (zero-arg callables) where
    they can change at runtime (the worker client + model are rebuilt on
    ``reconfigure_chat_llm``), and as direct refs where they're stable
    (the orchestrator + skill registry).
    """

    name: str = HANDLER_GOAL_WORKFLOW

    def __init__(
        self,
        *,
        orchestrator: Any,
        skill_registry: WorkflowSkillRegistry,
        worker_client_provider: Callable[[], Any],
        model_provider: Callable[[], str | None] | None = None,
        user_name_provider: Callable[[], str] | None = None,
        on_capability_gap: Callable[[dict[str, Any]], None] | None = None,
        max_iterations: int = 6,
        max_children: int = 8,
        child_wait_timeout_seconds: float = 120.0,
        planner_history_budget_chars: int = 4000,
        planner_max_tokens: int = 512,
        skill_router_enabled_provider: Callable[[], bool] | None = None,
        max_consecutive_failures: int = 2,
        max_wall_seconds: float = 300.0,
        loop_detection_enabled: bool = True,
        loop_window: int = 4,
        loop_repeat_threshold: int = 3,
        group_guidance_provider: "Callable[[], dict[str, str]] | None" = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._skills = skill_registry
        self._worker_client_provider = worker_client_provider
        self._model_provider = model_provider
        self._user_name_provider = user_name_provider
        self._on_capability_gap = on_capability_gap
        # Worker-lane skill router: when the provider returns True, the
        # planner menu is narrowed to the goal's group(s) (full-menu
        # fallback on ambiguity). None / False = today's full menu.
        self._skill_router_enabled_provider = skill_router_enabled_provider
        self._max_iterations = max(1, int(max_iterations))
        self._max_children = max(1, int(max_children))
        self._child_wait_timeout = max(1.0, float(child_wait_timeout_seconds))
        self._history_budget = max(500, int(planner_history_budget_chars))
        self._planner_max_tokens = max(64, int(planner_max_tokens))
        # Robustness limits: stop a workflow that keeps failing (e.g. a
        # browser goal when Chrome isn't running) instead of grinding
        # through every iteration on slow timeouts.
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._max_wall_seconds = max(0.0, float(max_wall_seconds))
        # No-progress / loop detector: over a rolling window of the last
        # ``loop_window`` completed steps, if the SAME result signature
        # (status|observation, args-agnostic) recurs ``loop_repeat_threshold``
        # times the workflow stops as ``blocked`` (stuck) rather than
        # grinding to the iteration cap. Complements the exact-repeat guard
        # (which needs identical args) and the consecutive-failure breaker
        # (which needs actual failures — a loop can be all-``done``).
        self._loop_detection_enabled = bool(loop_detection_enabled)
        self._loop_window = max(2, int(loop_window))
        self._loop_repeat_threshold = max(2, int(loop_repeat_threshold))
        # Live-read planner guidance map keyed by ``mcp:<server_id>`` group.
        # Sourced from plugin ``SKILL.md`` + runtime-captured server
        # instructions; the sole source of per-group operational guidance.
        self._group_guidance_provider = group_guidance_provider
        # Per-task daemon threads, keyed by task id, for shutdown joins.
        self._threads: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState:
        """Validate args + launch the loop thread; return immediately."""
        goal = str((args or {}).get("goal", "") or "").strip()
        if not goal:
            emit(TaskFailed(error="workflow goal is empty"))
            return {"args": args, "phase": "rejected"}
        task_id = _task_id_from_context()
        if task_id is None:
            emit(TaskFailed(error="workflow could not resolve its task id"))
            return {"args": args, "phase": "rejected"}
        user_id = str((args or {}).get("user_id", "") or "").strip() or "default"
        try:
            max_iter = int((args or {}).get("max_iterations", self._max_iterations))
        except (TypeError, ValueError):
            max_iter = self._max_iterations
        max_iter = max(1, min(self._max_iterations, max_iter))

        # Copy the context so the daemon thread inherits the task_id for
        # log correlation (plain threads don't inherit contextvars).
        ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: ctx.run(
                self._run_loop, task_id, user_id, goal, max_iter, emit
            ),
            name=f"workflow-{task_id:08x}",
            daemon=True,
        )
        with self._lock:
            self._threads[task_id] = thread
        log.info(
            "workflow started: task=%d goal=%r max_iter=%d user=%s",
            task_id,
            goal[:80],
            max_iter,
            user_id,
        )
        thread.start()
        # Return running state; the loop emits the terminal outcome.
        return {"args": args, "phase": "planning", "goal": goal}

    def resume(self, state: TaskState, emit: TaskEmitFn) -> TaskState:
        # A workflow surviving a restart in ``running`` can't safely
        # re-attach to its (now-orphaned) children, so fail gracefully.
        # The orchestrator's cascade-cancel handles any child rows.
        emit(
            TaskFailed(
                error=(
                    "workflow does not support resume after restart; "
                    "start it again"
                )
            )
        )
        return state

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        # The parent workflow doesn't ask the user for input directly in
        # this phase (children may, but those are answered on the child).
        emit(TaskFailed(error="workflow does not accept direct input"))
        return state

    def cancel(self, state: TaskState) -> None:
        # Cooperative: the orchestrator has already marked the row
        # ``cancelled`` and cascade-cancelled the children. The loop
        # polls its row status each iteration and stops at the next
        # boundary; the cancelled child unblocks any in-flight wait.
        return None

    # ── the loop ─────────────────────────────────────────────────────

    def _run_loop(
        self,
        task_id: int,
        user_id: str,
        goal: str,
        max_iter: int,
        emit: TaskEmitFn,
    ) -> None:
        """Plan→act→observe until finish / gap / cap. Emits terminal."""
        steps: list[PlannerStep] = []
        seen: set[str] = set()
        tried_calls: list[str] = []
        obs_window: deque[str] = deque(maxlen=self._loop_window)
        children_spawned = 0
        consecutive_failures = 0
        last_failed_skill = ""
        started_monotonic = time.monotonic()
        try:
            for iteration in range(max_iter):
                if self._is_cancelled(task_id):
                    log.info("workflow cancelled: task=%d (pre-plan)", task_id)
                    return
                # Wall-clock budget: a pile-up of slow timeouts (e.g. an
                # offline service) shouldn't run for many minutes.
                if (
                    self._max_wall_seconds > 0
                    and (time.monotonic() - started_monotonic)
                    >= self._max_wall_seconds
                ):
                    log.info(
                        "workflow wall-clock budget hit: task=%d budget=%.0fs",
                        task_id,
                        self._max_wall_seconds,
                    )
                    self._emit_finish(
                        emit, steps, OUTCOME_PARTIAL,
                        "Stopped after running longer than the time budget.",
                    )
                    return
                # If we've hit the child cap, force a finish on the next
                # plan by telling the planner there's no budget left.
                if children_spawned >= self._max_children:
                    log.info(
                        "workflow child cap reached: task=%d children=%d",
                        task_id,
                        children_spawned,
                    )
                    self._emit_finish(
                        emit, steps, OUTCOME_PARTIAL,
                        "Reached the maximum number of sub-steps.",
                    )
                    return

                emit(
                    TaskProgress(
                        message=f"thinking about step {iteration + 1}…",
                        phase="planning",
                    )
                )
                planner_skills = self._planner_skills(goal)
                decision = decide_next_action(
                    self._worker_client_provider(),
                    PlannerInput(
                        goal=goal,
                        skills=planner_skills,
                        steps=steps,
                        iteration=iteration,
                        max_iterations=max_iter,
                        history_budget_chars=self._history_budget,
                        user_name=self._user_name(),
                        guidance=self._planner_guidance(planner_skills),
                        already_tried=list(tried_calls),
                    ),
                    valid_skill_names=set(self._skills.names()),
                    model=self._model(),
                    max_tokens=self._planner_max_tokens,
                )

                if decision.is_missing_capability:
                    self._record_gap(task_id, goal, decision.missing_capability)
                    self._emit_finish(
                        emit,
                        steps,
                        OUTCOME_MISSING_CAPABILITY,
                        decision.findings
                        or (
                            "I don't know how to do that yet — I'd need to "
                            f"{decision.missing_capability}."
                        ),
                        missing_capability=decision.missing_capability,
                    )
                    return

                if decision.is_finish:
                    self._emit_finish(
                        emit,
                        steps,
                        decision.outcome or OUTCOME_SUCCESS,
                        decision.findings,
                    )
                    return

                # Skill action. Repeat guard: an exact (skill, args)
                # repeat means the planner is spinning — it's stuck, not
                # done, so this is a BLOCKED outcome (asks for help).
                canon_args = _canonical_args(decision.args)
                key = f"{decision.skill}:{canon_args}"
                if key in seen:
                    log.info(
                        "workflow repeat guard: task=%d skill=%s",
                        task_id,
                        decision.skill,
                    )
                    self._emit_blocked(
                        emit,
                        steps,
                        task_id,
                        reason="repeat_step",
                        findings=(
                            "I got stuck — I kept coming back to the same "
                            "step without making new progress, so I stopped. "
                            "Could you give me a bit more detail or point me "
                            "in the right direction?"
                        ),
                        detail={"skill": decision.skill},
                    )
                    return
                seen.add(key)
                tried_calls.append(f"{decision.skill}({canon_args})")

                emit(
                    TaskProgress(
                        message=f"running {decision.skill}…",
                        phase=decision.skill,
                    )
                )
                child_id = self._skills.spawn_child(
                    decision.skill,
                    decision.args,
                    SpawnContext(
                        orchestrator=self._orchestrator,
                        user_id=user_id,
                        parent_task_id=task_id,
                    ),
                )
                if child_id is None:
                    steps.append(
                        PlannerStep(
                            skill=decision.skill,
                            args=decision.args,
                            status="rejected",
                            observation=(
                                "could not start this step (cap or "
                                "missing handler)"
                            ),
                        )
                    )
                    consecutive_failures += 1
                    last_failed_skill = decision.skill
                    if self._should_break(consecutive_failures):
                        self._emit_failure_break(
                            emit, steps, last_failed_skill, task_id,
                            consecutive_failures,
                        )
                        return
                    continue
                children_spawned += 1
                status, row = self._wait_child(task_id, child_id)
                observation = _summarize_child(row, status)
                steps.append(
                    PlannerStep(
                        skill=decision.skill,
                        args=decision.args,
                        status=status,
                        observation=observation,
                    )
                )
                if self._is_cancelled(task_id):
                    log.info("workflow cancelled: task=%d (post-act)", task_id)
                    return
                # Consecutive-failure circuit breaker. A clean ``done``
                # resets the streak; anything else (failed / timeout /
                # cancelled-child / interrupted) advances it.
                if status == "done":
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    last_failed_skill = decision.skill
                    if self._should_break(consecutive_failures):
                        self._emit_failure_break(
                            emit, steps, last_failed_skill, task_id,
                            consecutive_failures,
                        )
                        return

                # No-progress / loop detector. Catches the case the two
                # guards above miss: the planner keeps issuing *different*
                # calls that all land on the same result (args differ, so
                # the repeat guard is silent; steps are ``done``, so the
                # failure breaker is silent). Coarse args-agnostic
                # signature over a rolling window.
                obs_window.append(_observation_signature(status, observation))
                looped = self._detect_loop(obs_window)
                if looped is not None:
                    signature, count = looped
                    log.info(
                        "workflow loop detected: task=%d count=%d window=%d "
                        "signature=%r",
                        task_id,
                        count,
                        self._loop_window,
                        signature[:120],
                    )
                    try:
                        emit(
                            TaskEventEmit(
                                EVENT_WORKFLOW_LOOP_DETECTED,
                                {
                                    "signature": signature[:200],
                                    "count": count,
                                    "window": self._loop_window,
                                    "threshold": self._loop_repeat_threshold,
                                },
                            )
                        )
                    except Exception:
                        log.debug("loop-detected audit emit failed", exc_info=True)
                    self._emit_blocked(
                        emit,
                        steps,
                        task_id,
                        reason="no_progress_loop",
                        findings=(
                            "I got stuck in a loop — I kept getting the same "
                            "result without making progress, so I stopped. "
                            "Could you help me narrow this down or tell me "
                            "what to try differently?"
                        ),
                        detail={"count": count, "window": self._loop_window},
                    )
                    return

            # Fell out of the loop without an explicit finish — cap hit.
            log.info(
                "workflow iteration cap reached: task=%d iters=%d",
                task_id,
                max_iter,
            )
            self._emit_finish(
                emit,
                steps,
                OUTCOME_PARTIAL,
                "Reached the step limit before fully finishing.",
            )
        except Exception:
            log.exception("workflow loop crashed: task=%d", task_id)
            try:
                emit(TaskFailed(error="the workflow hit an unexpected error"))
            except Exception:
                log.debug("workflow terminal emit failed", exc_info=True)
        finally:
            with self._lock:
                self._threads.pop(task_id, None)

    # ── helpers ──────────────────────────────────────────────────────

    def _wait_child(self, task_id: int, child_id: int) -> tuple[str, Any]:
        """Block on a child's terminal status; cancel it on a real timeout.

        Returns ``(status, row)``. The wait loops across timeout windows
        as long as the child is parked in ``awaiting_input`` — that's a
        child legitimately blocked on the user (e.g. a destructive-write
        approval the user answers in the TaskStrip), NOT a stalled child,
        so cancelling it would defeat the whole interactive-approval
        point. The child runs on the orchestrator pool; while it waits it
        holds no worker thread, so this is a cheap park. A genuine
        timeout (child still ``running``, no progress) cancels the child
        so it doesn't run orphaned after the parent moved on; a parent
        cancellation while waiting on an ``awaiting_input`` child cancels
        the child and reports ``cancelled``.
        """
        while True:
            try:
                status = self._orchestrator.wait_for_task(
                    child_id, timeout=self._child_wait_timeout
                )
            except Exception:
                log.exception(
                    "workflow wait_for_task failed: task=%d child=%d",
                    task_id,
                    child_id,
                )
                return "failed", self._safe_get_row(child_id)
            if status != "timeout":
                return status, self._safe_get_row(child_id)
            # ``timeout`` — distinguish "stalled" from "waiting on the
            # user". An ``awaiting_input`` child is the latter.
            row = self._safe_get_row(child_id)
            child_status = getattr(row, "status", "") if row is not None else ""
            if child_status == STATUS_AWAITING_INPUT:
                if self._is_cancelled(task_id):
                    log.info(
                        "workflow cancelled while child awaiting input: "
                        "task=%d child=%d",
                        task_id,
                        child_id,
                    )
                    try:
                        self._orchestrator.cancel(child_id)
                    except Exception:
                        log.debug(
                            "workflow child cancel failed", exc_info=True
                        )
                    return "cancelled", self._safe_get_row(child_id)
                log.info(
                    "workflow child awaiting input: task=%d child=%d "
                    "(waiting through for the user's answer)",
                    task_id,
                    child_id,
                )
                continue
            log.info(
                "workflow child timed out: task=%d child=%d timeout=%.0fs",
                task_id,
                child_id,
                self._child_wait_timeout,
            )
            try:
                self._orchestrator.cancel(child_id)
            except Exception:
                log.debug("workflow child cancel failed", exc_info=True)
            return "timeout", self._safe_get_row(child_id)

    def _safe_get_row(self, child_id: int) -> Any:
        """Fetch a child row, swallowing errors into ``None``."""
        try:
            return self._orchestrator.get(child_id)
        except Exception:
            return None

    def _is_cancelled(self, task_id: int) -> bool:
        """True when this workflow's row reached a terminal status."""
        try:
            row = self._orchestrator.get(task_id)
        except Exception:
            return False
        return row is not None and row.status in TERMINAL_STATUSES

    def _should_break(self, consecutive_failures: int) -> bool:
        return consecutive_failures >= self._max_consecutive_failures

    def _detect_loop(self, window: "deque[str]") -> "tuple[str, int] | None":
        """Return ``(signature, count)`` when the window shows no progress.

        Fires when a single result signature recurs at least
        ``loop_repeat_threshold`` times within the rolling window. Returns
        ``None`` when disabled, too few samples, or under threshold.
        """
        if not self._loop_detection_enabled:
            return None
        if len(window) < self._loop_repeat_threshold:
            return None
        signature, count = Counter(window).most_common(1)[0]
        if count >= self._loop_repeat_threshold:
            return signature, count
        return None

    def _is_browser_skill(self, skill: str) -> bool:
        return "browser" in (skill or "").lower()

    def _emit_failure_break(
        self,
        emit: TaskEmitFn,
        steps: list[PlannerStep],
        last_failed_skill: str,
        task_id: int,
        consecutive_failures: int,
    ) -> None:
        """Finish (partial) after the consecutive-failure breaker tripped."""
        if self._is_browser_skill(last_failed_skill):
            findings = (
                "I couldn't reach your browser — make sure Chrome is open "
                "and the Real Browser extension shows a green dot, then ask "
                "me again."
            )
        else:
            findings = (
                "I stopped because a tool kept failing — the service it "
                "needs may be unavailable right now."
            )
        log.info(
            "workflow failure breaker: task=%d failures=%d last_skill=%s",
            task_id,
            consecutive_failures,
            last_failed_skill,
        )
        self._emit_blocked(
            emit,
            steps,
            task_id,
            reason="consecutive_failures",
            findings=findings,
            detail={
                "failures": consecutive_failures,
                "last_skill": last_failed_skill,
            },
        )

    def _emit_blocked(
        self,
        emit: TaskEmitFn,
        steps: list[PlannerStep],
        task_id: int,
        *,
        reason: str,
        findings: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Emit a BLOCKED (stuck / needs-help) finish + audit event.

        Distinct from the budget-exhaustion finishes (iteration / child /
        wall-clock caps stay ``partial``): a blocked outcome means the
        workflow could not make progress on its own and the findings ask
        the user for a hand.
        """
        log.info(
            "workflow blocked: task=%d reason=%s", task_id, reason
        )
        try:
            emit(
                TaskEventEmit(
                    EVENT_WORKFLOW_BLOCKED,
                    {"reason": reason, **(detail or {})},
                )
            )
        except Exception:
            log.debug("blocked audit emit failed", exc_info=True)
        self._emit_finish(emit, steps, OUTCOME_BLOCKED, findings)

    def _emit_finish(
        self,
        emit: TaskEmitFn,
        steps: list[PlannerStep],
        outcome: str,
        findings: str,
        *,
        missing_capability: str = "",
    ) -> None:
        """Emit the aggregated TaskCompleted result."""
        narration = self._compose_narration(findings, steps, missing_capability)
        result: dict[str, Any] = {
            "outcome": outcome,
            "summary": (findings or narration)[:280],
            "content": narration,
            "steps": [
                {
                    "skill": s.skill,
                    "status": s.status,
                    "observation": s.observation,
                }
                for s in steps
            ],
        }
        if missing_capability:
            result["missing_capability"] = missing_capability
        emit(TaskCompleted(result=result))

    def _compose_narration(
        self, findings: str, steps: list[PlannerStep], missing_capability: str
    ) -> str:
        """Build the full reply-on-complete narration from the blackboard."""
        parts: list[str] = []
        if findings.strip():
            parts.append(findings.strip())
        if missing_capability:
            parts.append(
                "I don't know how to do that part yet — I'd need to be able "
                f"to {missing_capability}."
            )
        if steps:
            step_lines = []
            for s in steps:
                step_lines.append(
                    f"- {s.skill} [{s.status}]: {s.observation}"
                )
            parts.append("What I did:\n" + "\n".join(step_lines))
        return "\n\n".join(p for p in parts if p).strip() or (
            "I worked through it but didn't find anything to report."
        )

    def _record_gap(self, task_id: int, goal: str, capability: str) -> None:
        """Log + forward a capability gap so it's queryable later."""
        log.info(
            "workflow capability gap: task=%d capability=%r goal=%r",
            task_id,
            capability[:120],
            goal[:80],
        )
        if self._on_capability_gap is not None:
            try:
                self._on_capability_gap(
                    {
                        "task_id": task_id,
                        "capability": capability,
                        "goal": goal,
                        "at": time.time(),
                    }
                )
            except Exception:
                log.debug("capability gap sink raised", exc_info=True)

    def _user_name(self) -> str:
        if self._user_name_provider is None:
            return "the user"
        try:
            return self._user_name_provider() or "the user"
        except Exception:
            return "the user"

    def _model(self) -> str | None:
        if self._model_provider is None:
            return None
        try:
            return self._model_provider()
        except Exception:
            return None

    def _planner_skills(self, goal: str) -> list[dict[str, Any]]:
        """The skill catalogue for the planner, narrowed by the worker-lane
        router when enabled (full menu otherwise, or on ambiguity)."""
        if self._skill_router_enabled_provider is None:
            return self._skills.describe_for_planner()
        try:
            enabled = bool(self._skill_router_enabled_provider())
        except Exception:
            enabled = False
        if not enabled:
            return self._skills.describe_for_planner()
        try:
            groups = select_skill_groups(goal, self._skills.groups())
        except Exception:
            groups = None
        skills = self._skills.describe_for_planner(groups=groups)
        if groups is not None:
            log.info(
                "planner: narrowed groups=%s skills=%s",
                sorted(groups),
                [s.get("name") for s in skills],
            )
        return skills

    def _planner_guidance(self, skills: list[dict[str, Any]]) -> str:
        """Operational guidance for the skills actually in the menu.

        Reads the skills the planner will see this turn (so router
        narrowing is respected) and returns the plugin / captured guidance
        for each ``mcp:<server_id>`` group present. Empty string when no
        group in the menu carries guidance."""
        try:
            group_guidance: dict[str, str] = {}
            if self._group_guidance_provider is not None:
                try:
                    group_guidance = dict(self._group_guidance_provider() or {})
                except Exception:
                    group_guidance = {}
            return guidance_for_skills(
                skills,
                group_guidance=group_guidance,
            )
        except Exception:
            log.debug("planner guidance render failed", exc_info=True)
            return ""


__all__ = [
    "GoalWorkflowHandler",
    "OUTCOME_MISSING_CAPABILITY",
    "OUTCOME_BLOCKED",
    "EVENT_WORKFLOW_LOOP_DETECTED",
    "EVENT_WORKFLOW_BLOCKED",
]

"""Checkpointed plan/act/observe slices, reusing the existing task runtime."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import contextvars
from dataclasses import asdict
import hashlib
import json
import logging
import threading
import time

from app.llm.call_scope import CallScope, call_scope, LlmCallCancelled, LlmCallDeferred
from app.core.tasks.workflow.context_budget import TaskContextOverflow
from app.core.infra import timephrase
from app.core.tasks.task_handler import TaskCompleted, TaskFailed, TaskInputNeeded, TaskProgress
from app.core.tasks.workflow.harness_results import inspect_result, result_text, verify_completion
from app.core.tasks.workflow.profiles import PROFILES, catalogue, supports
from app.core.tasks.workflow.skill_registry import SpawnContext
from app.core.tasks.workflow.workflow_planner import PlannerInput, PlannerStep, decide_next_action

log = logging.getLogger("app.tasks.harness")


def _signature(skill) -> str:
    from app.core.tasks.workflow.operation_contract import input_schema

    parts = [input_schema(skill), skill.effect]
    if skill.generation:
        parts.append(skill.generation)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _short_args(args: dict) -> dict:
    return {
        str(key): value[:1000] if isinstance(value, str) else value for key, value in args.items()
    }


class WorkflowHarnessMixin:
    manages_state = True

    def discuss_work(self, task_id: int, message_id: int) -> None:
        from app.core.tasks.workflow.work_discussion import WorkDiscussion

        with self._lock:
            if getattr(self, "_discussions", None) is None:
                self._discussions = WorkDiscussion(self)
        if not self._discussions.submit(task_id, message_id):
            self._message(
                task_id,
                "The work discussion queue is busy. Your message is saved.",
                kind="discussion",
            )

    def shutdown(self) -> None:
        self._harness_stopped = True
        for task_id in list(self._threads):
            row = self._orchestrator.get(task_id)
            if row and row.status == "running" and row.state.get("harness_version") == 1:
                self._orchestrator.suspend_work(task_id, phase="recovering")
        if getattr(self, "_discussions", None) is not None:
            self._discussions.shutdown()

    def _start_harness(self, args: dict, emit, *, state: dict | None = None) -> dict:
        from app.core.tasks.workflow.goal_workflow_handler import _task_id_from_context

        task_id = _task_id_from_context()
        if task_id is None:
            emit(TaskFailed(error="Work task identity is unavailable."))
            return {}
        row = self._orchestrator.get(task_id)
        if state is not None and state.get("current_observation"):
            previous = state["current_observation"]
            state["current_observation"] = {
                "status": "stale",
                "server_id": previous.get("server_id"),
                "instruction": "Work resumed. Obtain a new observation before using old targets.",
            }
        if state is None:
            goal = str(args.get("goal") or "").strip()
            if not goal:
                emit(TaskFailed(error="Work needs an objective."))
                return {}
            contract = args.get("contract") or {}
            state = {
                "harness_version": 1,
                "epoch": 1,
                "args": args,
                "goal": goal,
                "contract": {
                    "objective": goal,
                    "criteria": contract.get("criteria") or [goal],
                    "constraints": contract.get("constraints") or [],
                    "source_message_id": (row.metadata or {}).get("source_message_id"),
                    "requested_by": row.user_id,
                    "created_at": row.created_at,
                    "conversation_request": (row.metadata or {}).get("origin_prompt"),
                    "conversation_session": (row.metadata or {}).get("source_session"),
                },
                "profile": args.get("profile") if args.get("profile") in PROFILES else "general",
                "plan": [],
                "notes": "",
                "steps": [],
                "pending": None,
                "seen": [],
                "iteration": 0,
                "children": 0,
                "active_seconds": 0.0,
                "tokens": 0,
                "message_cursor": 0,
                "guidance": [],
                "inspections": [],
                "failures": 0,
                "review_attempts": 0,
                "expires_at": time.time() + 7 * 86400,
                "limits": {
                    "iterations": self._max_iterations,
                    "children": self._max_children,
                    "active_seconds": self._max_wall_seconds or 1800,
                    "tokens": 40000,
                },
            }
            if not self._orchestrator.checkpoint(task_id, state):
                return state
            self._message(task_id, "I’m organizing this work and choosing the capabilities I need.")

        # Every generation's emit is fenced under the same admission lock as
        # pause/resume/cancel. A returning old model call cannot finish a newer
        # generation or ask a stale question after the user paused the job.
        def emit(outcome):
            if not getattr(self, "_harness_stopped", False):
                self._orchestrator.emit_work_outcome(task_id, state["epoch"], outcome)

        ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: ctx.run(self._harness_slice, task_id, deepcopy(state), emit),
            name=f"work-{task_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[task_id] = thread
        thread.start()
        return state

    def ready_to_resume(self, state: dict) -> bool:
        if time.time() > state.get("expires_at", 0):
            return True
        if state.get("wake_at", 0) > time.time():
            return False
        pending = state.get("pending")
        if not pending:
            return True
        if pending.get("child_id"):
            child = self._orchestrator.get(pending["child_id"])
            return child is None or child.status not in {"running", "awaiting_input", "paused"}
        return self._skills.get(pending["skill"]) is not None

    def _message(self, task_id: int, text: str, *, kind="update", profile="general") -> None:
        from app.core.services.response_text_service import strip_all_meta_tags

        self._orchestrator.append_work_message(
            task_id,
            role="assistant",
            content=strip_all_meta_tags(text),
            kind=kind,
            profile=profile,
        )

    def _running(self, task_id: int, state: dict) -> bool:
        if getattr(self, "_harness_stopped", False):
            return False
        row = self._orchestrator.get(task_id)
        return (
            not getattr(self, "_harness_stopped", False)
            and row is not None
            and row.status == "running"
            and row.state.get("epoch") == state["epoch"]
        )

    def _save(self, task_id: int, state: dict) -> bool:
        return self._orchestrator.checkpoint(task_id, state)

    def _guidance(self, task_id: int, state: dict) -> bool:
        changed = False
        for event in self._orchestrator.work_messages(task_id, after=state["message_cursor"]):
            state["message_cursor"] = event.id
            data = event.data or {}
            if data.get("role") == "user" and data.get("kind") == "guidance":
                changed = True
                state["guidance"].append(
                    {
                        "role": "user",
                        "content": data.get("content", ""),
                        "created_at": event.created_at,
                    }
                )
        return changed

    def _inference_identity(self):
        revision = getattr(self._worker_client_provider(), "routing_revision", 0)
        return [
            self._model(),
            self._context_window_provider(),
            revision if type(revision) is int else 0,
        ]

    def _context_options(self, state):
        identity = self._inference_identity()
        if state.get("context_identity") != identity:
            state["context_identity"], state["context_scale"] = identity, 1.0
        return {"context_window": identity[1], "estimate_scale": state.get("context_scale", 1.0)}

    def _current_context(self, state):
        observation = state.get("current_observation")
        if observation and observation.get("status") == "current":
            generation = observation.get("generation", "")
            owner = observation.get("server_id")
            if not any(
                row.get("group") == f"mcp:{owner}" and row.get("generation", "") == generation
                for row in self._skills.describe_for_planner()
            ):
                observation = {
                    "status": "stale",
                    "server_id": owner,
                    "instruction": "Capability changed. Obtain a new observation before acting.",
                }
                state["current_observation"] = observation
        return observation

    @contextmanager
    def _task_inference(self, task_id, state, emit, phase):
        def waiting():
            state["active_reserved"] = 0
            if not self._save(task_id, state):
                raise LlmCallCancelled()
            emit(
                TaskProgress(
                    message="Waiting for the shared model; cognition has priority.",
                    phase="waiting_model",
                )
            )

        def acquired():
            if not self._running(task_id, state):
                raise LlmCallCancelled()
            identity = self._inference_identity()
            if state.get("context_identity") != identity:
                raise LlmCallDeferred()
            if self._guidance(task_id, state):
                self._save(task_id, state)
                raise LlmCallDeferred()
            state["active_reserved"] = min(
                30, max(0, state["limits"]["active_seconds"] - state["active_seconds"])
            )
            if not self._save(task_id, state):
                raise LlmCallCancelled()
            emit(
                TaskProgress(
                    message="Reviewing evidence" if phase == "reviewing" else "Planning next step",
                    phase=phase,
                )
            )

        scope = CallScope(
            cancelled=lambda: not self._running(task_id, state),
            on_wait=waiting,
            on_acquired=acquired,
        )
        try:
            with call_scope(scope):
                yield scope
        finally:
            state["active_seconds"] += scope.active_seconds
            state["tokens"] += scope.reported_tokens
            state["queue_seconds"] = state.get("queue_seconds", 0.0) + scope.queue_seconds
            state["active_reserved"] = 0
            if self._running(task_id, state):
                self._save(task_id, state)

    def _record_context_budget(self, state, emit, stage, report):
        from app.core.tasks.task_handler import TaskEventEmit

        state["context_budget"] = {"stage": stage, **report}
        state["context_scale"] = report.get("next_estimate_scale", state.get("context_scale", 1.0))
        emit(TaskEventEmit(type="work_context_budget", data=state["context_budget"]))

    def _review_work(self, task_id, state, emit):
        request = state["review_request"]
        remaining = state["limits"]["active_seconds"] - state["active_seconds"]
        if remaining < 1:
            self._orchestrator.suspend_work(task_id, phase="budget_exhausted")
            return False
        with self._task_inference(task_id, state, emit, "reviewing"):
            check, usage = verify_completion(
                self._worker_client_provider(),
                self._model(),
                state,
                self._orchestrator,
                task_id,
                request["findings"],
                request["evidence_ids"],
                min(30, remaining),
                **self._context_options(state),
            )
        state["verification"] = check
        if check.get("budget"):
            self._record_context_budget(state, emit, "review", check["budget"])
        if not self._running(task_id, state):
            return False
        if self._guidance(task_id, state):
            state.pop("review_request", None)
            state["verification"] = {
                "status": "outdated",
                "reason": "User guidance changed during review.",
            }
            return self._save(task_id, state)
        state.pop("review_request", None)
        if check["status"] != "supported":
            state["review_attempts"] += 1
            state["notes"] = "Completion review needs: " + check["reason"]
            self._message(task_id, state["notes"], kind="review", profile="reviewer")
            if state["review_attempts"] < 3:
                self._save(task_id, state)
                return True
            self._finish_work(task_id, state, emit, "partial", state["notes"])
            return False
        self._finish_work(task_id, state, emit, request["outcome"], request["findings"])
        return False

    def _harness_slice(self, task_id: int, state: dict, emit) -> None:
        try:
            while self._running(task_id, state):
                if time.time() > state["expires_at"]:
                    self._finish_work(
                        task_id, state, emit, "partial", "This work expired while waiting."
                    )
                    return
                if state.get("review_request"):
                    if not self._review_work(task_id, state, emit):
                        return
                    continue
                if state.get("pending"):
                    if not self._continue_pending(task_id, state, emit):
                        return
                    continue
                if any(
                    state[key] >= state["limits"][limit]
                    for key, limit in (
                        ("iteration", "iterations"),
                        ("children", "children"),
                        ("active_seconds", "active_seconds"),
                        ("tokens", "tokens"),
                    )
                ):
                    self._save(task_id, state)
                    self._message(
                        task_id,
                        "I paused at the work budget. You can continue "
                        "with another bounded allowance.",
                    )
                    self._orchestrator.suspend_work(task_id, phase="budget_exhausted")
                    return
                self._guidance(task_id, state)
                state["iteration"] += 1
                state["active_reserved"] = 0
                if not self._save(task_id, state):
                    return
                skills = catalogue(
                    self._skills,
                    state["profile"],
                    query=state.get("query") or state["goal"],
                    page=state.get("catalogue_page", 0),
                    page_size=6,
                )
                emit(TaskProgress(message=f"Planning with {state['profile']}", phase="planning"))
                with self._task_inference(task_id, state, emit, "planning"):
                    decision = decide_next_action(
                        self._worker_client_provider(),
                        PlannerInput(
                            goal=state["goal"],
                            skills=skills,
                            steps=[
                                PlannerStep(
                                    **{
                                        key: step[key]
                                        for key in (
                                            "skill",
                                            "args",
                                            "status",
                                            "observation",
                                            "child_id",
                                            "profile",
                                        )
                                    }
                                )
                                for step in state["steps"]
                            ],
                            iteration=state["iteration"] - 1,
                            max_iterations=state["limits"]["iterations"],
                            history_budget_chars=max(8000, self._history_budget),
                            user_name=self._user_name(),
                            guidance=self._planner_guidance(skills),
                            work={
                                "contract": state["contract"],
                                "profile": state["profile"],
                                "failures": state["failures"] + state.get("admission_failures", 0),
                                "current_observation": self._current_context(state),
                                "profile_instructions": PROFILES[state["profile"]],
                                "profiles": PROFILES,
                                "capability_groups": sorted(self._skills.groups())[:40],
                                "plan": state["plan"],
                                "notes": state["notes"],
                                "inspected_results": state["inspections"][-3:],
                                "user_guidance": timephrase.format_transcript(state["guidance"]),
                                "remaining_steps": state["limits"]["iterations"]
                                - state["iteration"],
                            },
                        ),
                        valid_skill_names={row["name"] for row in skills},
                        model=self._model(),
                        max_tokens=self._planner_max_tokens,
                        **self._context_options(state),
                        timeout_seconds=min(
                            30, max(1, state["limits"]["active_seconds"] - state["active_seconds"])
                        ),
                    )
                self._record_context_budget(state, emit, "planner", decision.budget)
                if not self._running(task_id, state):
                    return
                if self._guidance(task_id, state):
                    self._save(task_id, state)
                    continue  # Fresh user guidance supersedes an action planned without it.
                if decision.plan:
                    state["plan"] = decision.plan
                if decision.notes:
                    state["notes"] = decision.notes
                if decision.profile in PROFILES:
                    state["profile"] = decision.profile
                if decision.is_finish:
                    if decision.outcome in {"success", "nothing_found"}:
                        state["review_request"] = {
                            "findings": decision.findings,
                            "evidence_ids": decision.evidence_ids,
                            "outcome": decision.outcome,
                        }
                        if not self._save(task_id, state):
                            return
                        continue
                    self._finish_work(
                        task_id,
                        state,
                        emit,
                        decision.outcome or "partial",
                        decision.findings or decision.reason,
                    )
                    return
                if decision.is_missing_capability:
                    self._record_gap(task_id, state["goal"], decision.missing_capability)
                    self._finish_work(
                        task_id,
                        state,
                        emit,
                        "missing_capability",
                        "I need this capability: " + decision.missing_capability,
                    )
                    return
                if decision.action != "skill":
                    if not self._native_action(task_id, state, decision, emit):
                        return
                    continue
                errors = self._skills.validate_call(
                    decision.skill, decision.args, {row["name"] for row in skills}
                )
                skill = self._skills.get(decision.skill)
                disclosed = next((row for row in skills if row["name"] == decision.skill), {})
                if skill and skill.generation != disclosed.get("generation", ""):
                    self._finish_work(
                        task_id,
                        state,
                        emit,
                        "blocked",
                        "Plugin settings changed while planning; please start a fresh plan.",
                    )
                    return
                if skill and not supports(skill, state["profile"]):
                    errors.append("Switch to a profile that supports this operation.")
                if errors:
                    state["admission_failures"] = state.get("admission_failures", 0) + 1
                    self._record_observation(state, decision.skill, "rejected", "; ".join(errors))
                    if state["admission_failures"] >= 2:
                        self._finish_work(
                            task_id,
                            state,
                            emit,
                            "blocked",
                            "I could not form a valid operation after a correction. "
                            "The original request and results are saved for a fresh plan.",
                        )
                        return
                    self._save(task_id, state)
                    continue
                state["admission_failures"] = 0
                canonical = hashlib.sha256(
                    json.dumps(
                        [decision.skill, decision.args]
                        + ([skill.generation] if skill.generation else []),
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if canonical in state["seen"]:
                    self._finish_work(
                        task_id,
                        state,
                        emit,
                        "blocked",
                        "I stopped a repeated operation without new progress.",
                    )
                    return
                state["seen"].append(canonical)
                state["pending"] = {
                    "step_id": f"{task_id}:{state['iteration']}:0",
                    "skill": decision.skill,
                    "args": decision.args,
                    "child_id": None,
                    "effect": skill.effect,
                    "schema_revision": _signature(skill),
                    "attempt": 0,
                    "reserved": False,
                    "profile": state["profile"],
                }
                if not self._save(task_id, state):
                    return
                self._message(
                    task_id, decision.reason or f"Using {decision.skill}.", profile=state["profile"]
                )
        except LlmCallCancelled:
            return
        except LlmCallDeferred:
            if self._running(task_id, state):
                if not state.get("review_request"):
                    state["iteration"] = max(0, state["iteration"] - 1)
                state["wake_at"] = time.time() + 2
                if self._save(task_id, state):
                    self._orchestrator.suspend_work(task_id, phase="waiting_model")
        except TaskContextOverflow:
            if self._running(task_id, state):
                self._save(task_id, state)
                emit(
                    TaskInputNeeded(
                        prompt="The original request and constraints cannot fit "
                        "safely in this model's context. Please narrow this step "
                        "or select a larger context."
                    )
                )
        except Exception:
            log.exception("work slice failed: task=%s", task_id)
            if not self._running(task_id, state):
                return
            emit(
                TaskFailed(
                    error="The work encountered an unexpected error; "
                    "its saved progress is retained."
                )
            )
        finally:
            with self._lock:
                if self._threads.get(task_id) is threading.current_thread():
                    self._threads.pop(task_id, None)

    def _continue_pending(self, task_id: int, state: dict, emit) -> bool:
        pending = state["pending"]
        if pending.get("child_id"):
            child = self._orchestrator.get(pending["child_id"])
            if child and child.status in {"running", "awaiting_input", "paused"}:
                self._orchestrator.suspend_work(task_id, phase="waiting_child")
                return False
            status = child.status if child else "interrupted"
            if status == "interrupted" and pending["effect"] == "read" and pending["attempt"] < 2:
                pending["attempt"] += 1
                pending["step_id"] += f":retry{pending['attempt']}"
                pending["child_id"], pending["reserved"] = None, False
                return self._save(task_id, state)
            if (status == "interrupted" and pending["effect"] != "read") or (
                child and child.state.get("uncertain_effect")
            ):
                state["verification"] = {
                    "status": "uncertain",
                    "reason": "An external operation was interrupted.",
                }
                self._finish_work(
                    task_id,
                    state,
                    emit,
                    "uncertain",
                    "An external operation was interrupted. Its effect may have happened; "
                    "I have not repeated it.",
                )
                return False
            text = (
                result_text(child)
                if child and child.result
                else (child.error if child else "Result unavailable")
            )
            server_id = (child.args or {}).get("server_id") if child else None
            current = state.get("current_observation") or {}
            if server_id and current.get("server_id") == server_id:
                state["current_observation"] = {
                    "status": "stale",
                    "server_id": server_id,
                    "after_task_id": pending["child_id"],
                    "instruction": "Resource changed. Obtain a new observation before acting.",
                }
            projection = (child.result or {}).get("planner_context") if child else None
            if status == "done" and isinstance(projection, dict):
                state["current_observation"] = {
                    **projection,
                    "status": "current",
                    "server_id": server_id,
                    "source_task_id": child.id,
                    "recorded_at": child.completed_at,
                    "generation": (child.args or {}).get("_config_generation", ""),
                }
                text = str((child.result or {}).get("summary") or "Observation saved")
            self._record_observation(
                state,
                pending["skill"],
                status,
                (text or "")[:1200],
                child_id=pending["child_id"],
                args=_short_args(pending["args"]),
                profile=pending["profile"],
            )
            live_skill = self._skills.get(pending["skill"])
            if live_skill is None or _signature(live_skill) != pending["schema_revision"]:
                self._finish_work(
                    task_id,
                    state,
                    emit,
                    "blocked",
                    "Plugin settings changed during this step; please start a fresh plan.",
                )
                return False
            state["pending"] = None
            state["failures"] = 0 if status == "done" else state["failures"] + 1
            if not self._save(task_id, state):
                return False
            self._message(
                task_id,
                f"{pending['skill']}: {status}. Result #{pending['child_id']} is saved.",
                kind="step",
                profile=pending["profile"],
            )
            if state["failures"] >= self._max_consecutive_failures:
                self._finish_work(
                    task_id,
                    state,
                    emit,
                    "blocked",
                    "Repeated operations failed. I preserved their results for review.",
                )
                return False
            from collections import deque
            from app.core.tasks.workflow.goal_workflow_handler import _observation_signature

            window = [
                *state.get("recent_observations", []),
                # A shared file header is not evidence of no progress. Use the
                # saved body, rather than its short planner preview, for reads.
                (status + ":" + hashlib.sha256((text or "").encode()).hexdigest())
                if status == "done"
                else _observation_signature(status, text or ""),
            ][-self._loop_window :]
            state["recent_observations"] = window
            if self._detect_loop(deque(window)):
                self._finish_work(
                    task_id,
                    state,
                    emit,
                    "blocked",
                    "Repeated operations returned the same result without progress.",
                )
                return False
            if not self._save(task_id, state):
                return False
            return True
        skill = self._skills.get(pending["skill"])
        if skill is None:
            self._orchestrator.suspend_work(task_id, phase="waiting_capability")
            return False
        if _signature(skill) != pending["schema_revision"]:
            self._finish_work(
                task_id,
                state,
                emit,
                "blocked",
                "The operation contract changed; this step needs a fresh plan.",
            )
            return False
        if not pending["reserved"]:
            if state["children"] >= state["limits"]["children"]:
                self._orchestrator.suspend_work(task_id, phase="budget_exhausted")
                return False
            pending["reserved"] = True
            state["children"] += 1
        if not self._save(task_id, state) or not self._running(task_id, state):
            return False
        child_id = self._skills.spawn_child(
            pending["skill"],
            pending["args"],
            SpawnContext(
                self._orchestrator,
                self._orchestrator.get(task_id).user_id,
                task_id,
                pending["step_id"],
                state["epoch"],
            ),
            expected_skill=skill,
        )
        if child_id is None:
            current = self._skills.get(pending["skill"])
            if current is None or _signature(current) != pending["schema_revision"]:
                self._finish_work(
                    task_id,
                    state,
                    emit,
                    "blocked",
                    "Plugin settings changed before dispatch; start a fresh plan.",
                )
                return False
            self._record_observation(
                state, pending["skill"], "rejected", "Operation admission rejected this step."
            )
            state["pending"] = None
            return self._save(task_id, state)
        pending["child_id"] = child_id
        # The child link is already durable from TaskStore.create's transaction.
        return self._save(task_id, state)

    def _record_observation(
        self, state, skill, status, text, *, child_id=None, args=None, profile=None
    ):
        state["steps"].append(
            asdict(
                PlannerStep(
                    skill=skill,
                    args=args or {},
                    status=status,
                    observation=text[:5000],
                    child_id=child_id,
                    profile=profile or state["profile"],
                )
            )
        )

    def _native_action(self, task_id, state, decision, emit) -> bool:
        args = decision.args
        if not isinstance(args, dict):
            self._record_observation(
                state, decision.action, "rejected", "Arguments must be an object."
            )
            return self._save(task_id, state)
        action = decision.action
        if action == "use_profile":
            profile = args.get("profile")
            if profile in PROFILES:
                state["profile"] = profile
                state["catalogue_page"] = 0
                self._message(
                    task_id, f"I’m switching to {profile} for this part.", profile=profile
                )
        elif action == "discover":
            state["query"] = str(args.get("query") or "")[:200]
            page = args.get("page", 0)
            state["catalogue_page"] = max(0, min(100, page)) if type(page) is int else 0
        elif action == "inspect_result":
            result = inspect_result(self._orchestrator, task_id, args)
            state["inspections"] = [*state["inspections"], result][-4:]
            self._record_observation(state, "inspect_result", "done", json.dumps(result)[:5000])
        elif action == "reply":
            self._message(
                task_id,
                str(args.get("message") or "")[:4000],
                kind="reply",
                profile=state["profile"],
            )
        elif action == "ask_user":
            question = str(args.get("question") or "").strip()[:1500]
            if question:
                if self._save(task_id, state):
                    self._message(task_id, question, kind="question", profile=state["profile"])
                    emit(TaskInputNeeded(prompt=question))
                return False
        elif action == "wait":
            seconds = args.get("seconds", 1)
            if type(seconds) is int and 1 <= seconds <= 3600:
                state["wake_at"] = time.time() + seconds
                if self._save(task_id, state):
                    self._orchestrator.suspend_work(task_id, phase="scheduled")
                return False
        else:
            self._record_observation(state, action, "rejected", "Unknown harness action.")
        return self._save(task_id, state)

    def _finish_work(self, task_id, state, emit, outcome, findings) -> None:
        from app.core.services.response_text_service import strip_all_meta_tags

        findings = strip_all_meta_tags(findings)
        if not self._running(task_id, state):
            return
        state.setdefault(
            "verification",
            {"status": "unverified", "reason": "Work ended without a supported completion."},
        )
        if not self._save(task_id, state):
            return
        self._message(
            task_id, findings or "Work finished.", kind="result", profile=state["profile"]
        )
        emit(
            TaskCompleted(
                result={
                    "outcome": outcome,
                    "summary": findings[:280],
                    "content": findings,
                    "steps": state["steps"],
                    "verification": state["verification"],
                    "profile": state["profile"],
                    "plan": state["plan"],
                }
            )
        )

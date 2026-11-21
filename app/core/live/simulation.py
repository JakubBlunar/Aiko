"""Replay scenario inputs through Live mode with a virtual clock and simulated delivery."""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot, WorldSituation,
)
from app.core.infra import timephrase
from app.core.infra.agent_settings_parse import (
    clamp_live_main_wake_max_per_hour, clamp_live_min_gap_after_speech_ms,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import LLM_ROLE_LIVE_POLICY, LlmRoute
from app.core.live.diagnostics import sanitize_live_dump
from app.core.live.urge_menu import menu_urge_ids
from app.core.proactive.cue_accounting import CUE_POLICIES
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.surfacing_attempt import SurfaceAttempt, current_attempt
from app.llm.chat_client import ChatUsage


_START = datetime(2026, 1, 1, 16, tzinfo=timezone.utc)
_STATE_FLAGS = {
    "connected", "present", "typing", "quiet", "playback", "shared", "unprompted_speech",
}
_STATE_TEXT = {"app", "floor", "sleep", "activity"}
_STEP_KEYS = {
    "label", "state", "impulse", "payload", "advance_ms", "cue", "concept", "dispatch",
    "reply", "proposal", "expect",
}


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.start = start
        self.elapsed_ms = 0

    def now(self) -> datetime:
        return self.start + timedelta(milliseconds=self.elapsed_ms)

    def monotonic(self) -> float:
        # Some production timers use 0 as the unset sentinel.
        return 1.0 + self.elapsed_ms / 1000.0


class _PolicyClient:
    """Count policy calls and either replay a proposal or delegate to a local client."""

    def __init__(self, host: _SimulationHost, client: Any) -> None:
        self.host = host
        self.client = client
        self.calls = 0
        self.proposal: dict[str, Any] = {}

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        if self.client is not None:
            return self.client.chat_json(messages, **kwargs)
        frame = self.host.live_situation_frame()
        supplied = dict(self.proposal)
        subject = supplied.pop("select_subject", None)
        proposal = {
            "snapshot_generation": frame.generation,
            "selected_urge_id": None,
            "intent": "noop",
            "arguments": {},
            "reason_code": "scenario",
            "context_refs": [],
            **supplied,
        }
        if subject is not None:
            urges = self.host._live_inclination.urges.active()
            menu = menu_urge_ids(urges)
            proposal["selected_urge_id"] = next(
                (urge.urge_id for urge in urges
                 if urge.subject == subject and urge.urge_id in menu),
                None,
            )
        return json.dumps(proposal), ChatUsage()


class _SimulationHost(LiveModeMixin, CuePoolMixin):
    """Live dependencies that normally come from the web session and its workers."""

    def __init__(
        self, clock: _Clock, route: LlmRoute, client: Any, max_wakes: int, min_gap_ms: int,
    ) -> None:
        self.clock = clock
        self.route = route
        self.session_key = "scenario:live"
        self._chat_db = ChatDatabase(Path(":memory:"))
        self._cue_store = CueStore(self._chat_db)
        self._settings = SimpleNamespace(agent=SimpleNamespace(
            behavior_posture="live_presence",
            live_quiet=False, live_unprompted_speech=True,
            live_main_wake_max_per_hour=max_wakes, live_min_gap_after_speech_ms=min_gap_ms,
        ))
        self.state: dict[str, Any] = {
            "present": True, "typing": False, "app": "", "floor": "neither",
            "sleep": "awake", "shared": False, "activity": "idle",
        }
        self._connected_clients = 1
        self._last_user_activity_ms = 0
        self._unanswered_text = ""
        self._pending_policy: deque = deque()
        self._wake_queue: deque = deque()
        self._brain_loop = SimpleNamespace(enqueue=self._wake_queue.append)
        self._turn_runner = SimpleNamespace(run=self._run_fake_turn)
        self._live_policy_client = _PolicyClient(self, client)
        self._concepts: dict[int, SimpleNamespace] = {}
        self._added_cues: dict[int, str] = {}
        self.dispatches: list[dict[str, Any]] = []
        self.spoken: list[dict[str, Any]] = []
        self._simulated_text = ""
        self._init_live_mode(
            monotonic=clock.monotonic, dispatch_inference=self._pending_policy.append,
        )

    def _live_heartbeat_enabled(self) -> bool:
        return False  # Steps invoke the production heartbeat on the scenario thread.

    def _route_or_none(self, role: str) -> LlmRoute | None:
        return self.route if role == LLM_ROLE_LIVE_POLICY else None

    def _live_concept_view(self):
        return SimpleNamespace(for_consumer=lambda role: tuple(self._concepts.values()))

    def conversation_situation_snapshot(self, *, fresh: bool) -> ConversationSituationSnapshot:
        state = self.state
        return ConversationSituationSnapshot(
            session_id=self.session_key, generation=self._live_mode_generation,
            observed_at=self.clock.now().isoformat(), input_mode="typed",
            floor_transition="neither", dialogue_act="", arc="casual_check_in",
            arc_confidence=0.4, mood_label="content", vitality_band="normal",
            user_present=state["present"], user_active_app=state["app"],
            floor_owner=state["floor"], typing_active=state["typing"],
            world=WorldSituation(activity=state["activity"]),
            inferred=None, inferred_stale=True, world_compatible=False, conflict_reason="",
            shared_commitment_active=state["shared"], sleep={"status": state["sleep"]},
            since_user_activity_ms=self.clock.elapsed_ms - self._last_user_activity_ms,
        )

    def _notify_message(self, role: str, text: str, message_id: int | None) -> None:
        self.spoken.append({
            "at_ms": self.clock.elapsed_ms, "delivery": role, "text": text,
            "message_id": message_id,
        })

    def _run_fake_turn(self, session_key: str, user_text: str, **kwargs):
        attempt = SurfaceAttempt()
        token = current_attempt.set(attempt)
        try:
            handoff = self._render_live_talk_about_block()
            attempt.finish(handoff)
        finally:
            current_attempt.reset(token)
        self.dispatches[-1]["talk_about"] = handoff
        text = self._simulated_text
        message_id = self._chat_db.add_message(session_key, "assistant", text) if text else None
        return SimpleNamespace(text=text, raw_text=text, assistant_message_id=message_id)

    def _post_turn_inner_life(self, **kwargs) -> None:
        # Run just the cue feedback path. No unrelated workers or model calls.
        self._settle_pool_cues(
            user_text="", assistant_text=kwargs["assistant_text"],
            assistant_message_id=kwargs["assistant_message_id"],
        )

    def _apply_state(self, update: dict[str, Any]) -> None:
        for key, value in update.items():
            if key in _STATE_FLAGS:
                if type(value) is not bool:
                    raise ValueError(f"state.{key} must be a boolean")
            elif key not in _STATE_TEXT or not isinstance(value, str):
                raise ValueError(f"unknown or invalid state field: {key}")
        if "quiet" in update:
            self._settings.agent.live_quiet = update["quiet"]
        if "unprompted_speech" in update:
            self._settings.agent.live_unprompted_speech = update["unprompted_speech"]
        if "connected" in update:
            self._connected_clients = int(update["connected"])
        if "playback" in update:
            if update["playback"]:
                self.mark_playback_pending()
            else:
                self._playback_drained.set()
        self.state.update(update)

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        if kind in {"user.message_sent", "user.speech_final"}:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{kind} needs payload.text")
            self.state.update(typing=False, floor="neither")
            self._last_user_activity_ms = self.clock.elapsed_ms
            self._unanswered_text = text
            message_id = self._chat_db.add_message(self.session_key, "user", text)
            self._settle_awaiting_cues(user_text=text, user_message_id=message_id)
            self.publish_live_user_meaning(
                text, mode="voice" if kind == "user.speech_final" else "typed",
            )
            return
        if kind == "user.session_changed":
            self.bump_live_mode_generation("scenario")
            return
        if kind == "aiko.playback_drained":
            self.notify_playback_drained()
            return
        if kind in {"user.typing_started", "user.typing_stopped"}:
            self.state["typing"] = kind == "user.typing_started"
        if kind in {"user.voice_start", "user.voice_stop", "user.stop"}:
            self.state["floor"] = "user" if kind == "user.voice_start" else "neither"
        self.publish_live_impulse(
            kind=kind, source="scenario", coalesce_key=kind, payload=payload,
        )

    def add_cue(self, cue: dict[str, Any]) -> None:
        kind = cue.get("type")
        if kind not in CUE_POLICIES:
            raise ValueError(f"unknown cue type: {kind!r}")
        cue_id = self._cue_store.add(
            kind, cue["subject"], cue["text"], ttl_hours=cue.get("ttl_hours"),
            hold_hours=cue.get("hold_hours", 0),
            payload={"session_id": self.session_key, **cue.get("payload", {})},
        )
        if not cue_id:
            raise ValueError("could not add the scenario cue")
        self._added_cues[cue_id] = cue["subject"]

    def add_concept(self, concept: dict[str, Any]) -> None:
        concept_id = concept["id"]
        self._concepts[concept_id] = SimpleNamespace(
            concept_id=concept_id, kind=concept["kind"], label=concept["label"],
            subject=concept.get("subject", "relationship"),
            confidence=concept.get("confidence", 0.8), plasticity=0.3,
            last_reinforced_at=self.clock.now().isoformat(),
        )

    def dispatch(self, config: dict[str, Any]) -> None:
        if not self._wake_queue:
            raise ValueError("dispatch requires a queued main wake")
        text = config.get("text", "")
        if not isinstance(text, str):
            raise ValueError("dispatch.text must be a string")
        event = self._wake_queue.popleft()
        blocked = self._live_main_wake_dispatch_block(event)
        self.dispatches.append({
            "at_ms": self.clock.elapsed_ms, "subject": event.cue_subject,
            "speech_act": event.speech_act, "cue_id": event.cue_id,
            "reason": blocked or ("simulated_reply" if text.strip() else "simulated_silence"),
        })
        self._simulated_text = text.strip()
        self._run_live_main_wake(event)
        self.refresh_live_situation(trigger_kind="heartbeat")

    def reply(self, text: str) -> None:
        if not self._unanswered_text or not isinstance(text, str) or not text.strip():
            raise ValueError("reply requires a prior user message and nonempty scripted text")
        user_text, self._unanswered_text = self._unanswered_text, ""
        message_id = self._chat_db.add_message(self.session_key, "assistant", text.strip())
        self._live_mark_aiko_spoke()
        self._notify_message("Assistant (scripted reply)", text.strip(), message_id)
        self._settle_pool_cues(
            user_text=user_text, assistant_text=text.strip(), assistant_message_id=message_id,
        )
        self.refresh_live_situation(trigger_kind="heartbeat")

    def snapshot(self) -> dict[str, Any]:
        controller = self._live_policy_controller.diagnostics()
        frame = self.live_situation_frame()
        queued = [
            {"subject": event.cue_subject, "speech_act": event.speech_act,
             "cue_id": event.cue_id, "reason": event.reason_code,
             "situation": event.situation_summary, "concept_ids": event.concept_ids}
            for event in self._wake_queue
        ]
        inclination = self._live_inclination.diagnostics()
        impulses = self.live_impulse_diagnostics()
        return {
            "at_ms": self.clock.elapsed_ms, "now": self.clock.now().isoformat(),
            "policy_calls": self._live_policy_client.calls,
            "main_wakes": controller["main_wake_admitted"],
            "user_intent_reason": (
                controller["last_user_intent_admit"].get("proposal") or {}
            ).get("reason_code", ""),
            "policy_failures": controller["policy_failure_count"],
            "last_policy_failure": controller["last_policy_failure"],
            "speech_count": len(self.spoken), "queued_count": len(queued),
            "queued_wakes": queued,
            "last_dispatch": dict(self.dispatches[-1]) if self.dispatches else None,
            "speech_budget": frame.constraints.speech_budget if frame else None,
            "wait": inclination["wait"], "budget": inclination["budget"],
            "notices": inclination["notices"],
            "decisions": controller["decision_trace"]["decisions"],
            "last_action": controller["last_action"],
            "last_admission": controller["last_admission"],
            "last_policy": sanitize_live_dump(controller["last_policy_proposal"]),
            "frame": sanitize_live_dump(frame.to_payload()) if frame else None,
            "embodiment": self._live_embodiment_diagnostics(),
            "policy_context": self._live_policy_context.diagnostics(),
            "impulse_bus": {
                key: impulses[key] for key in
                ("accepted", "pending", "expired", "coalesced", "generation_dropped")
            },
            "urges": [asdict(urge) for urge in self._live_inclination.urges.all_urges()],
            "cues": {
                subject: {
                    "id": cue_id, "state": row.state, "surfaced": row.surfaced_count,
                    "available": self._cue_store.available(cue_id) is not None,
                }
                for cue_id, subject in self._added_cues.items()
                if (row := self._cue_store.get(cue_id)) is not None
            },
        }


def _subset_failures(expected: Any, actual: Any, where: str) -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        failures = []
        for key, value in expected.items():
            path = f"{where}.{key}"
            if key not in actual:
                failures.append(f"{path} is missing")
            else:
                failures.extend(_subset_failures(value, actual[key], path))
        return failures
    if isinstance(expected, list) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return [f"{where}: expected {len(expected)} items, got {len(actual)}"]
        return [
            failure
            for index, (wanted, got) in enumerate(zip(expected, actual, strict=True))
            for failure in _subset_failures(wanted, got, f"{where}[{index}]")
        ]
    return [] if expected == actual else [f"{where}: expected {expected!r}, got {actual!r}"]


def run_live_scenario(
    scenario: dict[str, Any], *, policy_client: Any = None, route: LlmRoute | None = None,
) -> dict[str, Any]:
    """Replay synthetic inputs with a fresh in-memory SQLite database."""
    if not isinstance(scenario, dict) or set(scenario) - {
        "name", "start_at", "max_wakes_per_hour", "min_gap_after_speech_ms", "steps",
    }:
        raise ValueError("scenario contains unknown fields or is not an object")
    steps = scenario["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("scenario.steps must be a nonempty list")
    start = datetime.fromisoformat(scenario["start_at"]) if "start_at" in scenario else _START
    if start.tzinfo is None:
        raise ValueError("start_at must include a timezone")
    max_wakes = scenario.get("max_wakes_per_hour", 6)
    if type(max_wakes) is not int or max_wakes != clamp_live_main_wake_max_per_hour(max_wakes):
        raise ValueError("max_wakes_per_hour must be an integer from 0 to 30")
    min_gap_ms = scenario.get("min_gap_after_speech_ms", 8000)
    if type(min_gap_ms) is not int or min_gap_ms != clamp_live_min_gap_after_speech_ms(min_gap_ms):
        raise ValueError("min_gap_after_speech_ms must be an integer from 0 to 60000")
    clock = _Clock(start)
    route = route or LlmRoute(provider_id="scenario", model="scripted", max_tokens=384)
    with timephrase.scoped_now(clock.now):
        host = _SimulationHost(clock, route, policy_client, max_wakes, min_gap_ms)
        try:
            return _run_steps(host, steps, name=scenario.get("name", "Live scenario"))
        finally:
            host._chat_db._get_conn().close()


def _run_steps(host: _SimulationHost, steps: list, *, name: str) -> dict[str, Any]:
    timeline = []
    failures: list[str] = []
    for step_index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or set(step) - _STEP_KEYS:
            raise ValueError(f"step {step_index} contains unknown fields or is not an object")
        commands = [
            key for key in ("impulse", "advance_ms", "cue", "concept", "dispatch", "reply")
            if key in step
        ]
        if len(commands) > 1:
            raise ValueError(f"step {step_index} must have at most one command")
        command = commands[0] if commands else "observe"
        host._live_policy_client.proposal = step.get("proposal", {})
        host._apply_state(step.get("state", {}))
        before_calls = host._live_policy_client.calls
        before_decisions = {
            row["decision_id"] for row in
            host._live_policy_controller.diagnostics()["decision_trace"]["decisions"]
        }
        if command == "impulse":
            host.publish(step["impulse"], step.get("payload", {}))
        elif command == "advance_ms":
            amount = step["advance_ms"]
            if type(amount) is not int or amount < 0:
                raise ValueError(f"step {step_index} advance_ms must be a nonnegative integer")
            host.clock.elapsed_ms += amount
            host._live_heartbeat_tick()
        elif command == "dispatch":
            host.dispatch(step["dispatch"])
        elif command == "reply":
            host.reply(step["reply"])
        else:
            if command == "cue":
                host.add_cue(step["cue"])
            elif command == "concept":
                host.add_concept(step["concept"])
            host.refresh_live_situation(trigger_kind="heartbeat")
        while host._pending_policy:
            host._pending_policy.popleft()()
        result = host.snapshot()
        result["decisions"] = [
            row for row in result["decisions"] if row["decision_id"] not in before_decisions
        ]
        subjects = {urge["urge_id"]: urge["subject"] for urge in result["urges"]}
        for row in result["decisions"]:
            row["selected_subject"] = subjects.get(row["selected_urge_id"], "")
        result["new_policy_calls"] = result["policy_calls"] - before_calls
        result["step"] = step_index
        result["label"] = step.get("label", step.get("impulse", command))
        failures.extend(_subset_failures(step.get("expect", {}), result, f"step {step_index}"))
        timeline.append(result)
    return {
        "name": name, "policy": host.route.model, "steps": timeline,
        "elapsed_ms": host.clock.elapsed_ms, "dispatches": host.dispatches, "spoken": host.spoken,
        "expectation_failures": failures,
    }

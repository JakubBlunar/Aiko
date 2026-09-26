"""Live presence mode: posture, impulse bus, situation, inclination, policy, embodiment.

Pass 10 admits unprompted ``request_main_speech`` through a strict gate
into a BrainLoop-gated main-model turn. Micros stay Live actions (no
relationship-turn / post-turn cascade). User text/STT still wake
TurnRunner via a deterministic request_main_speech reflex.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Literal

from app.core.activity.evidence import ActivityEvidence, evidence_from_store
from app.core.brain.events import ProactiveEvent
from app.core.concepts.concept_diets import diet_for, resolve_budget, tuning_from_route
from app.core.infra import timephrase
from app.core.infra.agent_settings_parse import (
    clamp_live_main_wake_max_per_hour,
    clamp_live_min_gap_after_speech_ms,
)
from app.core.infra.settings import LLM_ROLE_LIVE_POLICY, persist_user_overrides
from app.core.live.actions import action_result_kind
from app.core.live.assembler import LiveAssembleInput, LiveSituationAssembler
from app.core.live.bus import LiveImpulseBus
from app.core.live.labels import cap_live_subject
from app.core.live.capabilities import semantic_capabilities_from_profile
from app.core.live.controller import LivePolicyController, USER_INTENT_KINDS
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.diagnostics import sanitize_live_dump
from app.core.live.epochs import classify_epoch
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame, overlay_from_frame
from app.core.live.heartbeat import LiveHeartbeat
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.journal import JOURNAL_KINDS, LiveExperienceJournal
from app.core.live.main_wake import (
    TRANSCRIPT_MAX_ROWS,
    main_wake_floor_busy,
    render_live_talk_about,
)
from app.core.live.micro_utterance import validate_micro_utterance
from app.core.live.policy_context import LivePolicyContextRuntime
from app.core.live.resolver import LiveBehaviorResolver, ResolvedBehaviorPlan
from app.core.live.urge_menu import menu_urge_ids
from app.core.services.response_text_service import strip_all_meta_tags
from app.core.session.session_text_utils import sanitize_assistant_text
from app.llm.token_utils import estimate_tokens


log = logging.getLogger("app.session")
live_log = logging.getLogger("app.live")

BEHAVIOR_POSTURE_TURN_BASED = "turn_based"
BEHAVIOR_POSTURE_LIVE = "live_presence"
_VALID_POSTURES = frozenset({
    BEHAVIOR_POSTURE_TURN_BASED,
    BEHAVIOR_POSTURE_LIVE,
})
_SITUATION_WORKER_S = 180.0
_MEMORY_EXTRACT_S = 300.0
_VITALITY_TICK_S = 30.0
_IDLE_RECONSIDER_MIN_MS = 15_000
_LIVE_INFO_IMPULSE_KINDS = frozenset({
    "silence.wake",
    "activity.session_changed",
    "activity.idle",
    "activity.lock",
    "sleep.status_changed",
    "user.message_sent",
    "user.speech_final",
    "situation.shared_commitment_changed",
    "idle.reconsider",
})


def normalize_behavior_posture(raw: Any) -> str:
    value = str(raw or BEHAVIOR_POSTURE_TURN_BASED).strip().lower()
    if value in _VALID_POSTURES:
        return value
    return BEHAVIOR_POSTURE_TURN_BASED


class LiveModeMixin:
    """Two-axis Live posture + impulse publication + situation assembly."""

    def _init_live_mode(self) -> None:
        self._live_mode_generation = 0
        self._live_impulse_bus = LiveImpulseBus()
        self._live_situation_assembler = LiveSituationAssembler()
        self._live_situation_overlay = None
        self._live_journal: LiveExperienceJournal | None = None
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is not None:
            try:
                self._live_journal = LiveExperienceJournal(chat_db)
            except Exception:
                log.debug("live journal init failed", exc_info=True)
                self._live_journal = None
        self._live_last_vitality_tick = 0.0
        self._live_last_mood_label = ""
        self._live_last_vitality_band = ""
        self._live_last_memory_extract = 0.0
        self._live_last_idle_reconsider = 0.0
        self._live_last_sleep_status = ""
        self._live_last_shared = False
        self._live_inclination = LiveInclinationRuntime(
            cue_adapter=CueUrgeAdapter(
                pending_provider=self._live_pending_cues,
                nudge_provider=self._live_prepared_nudge,
            ),
        )
        self._live_policy_context = LivePolicyContextRuntime(
            view_provider=self._live_concept_view,
            embedder_provider=lambda: getattr(self, "_embedder", None),
            token_budget_provider=self._live_policy_concept_token_budget,
        )
        self._live_policy_controller = LivePolicyController(
            client_provider=lambda: getattr(self, "_live_policy_client", None),
            model_provider=self._live_policy_model,
            route_provider=lambda: (
                self._route_or_none(LLM_ROLE_LIVE_POLICY)
                if callable(getattr(self, "_route_or_none", None))
                else None
            ),
            prompt_ceiling_provider=self._live_policy_prompt_ceiling,
            generation_provider=lambda: int(
                getattr(self, "_live_mode_generation", 0) or 0
            ),
            ledger_recorder=self._live_policy_record_outcome,
            inclination_provider=lambda: getattr(self, "_live_inclination", None),
            on_executed=self._refresh_live_embodiment,
            on_micro_utterance=self._deliver_live_micro_utterance,
            on_main_wake=self._enqueue_live_main_wake,
            on_action_result=self._publish_live_action_result,
            unprompted_speech_provider=self._live_unprompted_speech_enabled,
        )
        self._live_behavior_resolver = LiveBehaviorResolver()
        self._live_behavior_plan: ResolvedBehaviorPlan | None = None
        self._live_embodiment_signature: tuple[Any, ...] | None = None
        self._live_embodiment_listeners: list[Any] = []
        self._live_talk_about_payload: dict[str, Any] | None = None
        self._live_last_aiko_spoke_ms: float | None = None
        self._live_last_semantic_action_ms: float | None = None
        self._live_heartbeat = LiveHeartbeat(self._live_heartbeat_tick)
        self._apply_live_main_wake_budget()
        if self._live_heartbeat_enabled():
            self._live_heartbeat.start()
        drained = getattr(self, "_playback_drained", None)
        if drained is None:
            drained = threading.Event()
            drained.set()
            self._playback_drained = drained

    def _live_heartbeat_enabled(self) -> bool:
        return self.is_live_presence() and getattr(self, "_chat_db", None) is not None

    def is_live_presence(self) -> bool:
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        return (
            normalize_behavior_posture(
                getattr(agent, "behavior_posture", BEHAVIOR_POSTURE_TURN_BASED),
            )
            == BEHAVIOR_POSTURE_LIVE
        )

    def live_capture_available(self) -> bool:
        """True while the live voice session is active (mic up)."""
        return bool(getattr(self, "_live_voice_session_active", False))

    def notify_playback_drained(self) -> None:
        """Client finished playing the last TTS buffer (H7)."""
        event = getattr(self, "_playback_drained", None)
        if event is None:
            event = threading.Event()
            self._playback_drained = event
        event.set()
        self.publish_live_impulse(
            kind="aiko.playback_drained",
            source="web.audio",
            coalesce_key="aiko.playback_drained",
            privacy="local_state",
            priority="control",
            ttl_ms=4000,
        )

    def mark_playback_pending(self) -> None:
        """TTS is audible (or about to be); wait for a client drain ack."""
        event = getattr(self, "_playback_drained", None)
        if event is None:
            event = threading.Event()
            self._playback_drained = event
        event.clear()

    def is_playback_drained(self) -> bool:
        event = getattr(self, "_playback_drained", None)
        if event is None:
            return True
        return bool(event.is_set())

    def live_impulse_bus_enabled(self) -> bool:
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        if bool(getattr(agent, "live_impulse_bus_enabled", False)):
            return True
        return self.is_live_presence()

    def bump_live_mode_generation(self, reason: str = "") -> int:
        current = int(getattr(self, "_live_mode_generation", 0) or 0) + 1
        self._live_mode_generation = current
        bus = getattr(self, "_live_impulse_bus", None)
        if bus is not None:
            try:
                bus.drop_stale_generation(current)
            except Exception:
                log.debug("live generation drop failed", exc_info=True)
        if reason:
            log.debug("live mode_generation=%s reason=%s", current, reason)
        controller = getattr(self, "_live_policy_controller", None)
        if controller is not None:
            try:
                controller.cancel()
            except Exception:
                log.debug("live policy cancel failed", exc_info=True)
        self.publish_live_impulse(
            kind="user.session_changed",
            source="session.lifecycle",
            coalesce_key="user.session_changed",
            privacy="local_state",
            ttl_ms=15_000,
            payload={"reason": str(reason or "")},
        )
        return current

    def set_behavior_posture(self, posture: str) -> str:
        """Mutate Live posture and persist in the same call."""
        normalized = normalize_behavior_posture(posture)
        agent = self._settings.agent  # type: ignore[attr-defined]
        previous = normalize_behavior_posture(
            getattr(agent, "behavior_posture", BEHAVIOR_POSTURE_TURN_BASED),
        )
        agent.behavior_posture = normalized
        try:
            persist_user_overrides({"agent": {"behavior_posture": normalized}})
        except Exception:
            log.debug("persist behavior_posture failed", exc_info=True)
        if normalized != previous:
            self.bump_live_mode_generation("posture")
        heartbeat = getattr(self, "_live_heartbeat", None)
        if heartbeat is not None:
            if self._live_heartbeat_enabled():
                heartbeat.start()
            else:
                heartbeat.stop()
        if normalized != previous:
            if normalized == BEHAVIOR_POSTURE_LIVE:
                self._live_policy_set_loaded(True)
                self.refresh_live_situation(trigger_kind="heartbeat")
            else:
                self._live_policy_set_loaded(False)
                self._clear_live_embodiment()
            live_log.info(
                "live posture: live=%s quiet=%s unprompted=%s generation=%s",
                normalized == BEHAVIOR_POSTURE_LIVE,
                bool(getattr(agent, "live_quiet", False)),
                self._live_unprompted_speech_enabled(),
                int(getattr(self, "_live_mode_generation", 0) or 0),
            )
        return normalized

    def set_live_quiet(self, quiet: bool) -> None:
        quiet = bool(quiet)
        self._settings.agent.live_quiet = quiet  # type: ignore[attr-defined]
        try:
            persist_user_overrides({"agent": {"live_quiet": quiet}})
        except Exception:
            log.debug("persist live_quiet failed", exc_info=True)
        live_log.info(
            "live posture: live=%s quiet=%s unprompted=%s generation=%s",
            self.is_live_presence(),
            quiet,
            self._live_unprompted_speech_enabled(),
            int(getattr(self, "_live_mode_generation", 0) or 0),
        )

    def set_live_unprompted_speech(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._settings.agent.live_unprompted_speech = enabled  # type: ignore[attr-defined]
        try:
            persist_user_overrides({"agent": {"live_unprompted_speech": enabled}})
        except Exception:
            log.debug("persist live_unprompted_speech failed", exc_info=True)
        live_log.info(
            "live posture: live=%s quiet=%s unprompted=%s generation=%s",
            self.is_live_presence(),
            bool(getattr(self._settings.agent, "live_quiet", False)),
            enabled,
            int(getattr(self, "_live_mode_generation", 0) or 0),
        )

    def set_live_main_wake_max_per_hour(self, value: int) -> None:
        clamped = clamp_live_main_wake_max_per_hour(value)
        self._settings.agent.live_main_wake_max_per_hour = clamped  # type: ignore[attr-defined]
        try:
            persist_user_overrides(
                {"agent": {"live_main_wake_max_per_hour": clamped}},
            )
        except Exception:
            log.debug("persist live_main_wake_max_per_hour failed", exc_info=True)
        self._apply_live_main_wake_budget()

    def set_live_min_gap_after_speech_ms(self, value: int) -> None:
        clamped = clamp_live_min_gap_after_speech_ms(value)
        self._settings.agent.live_min_gap_after_speech_ms = clamped  # type: ignore[attr-defined]
        try:
            persist_user_overrides(
                {"agent": {"live_min_gap_after_speech_ms": clamped}},
            )
        except Exception:
            log.debug("persist live_min_gap_after_speech_ms failed", exc_info=True)

    def set_live_mic_consented(self, consented: bool) -> None:
        consented = bool(consented)
        self._settings.agent.live_mic_consented = consented  # type: ignore[attr-defined]
        try:
            persist_user_overrides({"agent": {"live_mic_consented": consented}})
        except Exception:
            log.debug("persist live_mic_consented failed", exc_info=True)

    def _live_unprompted_speech_enabled(self) -> bool:
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        return bool(getattr(agent, "live_unprompted_speech", True))

    def _apply_live_main_wake_budget(self) -> None:
        runtime = getattr(self, "_live_inclination", None)
        if runtime is None:
            return
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        runtime.budget.configure_main_wake(
            clamp_live_main_wake_max_per_hour(
                getattr(agent, "live_main_wake_max_per_hour", 6),
            ),
        )

    def _live_min_gap_after_speech_ms(self) -> int:
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        return clamp_live_min_gap_after_speech_ms(
            getattr(agent, "live_min_gap_after_speech_ms", 8000),
        )

    def set_live_impulse_bus_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._settings.agent.live_impulse_bus_enabled = enabled  # type: ignore[attr-defined]
        try:
            persist_user_overrides(
                {"agent": {"live_impulse_bus_enabled": enabled}},
            )
        except Exception:
            log.debug("persist live_impulse_bus_enabled failed", exc_info=True)

    def publish_live_impulse(
        self,
        *,
        kind: str,
        source: str,
        coalesce_key: str,
        privacy: str = "local_state",
        priority: str = "attention",
        ttl_ms: int = 8000,
        payload: dict[str, Any] | None = None,
        refresh: bool = True,
    ) -> None:
        if not self.live_impulse_bus_enabled():
            return
        bus = getattr(self, "_live_impulse_bus", None)
        if bus is None:
            return
        published = None
        try:
            published = bus.publish(
                kind=kind,
                source=source,
                session_key=str(getattr(self, "session_key", "") or ""),
                mode_generation=int(
                    getattr(self, "_live_mode_generation", 0) or 0,
                ),
                coalesce_key=coalesce_key,
                privacy=privacy,
                priority=priority,
                ttl_ms=ttl_ms,
                payload=payload,
            )
        except Exception:
            log.debug("publish_live_impulse failed", exc_info=True)
            return
        if published is None:
            return
        self._log_live_impulse(kind, payload or {})
        self._maybe_append_live_journal(
            kind=kind,
            text=str((payload or {}).get("text") or kind.replace(".", " ")),
            privacy=privacy,
            evidence_ids=(published.event_id,),
        )
        if refresh:
            self.refresh_live_situation(trigger_kind=kind)

    def _publish_live_action_result(self, record: Any) -> None:
        """Ack a Live action without re-entering situation refresh."""
        kind = ""
        payload: dict[str, Any] = {}
        try:
            kind = action_result_kind(getattr(record, "state", ""))
            to_payload = getattr(record, "to_impulse_payload", None)
            if callable(to_payload):
                payload = dict(to_payload())
        except Exception:
            log.debug("live action result payload failed", exc_info=True)
            return
        if not kind:
            return
        action_id = str(payload.get("action_id") or "")
        self.publish_live_impulse(
            kind=kind,
            source="live.action",
            coalesce_key=f"{kind}:{action_id}" if action_id else kind,
            privacy="local_state",
            priority="control",
            ttl_ms=4000,
            payload=payload,
            refresh=False,
        )

    def _live_inner_mood_and_band(self) -> tuple[str, str]:
        mood = ""
        band = ""
        snapshot_fn = getattr(self, "conversation_situation_snapshot", None)
        if not callable(snapshot_fn):
            return mood, band
        try:
            snap = snapshot_fn(fresh=True)
            mood = str(getattr(snap, "mood_label", "") or "")
            band = str(getattr(snap, "vitality_band", "") or "")
        except Exception:
            log.debug("live inner-state snapshot failed", exc_info=True)
        return mood, band

    def _publish_live_inner_state_impulses(self) -> None:
        """Ack affect/vitality edges without re-entering situation refresh."""
        mood, band = self._live_inner_mood_and_band()
        last_mood = str(getattr(self, "_live_last_mood_label", "") or "")
        last_band = str(getattr(self, "_live_last_vitality_band", "") or "")
        if mood and mood != last_mood:
            self.publish_live_impulse(
                kind="aiko.affect_changed",
                source="live.inner_state",
                coalesce_key="aiko.affect_changed",
                privacy="local_state",
                priority="control",
                ttl_ms=4000,
                payload={"mood_label": mood},
                refresh=False,
            )
        if band and band != last_band:
            self.publish_live_impulse(
                kind="aiko.vitality_changed",
                source="live.inner_state",
                coalesce_key="aiko.vitality_changed",
                privacy="local_state",
                priority="control",
                ttl_ms=4000,
                payload={"band": band},
                refresh=False,
            )
        self._live_last_mood_label = mood
        self._live_last_vitality_band = band

    def _log_live_impulse(self, kind: str, payload: dict[str, Any]) -> None:
        token = str(kind or "")
        if token not in _LIVE_INFO_IMPULSE_KINDS:
            return
        app_name = str(payload.get("app") or "").strip() or "-"
        os_idle = str(payload.get("os_idle") or "").strip() or "-"
        live_log.info(
            "live impulse: kind=%s epoch=%s app=%s os_idle=%s generation=%s",
            token,
            classify_epoch(token),
            app_name,
            os_idle,
            int(getattr(self, "_live_mode_generation", 0) or 0),
        )

    def publish_live_user_meaning(self, text: str, *, mode: str) -> None:
        """Shadow dual-write of a submitted chat line or STT final."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        kind = (
            "user.speech_final"
            if str(mode).strip().lower() in {"voice", "live"}
            else "user.message_sent"
        )
        self.publish_live_impulse(
            kind=kind,
            source="session.turn",
            coalesce_key=kind,
            privacy="transcript",
            priority="attention",
            ttl_ms=30_000,
            payload={"text": cleaned, "mode": str(mode or "typed")},
        )

    def live_impulse_diagnostics(self) -> dict[str, Any]:
        bus = getattr(self, "_live_impulse_bus", None)
        snap = bus.snapshot() if bus is not None else {}
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        return sanitize_live_dump({
            **snap,
            "behavior_posture": normalize_behavior_posture(
                getattr(agent, "behavior_posture", BEHAVIOR_POSTURE_TURN_BASED),
            ),
            "live_quiet": bool(getattr(agent, "live_quiet", False)),
            "live_unprompted_speech": bool(
                getattr(agent, "live_unprompted_speech", True),
            ),
            "live_main_wake_max_per_hour": clamp_live_main_wake_max_per_hour(
                getattr(agent, "live_main_wake_max_per_hour", 6),
            ),
            "live_min_gap_after_speech_ms": clamp_live_min_gap_after_speech_ms(
                getattr(agent, "live_min_gap_after_speech_ms", 8000),
            ),
            "live_mic_consented": bool(
                getattr(agent, "live_mic_consented", False),
            ),
            "live_impulse_bus_enabled": bool(
                getattr(agent, "live_impulse_bus_enabled", False),
            ),
            "bus_accepting": self.live_impulse_bus_enabled(),
            "mode_generation": int(
                getattr(self, "_live_mode_generation", 0) or 0,
            ),
            "capture_available": self.live_capture_available(),
            "live_voice_session_active": bool(
                getattr(self, "_live_voice_session_active", False),
            ),
        })

    def live_situation_frame(self) -> LiveSituationFrame | None:
        assembler = getattr(self, "_live_situation_assembler", None)
        if assembler is None:
            return None
        frame = assembler.last_frame
        if frame is not None:
            return frame
        return self.refresh_live_situation(trigger_kind="heartbeat")

    def live_situation_diagnostics(self) -> dict[str, Any]:
        frame = self.live_situation_frame()
        journal = getattr(self, "_live_journal", None)
        session_id = str(getattr(self, "session_key", "") or "")
        tail: list[dict[str, Any]] = []
        if journal is not None:
            try:
                tail = [entry.to_payload() for entry in journal.tail(session_id)]
            except Exception:
                log.debug("live journal tail failed", exc_info=True)
        assembler = getattr(self, "_live_situation_assembler", None)
        return sanitize_live_dump({
            **self.live_impulse_diagnostics(),
            "frame": frame.to_payload() if frame is not None else None,
            "frame_generation": (
                int(assembler.generation) if assembler is not None else 0
            ),
            "heartbeat_running": bool(
                getattr(getattr(self, "_live_heartbeat", None), "running", False)
            ),
            "journal": tail,
            **self._live_inclination_diagnostics(),
            **self._live_policy_diagnostics(),
            **self._live_embodiment_diagnostics(),
        })

    def _live_activity_evidence(self) -> ActivityEvidence | None:
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        if not bool(getattr(agent, "activity_awareness_enabled", False)):
            return None
        try:
            return evidence_from_store(
                getattr(self, "_activity_store", None),
                now=timephrase.utcnow(),
            )
        except Exception:
            log.debug("live activity evidence failed", exc_info=True)
            return None

    def _live_current_actions(self) -> tuple[str, ...]:
        controller = getattr(self, "_live_policy_controller", None)
        held = getattr(controller, "last_accepted_nonverbal", None) or {}
        if not isinstance(held, dict):
            return ()
        intent = str(held.get("intent") or "").strip()
        if not intent:
            return ()
        until = float(held.get("hold_until_ms") or 0.0)
        if until and time.monotonic() * 1000.0 >= until:
            return ()
        return (intent,)

    def _live_recent_actions(self) -> tuple[str, ...]:
        policy = getattr(self, "_live_policy_context", None)
        getter = getattr(policy, "recent_completed_actions", None)
        if not callable(getter):
            return ()
        try:
            return tuple(getter(limit=4))
        except Exception:
            log.debug("live recent actions failed", exc_info=True)
            return ()

    def _live_relationship_phase(self) -> str:
        tracker = getattr(self, "_relationship_tracker", None)
        current = getattr(tracker, "current_phase", None)
        if not callable(current):
            return ""
        user_id = str(getattr(self, "_user_id", "") or "")
        if not user_id:
            return ""
        try:
            return str(current(user_id) or "")
        except Exception:
            log.debug("live relationship phase failed", exc_info=True)
            return ""

    def _live_goal_summaries(self) -> tuple[str, ...]:
        store = getattr(self, "_goal_store", None)
        lister = getattr(store, "list_active", None)
        if not callable(lister):
            return ()
        try:
            active = list(lister())
        except Exception:
            log.debug("live goal summaries failed", exc_info=True)
            return ()
        out: list[str] = []
        for goal in active:
            meta = getattr(goal, "metadata", None) or {}
            if not isinstance(meta, dict):
                meta = {}
            summary = str(
                meta.get("summary") or getattr(goal, "content", "") or ""
            ).strip()
            capped = cap_live_subject(summary)
            if not capped or capped in out:
                continue
            out.append(capped)
            if len(out) >= 3:
                break
        return tuple(out)

    def _live_resource_contention(self) -> str:
        client = getattr(self, "_live_policy_client", None)
        if client is None:
            return "independent"
        return (
            "shared_worker"
            if getattr(client, "_gate", None) is not None
            else "independent"
        )

    def _live_mark_aiko_spoke(self) -> None:
        self._live_last_aiko_spoke_ms = time.monotonic() * 1000.0

    def refresh_live_situation(
        self,
        *,
        trigger_kind: str = "heartbeat",
        now_mono_ms: float | None = None,
    ) -> LiveSituationFrame | None:
        if not self.live_impulse_bus_enabled() and not self.is_live_presence():
            return None
        snapshot_fn = getattr(self, "conversation_situation_snapshot", None)
        if not callable(snapshot_fn):
            return None
        try:
            snapshot = snapshot_fn(fresh=True)
        except Exception:
            log.debug("live situation snapshot failed", exc_info=True)
            return None
        bus = getattr(self, "_live_impulse_bus", None)
        impulses = bus.tail_impulses() if bus is not None else ()
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        assembler = getattr(self, "_live_situation_assembler", None)
        if assembler is None:
            assembler = LiveSituationAssembler()
            self._live_situation_assembler = assembler
        sleep_status = ""
        if isinstance(getattr(snapshot, "sleep", None), dict):
            sleep_status = str(snapshot.sleep.get("status") or "")
        shared = bool(getattr(snapshot, "shared_commitment_active", False))
        if (
            self._live_last_sleep_status
            and sleep_status
            and sleep_status != self._live_last_sleep_status
        ):
            self._maybe_append_live_journal(
                kind="sleep.status_changed",
                text=f"sleep status {self._live_last_sleep_status} to {sleep_status}",
            )
            trigger_kind = "sleep.status_changed"
        if self._live_last_shared != shared and (
            self._live_last_sleep_status or assembler.last_frame is not None
        ):
            self._maybe_append_live_journal(
                kind="situation.shared_commitment_changed",
                text=(
                    "shared commitment on" if shared else "shared commitment off"
                ),
            )
            trigger_kind = "situation.shared_commitment_changed"
        inp = LiveAssembleInput(
            snapshot=snapshot,
            impulses=impulses,
            now=timephrase.utcnow(),
            monotonic_ms=(
                time.monotonic() * 1000.0
                if now_mono_ms is None
                else float(now_mono_ms)
            ),
            live_quiet=bool(getattr(agent, "live_quiet", False)),
            capture_available=self.live_capture_available(),
            turn_in_progress=bool(getattr(self, "_turn_in_progress", False)),
            live_voice_session_active=bool(
                getattr(self, "_live_voice_session_active", False),
            ),
            mode_generation=int(getattr(self, "_live_mode_generation", 0) or 0),
            trigger_kind=str(trigger_kind or "heartbeat"),
            tts_active=bool(
                getattr(self, "is_tts_playing", lambda: False)()
            ) if callable(getattr(self, "is_tts_playing", None)) else False,
            playback_active=not bool(self.is_playback_drained()),
            last_aiko_spoke_ms=getattr(self, "_live_last_aiko_spoke_ms", None),
            last_semantic_action_ms=getattr(
                self, "_live_last_semantic_action_ms", None,
            ),
            current_actions=self._live_current_actions(),
            recent_actions=self._live_recent_actions(),
            relationship_phase=self._live_relationship_phase(),
            goals=self._live_goal_summaries(),
            resource_contention=self._live_resource_contention(),
            activity_evidence=self._live_activity_evidence(),
        )
        try:
            frame = assembler.assemble(inp)
        except Exception:
            log.debug("live situation assemble failed", exc_info=True)
            return None
        runtime = getattr(self, "_live_inclination", None)
        if runtime is not None:
            try:
                frame = runtime.apply(
                    frame,
                    trigger_kind=str(trigger_kind or "heartbeat"),
                    now_mono_ms=float(inp.monotonic_ms),
                    live_quiet=bool(getattr(agent, "live_quiet", False)),
                )
                assembler.remember(frame)
            except Exception:
                log.debug("live inclination apply failed", exc_info=True)
        policy = getattr(self, "_live_policy_context", None)
        if policy is not None:
            try:
                frame = policy.apply(
                    frame,
                    urges=getattr(runtime, "urges", None),
                    trigger_kind=str(trigger_kind or "heartbeat"),
                    live_quiet=bool(getattr(agent, "live_quiet", False)),
                    min_gap_after_speech_ms=self._live_min_gap_after_speech_ms(),
                )
                assembler.remember(frame)
            except Exception:
                log.debug("live policy context apply failed", exc_info=True)
        if self.is_live_presence():
            self._maybe_run_live_policy(
                frame, trigger_kind=str(trigger_kind or "heartbeat"),
            )
        self._live_situation_overlay = overlay_from_frame(frame)
        self._live_last_sleep_status = frame.constraints.sleep_status
        self._live_last_shared = frame.shared.shared_commitment_active
        cache = getattr(self, "_conversation_situation_snapshot_cache", None)
        if cache is not None:
            self._conversation_situation_snapshot_cache = None
        self._refresh_live_embodiment(frame)
        return frame

    def _live_tts_active(self) -> bool:
        playing = getattr(self, "is_tts_playing", None)
        if not callable(playing):
            return False
        try:
            return bool(playing())
        except Exception:
            return False

    def _live_policy_inflight(self) -> bool:
        controller = getattr(self, "_live_policy_controller", None)
        if controller is None:
            return False
        return bool(getattr(controller, "_inflight", False))

    def _live_idle_reconsider_due(self, now_mono: float) -> bool:
        """True when wait expiry may promote one coalesced 4B tick.

        Heartbeat itself stays data_only. This is the semantic timer the
        spec allows, not a 1 Hz poll.
        """
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        if bool(getattr(agent, "live_quiet", False)):
            return False
        if self._live_tts_active() or self._live_policy_inflight():
            return False
        sleep_status = str(getattr(self, "_live_last_sleep_status", "") or "")
        assembler = getattr(self, "_live_situation_assembler", None)
        frame = getattr(assembler, "last_frame", None) if assembler is not None else None
        if frame is not None:
            sleep_status = str(frame.constraints.sleep_status or "")
        elif callable(getattr(self, "conversation_situation_snapshot", None)):
            try:
                snap = self.conversation_situation_snapshot(fresh=True)
                sleep = getattr(snap, "sleep", None)
                if isinstance(sleep, dict):
                    sleep_status = str(sleep.get("status") or "")
            except Exception:
                pass
        if sleep_status in SLEEP_SPEECH_FORBID:
            return False
        last = float(getattr(self, "_live_last_idle_reconsider", 0.0) or 0.0)
        if last and (now_mono - last) * 1000.0 < _IDLE_RECONSIDER_MIN_MS:
            return False
        runtime = getattr(self, "_live_inclination", None)
        wait = getattr(getattr(runtime, "wait", None), "current", None)
        if wait is None:
            return False
        deadline = float(getattr(wait, "deadline_monotonic_ms", 0.0) or 0.0)
        return now_mono * 1000.0 >= deadline

    def _note_idle_reconsider_impulse(self) -> None:
        if not self.live_impulse_bus_enabled():
            return
        bus = getattr(self, "_live_impulse_bus", None)
        if bus is None:
            return
        try:
            bus.publish(
                kind="idle.reconsider",
                source="live.heartbeat",
                session_key=str(getattr(self, "session_key", "") or ""),
                mode_generation=int(
                    getattr(self, "_live_mode_generation", 0) or 0,
                ),
                coalesce_key="idle.reconsider",
                privacy="local_state",
                priority="attention",
                ttl_ms=8000,
            )
        except Exception:
            log.debug("idle.reconsider impulse failed", exc_info=True)
            return
        self._log_live_impulse("idle.reconsider", {})

    def _live_heartbeat_tick(self) -> None:
        if not self.is_live_presence():
            return
        if bool(getattr(self, "_turn_in_progress", False)):
            return
        now_mono = time.monotonic()
        self._tick_live_affect()
        if now_mono - float(self._live_last_vitality_tick or 0.0) >= _VITALITY_TICK_S:
            self._live_last_vitality_tick = now_mono
            self._tick_live_vitality()
        self._publish_live_inner_state_impulses()
        trigger = "heartbeat"
        if self._live_idle_reconsider_due(now_mono):
            trigger = "idle.reconsider"
            self._live_last_idle_reconsider = now_mono
            self._note_idle_reconsider_impulse()
        self.refresh_live_situation(trigger_kind=trigger)
        self._maybe_schedule_live_situation_worker()
        self._maybe_live_memory_extract(now_mono)

    def _tick_live_affect(self) -> None:
        updater = getattr(self, "_affect_updater", None)
        if updater is None or not hasattr(updater, "tick_elapsed"):
            return
        try:
            updater.tick_elapsed(getattr(self, "_user_id", "default"))
        except Exception:
            log.debug("live affect tick failed", exc_info=True)

    def _tick_live_vitality(self) -> None:
        snapshot_fn = getattr(self, "vitality_snapshot", None)
        if not callable(snapshot_fn):
            return
        try:
            snap = snapshot_fn()
        except Exception:
            log.debug("live vitality snapshot failed", exc_info=True)
            return
        energy = snap.get("energy") if isinstance(snap, dict) else None
        notify = getattr(self, "_notify_vitality", None)
        if energy is None or not callable(notify):
            return
        try:
            notify(float(energy))
        except Exception:
            log.debug("live vitality notify failed", exc_info=True)

    def _maybe_schedule_live_situation_worker(self) -> None:
        worker = getattr(self, "_conversation_situation_worker", None)
        if worker is None or not bool(getattr(self, "_remember_history", True)):
            return
        frame = getattr(
            getattr(self, "_live_situation_assembler", None), "last_frame", None,
        )
        inferred_stale = True
        if frame is not None:
            inferred_stale = bool(frame.shared.inferred_stale)
        journal = getattr(self, "_live_journal", None)
        has_journal = False
        if journal is not None:
            try:
                has_journal = bool(
                    journal.has_unextracted_memory_eligible(self.session_key)
                    or journal.tail(self.session_key, limit=1)
                )
            except Exception:
                has_journal = False
        try:
            if not worker.should_run_live(
                self.session_key,
                inferred_stale=inferred_stale,
                has_new_journal=has_journal,
                min_interval_s=_SITUATION_WORKER_S,
            ):
                return
        except Exception:
            log.debug("live situation worker cadence failed", exc_info=True)
            return
        try:
            worker.mark_live_run(self.session_key)
        except Exception:
            log.debug("live situation worker mark failed", exc_info=True)
            return

        session_key = self.session_key

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                worker.run(session_key)
            except Exception:
                log.debug("live situation worker run failed", exc_info=True)

        scheduler = getattr(self, "_scheduler", None)
        if scheduler is None:
            try:
                worker.run(session_key)
            except Exception:
                log.debug("live situation worker run failed", exc_info=True)
            return
        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            scheduler.submit(ScheduledJob(
                name="conversation_situation_live",
                priority=71,
                estimated_seconds=3.0,
                callable=_job,
                dedupe_key=f"conversation_situation_live:{session_key}",
            ))
        except Exception:
            log.debug("live situation worker schedule failed", exc_info=True)

    def _maybe_live_memory_extract(self, now_mono: float) -> None:
        if now_mono - float(self._live_last_memory_extract or 0.0) < _MEMORY_EXTRACT_S:
            return
        journal = getattr(self, "_live_journal", None)
        extractor = getattr(self, "_memory_extractor", None)
        if journal is None or extractor is None:
            return
        try:
            if not journal.has_unextracted_memory_eligible(self.session_key):
                return
        except Exception:
            return
        self._live_last_memory_extract = now_mono
        try:
            extractor.extract_for_session(self.session_key)
        except Exception:
            log.debug("live memory extract failed", exc_info=True)

    def _maybe_append_live_journal(
        self,
        *,
        kind: str,
        text: str,
        privacy: str = "local_state",
        evidence_ids: tuple[str, ...] = (),
    ) -> None:
        if kind not in JOURNAL_KINDS:
            return
        journal = getattr(self, "_live_journal", None)
        if journal is None:
            return
        try:
            journal.append(
                session_id=str(getattr(self, "session_key", "") or ""),
                kind=kind,
                text=text,
                privacy=privacy,
                evidence_ids=evidence_ids,
            )
        except Exception:
            log.debug("live journal append failed", exc_info=True)

    def _stop_live_heartbeat(self) -> None:
        heartbeat = getattr(self, "_live_heartbeat", None)
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                log.debug("live heartbeat stop failed", exc_info=True)

    def _enqueue_silence_proactive(
        self,
        source: Literal["voice_silence", "typed_silence"],
    ) -> None:
        """Timers wake BrainLoop; they must not call the director themselves."""
        loop = getattr(self, "_brain_loop", None)
        if loop is None:
            return
        from app.core.brain.events import ProactiveEvent

        try:
            loop.enqueue(
                ProactiveEvent(
                    session_key=str(getattr(self, "session_key", "") or ""),
                    source=source,
                ),
            )
        except Exception:
            log.debug("enqueue silence proactive failed", exc_info=True)

    def live_epoch_kind(self, kind: str, *, situation_changed: bool = False) -> str:
        return classify_epoch(kind, situation_changed=situation_changed)

    def _live_pending_cues(self) -> list[Any]:
        store = getattr(self, "_cue_store", None)
        if store is None or not hasattr(store, "pending"):
            return []
        try:
            return list(store.pending(limit=8) or [])
        except Exception:
            log.debug("live cue pending peek failed", exc_info=True)
            return []

    def _live_prepared_nudge(self) -> Any | None:
        store = getattr(self, "_prepared_nudge_store", None)
        if store is None or not hasattr(store, "get_fresh"):
            return None
        try:
            return store.get_fresh(str(getattr(self, "_user_id", "default") or "default"))
        except Exception:
            log.debug("live nudge peek failed", exc_info=True)
            return None

    def _live_inclination_diagnostics(self) -> dict[str, Any]:
        runtime = getattr(self, "_live_inclination", None)
        if runtime is None:
            return {"notices": [], "urges": [], "wait": None, "budget": {}}
        try:
            return runtime.diagnostics()
        except Exception:
            log.debug("live inclination diagnostics failed", exc_info=True)
            return {"notices": [], "urges": [], "wait": None, "budget": {}}

    def _live_concept_view(self) -> Any:
        try:
            from app.core.concepts.concept_view import concept_view_from

            return concept_view_from(self)
        except Exception:
            log.debug("live concept view failed", exc_info=True)
            return None

    def _live_policy_diagnostics(self) -> dict[str, Any]:
        runtime = getattr(self, "_live_policy_context", None)
        extra: dict[str, Any] = {}
        controller = getattr(self, "_live_policy_controller", None)
        if controller is not None:
            try:
                extra.update(controller.diagnostics())
            except Exception:
                log.debug("live policy controller diagnostics failed", exc_info=True)
        if runtime is None:
            return {
                "policy_concepts": [],
                "modifiers": {},
                "policy_ledger": [],
                **extra,
            }
        try:
            return {**runtime.diagnostics(), **extra}
        except Exception:
            log.debug("live policy diagnostics failed", exc_info=True)
            return {
                "policy_concepts": [],
                "modifiers": {},
                "policy_ledger": [],
                **extra,
            }

    def admit_live_user_intent(self) -> bool:
        """Critical user intent always wakes TurnRunner. Live must not go mute."""
        if not self.is_live_presence():
            return True
        controller = getattr(self, "_live_policy_controller", None)
        frame = getattr(
            getattr(self, "_live_situation_assembler", None), "last_frame", None,
        )
        if controller is None or frame is None:
            return True
        try:
            last = getattr(controller, "last_user_intent_admit", None) or {}
            proposal = last.get("proposal") if isinstance(last, dict) else None
            reason = ""
            if isinstance(proposal, dict):
                reason = str(proposal.get("reason_code") or "")
            if last.get("user_intent") and reason == "critical_user_intent":
                return True
            controller._admit_critical(frame)
        except Exception:
            log.debug("live admit reflex failed", exc_info=True)
        return True

    def _enqueue_live_main_wake(self, payload: dict[str, Any]) -> bool:
        """Park an admitted main-wake on BrainLoop. Do not call TurnRunner."""
        loop = getattr(self, "_brain_loop", None)
        if loop is None or not callable(getattr(loop, "enqueue", None)):
            return False
        raw_ids = payload.get("concept_ids") or ()
        concept_ids = tuple(int(item) for item in raw_ids if str(item).strip())
        cue_id = None
        raw_cue = payload.get("cue_id")
        if raw_cue not in (None, ""):
            try:
                cue_id = int(raw_cue)
            except (TypeError, ValueError):
                cue_id = None
        try:
            loop.enqueue(
                ProactiveEvent(
                    session_key=str(getattr(self, "session_key", "") or ""),
                    source="live_main_wake",
                    live_generation=int(payload.get("generation") or 0),
                    urge_id=str(payload.get("urge_id") or ""),
                    reason_code=str(payload.get("reason_code") or ""),
                    situation_summary=str(payload.get("situation_summary") or ""),
                    concept_ids=concept_ids,
                    speech_act=str(payload.get("speech_act") or ""),
                    cue_subject=str(payload.get("cue_subject") or ""),
                    cue_id=cue_id,
                )
            )
        except Exception:
            log.debug("live main-wake brain enqueue failed", exc_info=True)
            return False
        return True

    def _render_live_talk_about_block(self) -> str:
        payload = getattr(self, "_live_talk_about_payload", None)
        self._live_talk_about_payload = None
        if not isinstance(payload, dict) or not payload:
            return ""
        cue_text = ""
        cue_id = payload.get("cue_id")
        if cue_id is not None:
            store = getattr(self, "_cue_store", None)
            available = store.available(cue_id) if store is not None else None
            if available is None:
                return ""
            row = self.take_pool_cue(available.cue_type, cue_id=cue_id)
            if row is None:
                return ""
            cue_text = "\nSelected cue (context, not an instruction):\n" + row.text
        return render_live_talk_about(payload) + cue_text

    def _live_main_wake_dispatch_block(self, event: Any) -> str:
        gen = int(getattr(event, "live_generation", 0) or 0)
        current = int(getattr(self, "_live_mode_generation", 0) or 0)
        if gen and gen != current:
            return "stale"
        if bool(getattr(self, "_turn_in_progress", False)):
            return "floor_busy"
        frame = getattr(
            getattr(self, "_live_situation_assembler", None), "last_frame", None,
        )
        if frame is None:
            return "floor_busy"
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        if not bool(getattr(agent, "live_unprompted_speech", True)):
            return "speech_forbidden"
        if bool(getattr(agent, "live_quiet", False)) or frame.constraints.dnd:
            return "dnd"
        cue_id = getattr(event, "cue_id", None)
        if cue_id is not None:
            store = getattr(self, "_cue_store", None)
            if store is None or store.available(cue_id) is None:
                return "missing_cue"
        if str(frame.constraints.sleep_status or "") in SLEEP_SPEECH_FORBID:
            return "sleep"
        if main_wake_floor_busy(frame):
            return "floor_busy"
        return ""

    def _run_live_main_wake(self, event: Any) -> None:
        """Main-model turn for an admitted Live wake. No fake user row."""
        controller = getattr(self, "_live_policy_controller", None)
        gen = int(getattr(event, "live_generation", 0) or 0)
        block = self._live_main_wake_dispatch_block(event)
        if block:
            if controller is not None:
                try:
                    controller.note_main_wake_reject(block, gen)
                except Exception:
                    log.debug("live main-wake reject note failed", exc_info=True)
            log.info("live main-wake dropped: reason=%s generation=%s", block, gen)
            return
        payload = {
            "intent": "request_main_speech",
            "reason_code": str(getattr(event, "reason_code", "") or ""),
            "urge_id": str(getattr(event, "urge_id", "") or ""),
            "urge_kind": "",
            "situation_summary": str(getattr(event, "situation_summary", "") or ""),
            "concept_ids": tuple(getattr(event, "concept_ids", ()) or ()),
            "speech_act": str(getattr(event, "speech_act", "") or ""),
            "cue_subject": str(getattr(event, "cue_subject", "") or ""),
            "cue_id": getattr(event, "cue_id", None),
        }
        runtime = getattr(self, "_live_inclination", None)
        if runtime is not None:
            try:
                for urge in runtime.urges.active():
                    if urge.urge_id == payload["urge_id"]:
                        payload["urge_kind"] = urge.kind
                        break
            except Exception:
                log.debug("live main-wake urge lookup failed", exc_info=True)
        self._live_talk_about_payload = payload
        remember = bool(getattr(self, "_remember_history", True))
        session_key = (
            self.session_key if remember else f"{self.session_key}:noremember"
        )
        runner = getattr(self, "_turn_runner", None)
        if runner is None:
            self._live_talk_about_payload = None
            return
        self._last_turn_mode = "typed"
        self._active_turn_user_text = ""
        self._active_turn_attachments = []
        snapshot_cues = getattr(self, "_snapshot_armed_cues", None)
        if callable(snapshot_cues):
            try:
                snapshot_cues()
            except Exception:
                log.debug("live main-wake cue snapshot failed", exc_info=True)
        if callable(getattr(self, "_touch_user_activity", None)):
            try:
                self._touch_user_activity()
            except Exception:
                log.debug("live main-wake activity touch failed", exc_info=True)
        self._turn_in_progress = True
        tts_chunk_cb = None
        on_earcon_cb = None
        settings = getattr(self, "_settings", None)
        tts_on = bool(getattr(getattr(settings, "tts", None), "enabled", False))
        if tts_on:
            prosody = getattr(self, "_prosody", None)
            tts = getattr(self, "_tts", None)
            tts_chunk_cb = (
                prosody.dispatch if prosody is not None else getattr(tts, "enqueue", None)
            )
            if tts is not None and hasattr(tts, "enqueue_earcon"):
                on_earcon_cb = tts.enqueue_earcon
        try:
            result = runner.run(
                session_key,
                "",
                on_tts_chunk=tts_chunk_cb,
                on_earcon=on_earcon_cb,
                on_overlay=getattr(self, "_emit_avatar_overlay", None),
                on_outfit=getattr(self, "_emit_avatar_outfit", None),
                on_motion=getattr(self, "_emit_avatar_motion", None),
                on_touch=getattr(self, "_emit_avatar_touch", None),
                allow_empty_user=True,
            )
        except Exception:
            log.exception("live main-wake turn failed")
            self._live_talk_about_payload = None
            if controller is not None:
                try:
                    controller.note_main_wake_silence(gen)
                except Exception:
                    log.debug("live main-wake silence note failed", exc_info=True)
            return
        finally:
            self._turn_in_progress = False
            self._live_talk_about_payload = None
        text = sanitize_assistant_text(
            strip_all_meta_tags(getattr(result, "text", "") or "")
        )
        if not text:
            if controller is not None:
                try:
                    controller.note_main_wake_silence(gen)
                except Exception:
                    log.debug("live main-wake silence note failed", exc_info=True)
            log.info("live main-wake silence: generation=%s", gen)
            return
        self._live_mark_aiko_spoke()
        notify = getattr(self, "_notify_message", None)
        if callable(notify):
            try:
                notify(
                    "Assistant (proactive)",
                    text,
                    getattr(result, "assistant_message_id", None),
                )
            except Exception:
                log.debug("live main-wake notify failed", exc_info=True)
        try:
            self._post_turn_inner_life(
                user_text="",
                reaction=getattr(result, "reaction", "neutral") or "neutral",
                assistant_text=text,
                raw_assistant_text=getattr(result, "raw_text", "") or "",
                user_message_id=None,
                assistant_message_id=getattr(result, "assistant_message_id", None),
                telemetry=getattr(result, "telemetry", None),
            )
        except Exception:
            log.debug("live main-wake post-turn failed", exc_info=True)

    def _maybe_run_live_policy(
        self,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
    ) -> None:
        controller = getattr(self, "_live_policy_controller", None)
        if controller is None:
            return
        try:
            controller.consider(
                frame,
                trigger_kind=trigger_kind,
                prompt_input=self._live_policy_prompt_input(frame),
                user_intent=str(trigger_kind) in USER_INTENT_KINDS,
            )
        except Exception:
            log.debug("live policy consider failed", exc_info=True)

    def _live_policy_prompt_input(self, frame: LiveSituationFrame) -> dict[str, Any]:
        runtime = getattr(self, "_live_inclination", None)
        policy = getattr(self, "_live_policy_context", None)
        bus = getattr(self, "_live_impulse_bus", None)
        journal = getattr(self, "_live_journal", None)
        urges = ()
        if runtime is not None:
            try:
                urges = runtime.urges.active()
            except Exception:
                urges = ()
        impulses = bus.tail_impulses() if bus is not None else ()
        journal_entries = ()
        if journal is not None:
            try:
                journal_entries = journal.tail(str(getattr(self, "session_key", "") or ""))
            except Exception:
                journal_entries = ()
        ledger = ()
        concepts = ()
        if policy is not None:
            try:
                ledger = tuple(policy._ledger)
            except Exception:
                ledger = ()
            concepts = getattr(policy, "last_picks", ()) or ()
        rows: list[Any] = []
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is not None:
            try:
                rows = list(
                    chat_db.get_messages(
                        str(getattr(self, "session_key", "") or ""),
                        limit=TRANSCRIPT_MAX_ROWS,
                    )
                    or []
                )
            except Exception:
                rows = []
        known_urge_ids = menu_urge_ids(urges)
        known_refs = [str(frame.generation)]
        known_refs.extend(f"urge:{item}" for item in known_urge_ids)
        known_refs.extend(
            f"impulse:{item.event_id}" for item in impulses if getattr(item, "event_id", "")
        )
        for pick in concepts:
            cid = getattr(pick, "concept_id", None)
            if cid:
                known_refs.append(f"concept:{cid}")
        return {
            "concepts": concepts,
            "transcript_rows": rows,
            "impulses": impulses,
            "journal_entries": journal_entries,
            "ledger": ledger,
            "urges": urges,
            "diet_tuning": tuning_from_route(self, role=LLM_ROLE_LIVE_POLICY),
            "known_urge_ids": known_urge_ids,
            "known_context_refs": tuple(known_refs),
        }

    def _live_policy_concept_token_budget(self) -> int:
        diet = diet_for("live_policy")
        if diet is None:
            return 96
        try:
            return resolve_budget(
                diet, tuning_from_route(self, role=LLM_ROLE_LIVE_POLICY),
            )
        except Exception:
            return 96

    def _live_policy_prompt_ceiling(self) -> int:
        agent = getattr(getattr(self, "_settings", None), "agent", None)
        try:
            return int(getattr(agent, "live_policy_prompt_max_tokens", 12000) or 12000)
        except (TypeError, ValueError):
            return 12000

    def _live_policy_model(self) -> str:
        resolve = getattr(self, "_route_or_none", None)
        if not callable(resolve):
            return "qwen3.5:4b"
        try:
            route = resolve(LLM_ROLE_LIVE_POLICY)
        except Exception:
            return "qwen3.5:4b"
        if route is None:
            return "qwen3.5:4b"
        return str(route.model or "").strip() or "qwen3.5:4b"

    def _deliver_live_micro_utterance(
        self,
        text: str,
        proposal: Any,
        frame: LiveSituationFrame,
    ) -> bool:
        """Speak a validated Live line. Not a user turn."""
        del proposal, frame
        if not self._live_unprompted_speech_enabled():
            return False
        if bool(getattr(self, "_turn_in_progress", False)):
            return False
        playing = getattr(self, "is_tts_playing", None)
        if callable(playing) and playing():
            return False
        drained = getattr(self, "is_playback_drained", None)
        if callable(drained) and not drained():
            return False
        cleaned = sanitize_assistant_text(strip_all_meta_tags(text))
        if validate_micro_utterance(cleaned) is None:
            return False
        chat_db = getattr(self, "_chat_db", None)
        session_key = str(getattr(self, "session_key", "") or "")
        if chat_db is None or not session_key:
            return False
        message_id = None
        try:
            message_id = chat_db.add_message(
                session_key,
                "assistant",
                cleaned,
                estimate_tokens(cleaned),
                dialogue_act="live_micro",
            )
        except Exception:
            log.debug("live micro persist failed", exc_info=True)
            return False
        notify = getattr(self, "_notify_message", None)
        if callable(notify):
            try:
                notify("Assistant (live)", cleaned, message_id)
            except Exception:
                log.debug("live micro notify failed", exc_info=True)
        speak = getattr(self, "speak_text", None)
        if callable(speak):
            try:
                speak(cleaned)
            except Exception:
                log.debug("live micro speak failed", exc_info=True)
        self._live_mark_aiko_spoke()
        return True

    def _live_policy_record_outcome(
        self,
        *,
        proposed_action: str,
        arbiter_result: str,
        main_model_used: bool = False,
        executed: bool = False,
        action_id: str = "",
        action_state: str = "",
    ) -> None:
        runtime = getattr(self, "_live_policy_context", None)
        if runtime is None:
            return
        runtime.record_outcome(
            proposed_action=proposed_action,
            arbiter_result=arbiter_result,
            main_model_used=main_model_used,
            executed=executed,
            action_id=action_id,
            action_state=action_state,
        )

    def _live_policy_set_loaded(self, loaded: bool) -> None:
        controller = getattr(self, "_live_policy_controller", None)
        if controller is None:
            return
        try:
            if loaded:
                controller.warm()
            else:
                controller.unload()
        except Exception:
            log.debug("live policy load/unload failed", exc_info=True)

    def add_live_embodiment_listener(self, callback: Any) -> None:
        listeners = getattr(self, "_live_embodiment_listeners", None)
        if listeners is None:
            self._live_embodiment_listeners = []
            listeners = self._live_embodiment_listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def live_embodiment_payload(self) -> dict[str, Any] | None:
        plan = getattr(self, "_live_behavior_plan", None)
        if plan is None:
            return None
        frame = getattr(
            getattr(self, "_live_situation_assembler", None), "last_frame", None,
        )
        payload: dict[str, Any] = {"plan": plan.to_payload()}
        if frame is not None:
            payload["commitment"] = frame.commitment.to_payload()
            payload["world_activity"] = frame.aiko.activity
            payload["world_posture"] = frame.aiko.posture
            payload["attention_target"] = frame.attention.target
            payload["sleep_status"] = frame.constraints.sleep_status
        return payload

    def _live_embodiment_diagnostics(self) -> dict[str, Any]:
        plan = getattr(self, "_live_behavior_plan", None)
        caps = self._live_semantic_capabilities()
        return {
            "behavior_plan": plan.to_payload() if plan is not None else None,
            "semantic_capabilities": caps.to_payload(),
        }

    def _live_semantic_capabilities(self):
        payload_fn = getattr(self, "avatar_payload", None)
        profile = payload_fn() if callable(payload_fn) else None
        if not isinstance(profile, dict):
            profile = {}
        return semantic_capabilities_from_profile(profile)

    def _refresh_live_embodiment(self, frame: LiveSituationFrame) -> None:
        resolver = getattr(self, "_live_behavior_resolver", None)
        if resolver is None:
            resolver = LiveBehaviorResolver()
            self._live_behavior_resolver = resolver
        try:
            policy_intent = None
            ttl_ms = None
            held = None
            controller = getattr(self, "_live_policy_controller", None)
            if controller is not None:
                held = controller.accepted_nonverbal_for(int(frame.generation))
                if held:
                    policy_intent = str(held.get("intent") or "") or None
                    raw_ttl = held.get("ttl_ms")
                    if raw_ttl is not None:
                        ttl_ms = int(raw_ttl)
            presence_style = None
            attention_target = None
            reaction_tone = None
            reaction_intensity = None
            keep_attention = False
            if held:
                style = str(held.get("presence_style") or "").strip()
                if style:
                    presence_style = style
                target = str(held.get("attention_target") or "").strip()
                if target:
                    attention_target = target
                tone = str(held.get("reaction_tone") or "").strip()
                if tone:
                    reaction_tone = tone
                band = str(held.get("reaction_intensity") or "").strip()
                if band:
                    reaction_intensity = band
                keep_attention = bool(held.get("keep_attention"))
            plan = resolver.resolve(
                frame,
                self._live_semantic_capabilities(),
                policy_intent=policy_intent,
                ttl_ms=ttl_ms,
                presence_style=presence_style,
                attention_target=attention_target,
                reaction_tone=reaction_tone,
                reaction_intensity=reaction_intensity,
                keep_attention=keep_attention,
            )
        except Exception:
            log.debug("live behavior resolve failed", exc_info=True)
            return
        self._live_behavior_plan = plan
        signature = (
            plan.intent,
            plan.attention_target,
            plan.gaze_class,
            plan.body_class,
            plan.breath_class,
            plan.expression_class,
            plan.reaction_tone,
            plan.reaction_intensity,
            plan.degrade_to_sleep,
            frame.aiko.activity,
            frame.aiko.posture,
        )
        if signature == getattr(self, "_live_embodiment_signature", None):
            return
        self._live_embodiment_signature = signature
        if policy_intent:
            self._live_last_semantic_action_ms = time.monotonic() * 1000.0
        self._notify_live_embodiment()

    def _clear_live_embodiment(self) -> None:
        self._live_behavior_plan = None
        self._live_embodiment_signature = None
        self._notify_live_embodiment()

    def _notify_live_embodiment(self) -> None:
        payload = self.live_embodiment_payload()
        event: dict[str, Any] = {"type": "live_embodiment", "plan": None}
        if payload is not None:
            event = {"type": "live_embodiment", **payload}
        for listener in list(getattr(self, "_live_embodiment_listeners", [])):
            try:
                listener(event)
            except Exception:
                log.debug("live embodiment listener raised", exc_info=True)

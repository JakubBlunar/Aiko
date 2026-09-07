"""Compute-only idle worker that advances Aiko's persisted sleep lifecycle."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from app.core.affect import circadian, vitality, vitality_rhythm
from app.core.infra import timephrase
from app.core.proactive.idle_worker import SLEEP_CONTINUE, WorkSignal
from app.core.world.sleep_state import (
    ASLEEP,
    AWAKE,
    WINDING_DOWN,
    WOKEN,
    PropensityInputs,
    evaluate_propensity,
    should_finish_waking,
)
from app.core.world.sleep_store import SleepGenerationConflict, SleepStore


log = logging.getLogger("app.sleep_lifecycle")


class SleepLifecycleWorker:
    """Own autonomous wind-down, sleep entry, nap expiry, and wake fallback."""

    name = "sleep_lifecycle"
    sleep_policy = SLEEP_CONTINUE

    def __init__(
        self,
        *,
        sleep_store: SleepStore,
        chat_db: Any,
        world_store: Any | None,
        world_guard: Any | None,
        idle_depth_provider: Callable[[], float],
        settings_provider: Callable[[], Any],
        world_notify: Callable[[dict[str, Any]], None] | None = None,
        override_take: Callable[[str, Any], Any] | None = None,
        interval_seconds: float = 60.0,
    ) -> None:
        self._sleep_store = sleep_store
        self._db = chat_db
        self._world = world_store
        self._guard = world_guard
        self._idle_depth_provider = idle_depth_provider
        self._settings_provider = settings_provider
        self._world_notify = world_notify
        self._override_take = override_take
        self._interval = max(15.0, float(interval_seconds))
        self._last_inputs: dict[str, Any] = {}
        self._last_decision: dict[str, Any] = {}

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        del now, last_run_at
        settings = self._settings_provider()
        state = self._sleep_store.get_state()
        return bool(getattr(settings, "sleep_enabled", True)) or state.status != AWAKE

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal:
        state = self._sleep_store.get_state()
        elapsed = (
            None if last_run_at is None else max(0.0, (now - last_run_at).total_seconds())
        )
        if state.status != AWAKE:
            return WorkSignal(1.0, f"advance_{state.status}", needs_llm=False)
        heartbeat = max(1.0, self.interval_seconds)
        pressure = 1.0 if elapsed is None else min(1.0, elapsed / heartbeat)
        return WorkSignal(pressure, "sleep_propensity", needs_llm=False)

    def status(self) -> dict[str, Any]:
        return {
            "inputs": dict(self._last_inputs),
            "decision": dict(self._last_decision),
        }

    def run(self) -> dict[str, Any]:
        now = timephrase.now()
        if self._override_take is not None:
            interruption = self._override_take("sleep_force_interruption", None)
            if interruption is not None:
                payload = interruption if isinstance(interruption, dict) else {}
                state, episode = self._sleep_store.record_interruption(
                    session_id=str(payload.get("session_id") or "mcp-debug"),
                    message_id=None,
                    message_text=str(payload.get("text") or "debug interruption"),
                    now=now,
                )
                return {
                    "status": state.status,
                    "interruption_recorded": episode is not None,
                }
            forced = self._override_take("sleep_force_transition", None)
            if forced is not None:
                payload = forced if isinstance(forced, dict) else {"action": forced}
                current = self._sleep_store.get_state()
                updated = self._sleep_store.transition(
                    str(payload.get("action") or ""),
                    expected_generation=current.generation,
                    now=now,
                    sleep_kind=str(payload.get("sleep_kind") or "overnight"),
                    reason_code="debug_forced",
                    reason_text=str(payload.get("reason_text") or ""),
                    previous_world=self._world_state_payload(),
                    outcome="debug_forced",
                )
                if updated.status == ASLEEP:
                    self._set_sleep_world()
                elif updated.status == WOKEN and self._world is not None:
                    self._set_woken_world()
                elif updated.status == AWAKE:
                    self._clear_napping()
                return {"status": updated.status, "forced_action": payload.get("action")}
        settings = self._settings_provider()
        state = self._sleep_store.get_state()
        # A wind-down needs one final guard check before sleep. Startup
        # reconciliation is handled by SessionController; doing it here would
        # skip that check and the world projection in ``_advance_wind_down``.
        if state.status != WINDING_DOWN:
            prior_status = state.status
            state = self._sleep_store.reconcile(
                now=now,
                wind_down_minutes=float(
                    getattr(settings, "sleep_wind_down_minutes", 5.0)
                ),
                nap_max_hours=float(
                    getattr(settings, "sleep_nap_max_hours", 2.0)
                ),
            )
            if prior_status == ASLEEP and state.status == WOKEN:
                self._set_woken_world()
        if not bool(getattr(settings, "sleep_enabled", True)):
            if state.status == WINDING_DOWN:
                state = self._sleep_store.transition(
                    "cancel", expected_generation=state.generation, now=now,
                )
            elif state.status == ASLEEP:
                state = self._sleep_store.transition(
                    "wake",
                    expected_generation=state.generation,
                    now=now,
                )
                state = self._sleep_store.transition(
                    "fully_awake",
                    expected_generation=state.generation,
                    now=now,
                    outcome="disabled",
                )
                self._clear_napping()
            elif state.status == WOKEN:
                state = self._sleep_store.transition(
                    "fully_awake",
                    expected_generation=state.generation,
                    now=now,
                    outcome="disabled",
                )
                self._clear_napping()
            return {"status": state.status, "reason": "disabled"}

        if state.status == WINDING_DOWN:
            return self._advance_wind_down(state, now, settings)
        if state.status == ASLEEP:
            return {"status": ASLEEP, "reason": "sleeping"}
        if state.status == WOKEN:
            return self._advance_woken(state, now, settings)
        return self._consider_sleep(state, now, settings)

    def _energy_and_baseline(
        self, now: datetime, settings: Any,
    ) -> tuple[float, float, str]:
        rhythm_enabled = bool(
            getattr(settings, "vitality_rhythm_enabled", True)
        )
        baseline, rhythm = vitality_rhythm.current_baseline(
            self._db,
            now,
            enabled=rhythm_enabled,
            exception_chance=float(
                getattr(settings, "vitality_rhythm_exception_chance", 0.3)
            ),
        )
        stored = vitality.deserialize(
            self._db.kv_get(vitality.KV_VITALITY),
            baseline=baseline,
            now=now,
        )
        circadian_state = circadian.compute(now=now)
        return float(stored.energy), float(baseline), str(circadian_state.period)

    def _consider_sleep(self, state: Any, now: datetime, settings: Any) -> dict[str, Any]:
        energy, baseline, period = self._energy_and_baseline(now, settings)
        idle_hours = max(0.0, float(self._idle_depth_provider()) / 3600.0)
        awake_at = timephrase.parse_iso(state.entered_at)
        awake_hours = (
            max(0.0, (now - timephrase.to_aware(awake_at)).total_seconds() / 3600.0)
            if awake_at is not None
            else 0.0
        )
        guard = self._guard.check_autonomous(
            {"location", "posture", "activity"}, now=now,
        ) if self._guard is not None else None
        inputs = PropensityInputs(
            energy=energy,
            circadian_baseline=baseline,
            idle_hours=idle_hours,
            awake_hours=awake_hours,
            circadian_period=period,
            naps_enabled=bool(getattr(settings, "sleep_naps_enabled", True)),
            shared_situation_active=bool(
                guard is not None
                and getattr(guard, "reason", "") == "active_shared_conversation_situation"
            ),
            intentional_world_hold=bool(
                guard is not None and getattr(guard, "reason", "") == "intentional_hold"
            ),
        )
        decision = evaluate_propensity(
            inputs,
            overnight_threshold=float(
                getattr(settings, "sleep_overnight_threshold", 0.58)
            ),
            nap_energy_threshold=float(
                getattr(settings, "sleep_nap_energy_threshold", 0.22)
            ),
            min_idle_hours=float(
                getattr(settings, "sleep_min_idle_minutes", 15.0)
            )
            / 60.0,
            min_awake_hours=float(
                getattr(settings, "sleep_min_awake_hours", 4.0)
            ),
        )
        self._last_inputs = {
            "energy": energy,
            "circadian_baseline": baseline,
            "circadian_period": period,
            "idle_hours": round(idle_hours, 3),
            "awake_hours": round(awake_hours, 3),
        }
        self._last_decision = {
            "should_wind_down": decision.should_wind_down,
            "sleep_kind": decision.sleep_kind,
            "score": decision.score,
            "reason_code": decision.reason_code,
        }
        if not decision.should_wind_down:
            return {"status": AWAKE, **self._last_decision}

        previous = self._world_state_payload()
        try:
            updated = self._sleep_store.transition(
                "wind_down",
                expected_generation=state.generation,
                now=now,
                sleep_kind=decision.sleep_kind,
                reason_code=decision.reason_code,
                previous_world=previous,
            )
        except SleepGenerationConflict:
            return {"status": self._sleep_store.get_state().status, "reason": "race_lost"}
        return {"status": updated.status, **self._last_decision}

    def _advance_wind_down(
        self, state: Any, now: datetime, settings: Any,
    ) -> dict[str, Any]:
        entered = timephrase.parse_iso(state.entered_at) or now
        required = max(
            0.0, float(getattr(settings, "sleep_wind_down_minutes", 5.0)) * 60.0
        )
        elapsed = max(0.0, (now - timephrase.to_aware(entered)).total_seconds())
        if elapsed < required:
            return {
                "status": WINDING_DOWN,
                "seconds_until_sleep": round(required - elapsed, 1),
            }
        if self._guard is not None:
            guard = self._guard.check_autonomous(
                {"location", "posture", "activity"},
                now=now,
            )
            if not bool(getattr(guard, "allowed", False)):
                updated = self._sleep_store.transition(
                    "cancel",
                    expected_generation=state.generation,
                    now=now,
                    outcome=str(getattr(guard, "reason", "world_guard")),
                )
                return {
                    "status": updated.status,
                    "reason": getattr(guard, "reason", "world_guard"),
                }
        try:
            updated = self._sleep_store.transition(
                "fall_asleep",
                expected_generation=state.generation,
                now=now,
            )
        except SleepGenerationConflict:
            return {"status": self._sleep_store.get_state().status, "reason": "race_lost"}
        self._set_sleep_world()
        return {"status": updated.status, "episode_id": updated.current_episode_id}

    def _advance_woken(
        self, state: Any, now: datetime, settings: Any,
    ) -> dict[str, Any]:
        energy, _baseline, _period = self._energy_and_baseline(now, settings)
        entered = timephrase.parse_iso(state.entered_at) or now
        minutes = max(0.0, (now - timephrase.to_aware(entered)).total_seconds() / 60.0)
        episode = self._sleep_store.current_episode()
        interruptions = len(episode.interruptions) if episode is not None else 0
        if should_finish_waking(
            energy=energy,
            woken_minutes=minutes,
            engaged_turns=max(0, interruptions - 1),
            energy_threshold=float(
                getattr(settings, "sleep_wake_energy_threshold", 0.48)
            ),
            max_woken_minutes=float(
                getattr(settings, "sleep_max_woken_minutes", 90.0)
            ),
        ):
            updated = self._sleep_store.transition(
                "fully_awake",
                expected_generation=state.generation,
                now=now,
                outcome="gradual_wake",
            )
            self._clear_napping()
            return {"status": updated.status, "reason": "recovered"}

        back_minutes = float(
            getattr(settings, "sleep_back_to_sleep_minutes", 20.0)
        )
        if (
            minutes >= back_minutes
            and float(self._idle_depth_provider()) >= back_minutes * 60.0
            and energy < float(getattr(settings, "sleep_wake_energy_threshold", 0.48))
        ):
            updated = self._sleep_store.transition(
                "back_to_sleep",
                expected_generation=state.generation,
                now=now,
            )
            self._set_sleep_world()
            return {"status": updated.status, "reason": "settled_back"}
        return {"status": WOKEN, "energy": energy, "woken_minutes": round(minutes, 1)}

    def _world_state_payload(self) -> dict[str, Any]:
        if self._world is None:
            return {}
        try:
            state = self._world.get_state()
            location = self._world.get_location_by_id(state.location_id)
            return {
                **state.to_dict(),
                "location_slug": getattr(location, "slug", None),
            }
        except Exception:
            return {}

    def _set_sleep_world(self) -> None:
        if self._world is None:
            return
        try:
            bed = self._world.get_location("bed")
            state = self._world.set_state(
                location_id=getattr(bed, "id", None) if bed is not None else ...,
                posture="lying",
                activity="napping",
            )
            if self._world_notify is not None:
                self._world_notify({"state": state.to_dict()})
        except Exception:
            log.debug("sleep world update failed", exc_info=True)

    def _clear_napping(self) -> None:
        if self._world is None:
            return
        try:
            current = self._world.get_state()
            if current.activity != "napping":
                return
            state = self._world.set_state(activity="idle")
            if self._world_notify is not None:
                self._world_notify({"state": state.to_dict()})
        except Exception:
            log.debug("wake world update failed", exc_info=True)

    def _set_woken_world(self) -> None:
        if self._world is None:
            return
        try:
            state = self._world.set_state(posture="lying", activity="waking_up")
            if self._world_notify is not None:
                self._world_notify({"state": state.to_dict()})
        except Exception:
            log.debug("woken world update failed", exc_info=True)


__all__ = ["SleepLifecycleWorker"]

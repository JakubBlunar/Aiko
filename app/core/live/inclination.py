"""Compose notices, cues, budgets, and wait onto a Live situation frame."""
from __future__ import annotations

import time
from dataclasses import replace

from app.core.live.budget import LiveBehaviorBudget
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.frame import LiveSituationFrame, SLEEP_SPEECH_FORBID
from app.core.live.main_wake import admit_main_wake, main_wake_floor_busy
from app.core.live.notice import notices_from_trigger
from app.core.live.urge_store import LiveUrgeStore
from app.core.live.wait import LiveWaitScheduler


class LiveInclinationRuntime:
    """Phase 3 inclination layer. Never grants speech or fulfilment."""

    def __init__(
        self,
        *,
        cue_adapter: CueUrgeAdapter | None = None,
    ) -> None:
        self.urges = LiveUrgeStore()
        self.budget = LiveBehaviorBudget()
        self.wait = LiveWaitScheduler()
        self.cue_adapter = cue_adapter or CueUrgeAdapter()
        self._previous_frame: LiveSituationFrame | None = None

    def apply(
        self,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
        now_mono_ms: float,
        live_quiet: bool = False,
    ) -> LiveSituationFrame:
        self.urges.expire_due(now_mono_ms)
        self.wait.on_generation(frame.generation, trigger_kind)
        suppress = live_quiet or (
            frame.constraints.sleep_status in SLEEP_SPEECH_FORBID
        )
        if suppress:
            self.urges.withdraw_expressive(reason="constraint")
        if self.wait.should_suppress_inference(
            trigger_kind,
            now_mono_ms=now_mono_ms,
            generation=frame.generation,
        ):
            return self._stamp(frame, now_mono_ms=now_mono_ms, selected="")

        notices = notices_from_trigger(
            trigger_kind, frame, previous=self._previous_frame,
        )
        self.urges.ingest_notices(
            notices,
            now_mono_ms=now_mono_ms,
            suppress_expressive=suppress,
        )
        if not suppress:
            previous = self._previous_frame
            new_opening = not main_wake_floor_busy(frame) and (
                trigger_kind in {"user.typing_stopped", "aiko.playback_drained"}
                and previous is not None
                and (
                    main_wake_floor_busy(previous)
                    or previous.interaction.playback_active
                )
                or any(notice.kind == "focus_boundary" for notice in notices)
            )
            self.cue_adapter.project(
                self.urges,
                now_mono_ms=now_mono_ms,
                suppress_expressive=suppress,
                reconsider=new_opening and not frame.constraints.dnd,
            )

        budget = self.budget.snapshot(now_mono_ms)
        selected = ""
        if budget.exhausted:
            wait_urge = self.urges.propose(
                kind="wait",
                subject="budget",
                source="budget",
                source_ids=tuple(f"budget:{name}" for name in budget.exhausted),
                repetition_key="budget:" + ",".join(budget.exhausted),
                now_mono_ms=now_mono_ms,
                ttl_ms=max(1_000, budget.next_replenish_ms),
            )
            selected = wait_urge.urge_id if wait_urge is not None else ""
            self.wait.schedule(
                generation=frame.generation,
                urge_id=selected,
                reconsider_after_ms=max(1_000, budget.next_replenish_ms or 15_000),
                now_mono_ms=now_mono_ms,
                wake_on=(
                    "user.message_sent",
                    "user.speech_final",
                    "user.voice_start",
                ),
                reason_code="budget_exhausted",
            )
        elif live_quiet:
            self.wait.schedule(
                generation=frame.generation,
                urge_id="",
                reconsider_after_ms=30_000,
                now_mono_ms=now_mono_ms,
                wake_on=("user.message_sent", "user.speech_final"),
                reason_code="live_quiet",
            )

        stamped = self._stamp(frame, now_mono_ms=now_mono_ms, selected=selected)
        self._previous_frame = stamped
        return stamped

    def eligible_policy_urges(
        self, frame: LiveSituationFrame, *, menu_ids: set[str], now_mono_ms: float,
        unprompted_speech: bool, user_intent: bool,
    ) -> tuple[str, ...]:
        remaining = self.budget.remaining("main_wake", now_mono_ms=now_mono_ms)
        return tuple(
            urge.urge_id for urge in self.urges.active()
            if urge.cue_id is not None and urge.urge_id in menu_ids
            and not admit_main_wake(
                intent="request_main_speech", user_intent=user_intent,
                selected_urge_id=urge.urge_id, urges=(urge,), frame=frame,
                decided_generation=None, now_mono_ms=now_mono_ms,
                budget_remaining=remaining, unprompted_speech=unprompted_speech,
            )
        )

    def _stamp(
        self,
        frame: LiveSituationFrame,
        *,
        now_mono_ms: float,
        selected: str,
    ) -> LiveSituationFrame:
        active = self.urges.active()
        ttl = 0
        if active:
            ttl = max(
                0,
                max(
                    int(
                        urge.expires_after_ms
                        - (now_mono_ms - urge.created_monotonic_ms)
                    )
                    for urge in active
                ),
            )
        budget = self.budget.snapshot(now_mono_ms)
        wait = self.wait.current
        next_ms = 0
        if wait is not None:
            next_ms = max(0, int(wait.deadline_monotonic_ms - now_mono_ms))
        elif budget.next_replenish_ms:
            next_ms = budget.next_replenish_ms
        else:
            deadlines = [
                urge.next_reconsideration_ms for urge in self.urges.all_urges()
                if urge.state == "deferred" and urge.next_reconsideration_ms > 0
            ]
            if deadlines:
                next_ms = max(0, int(min(deadlines) - now_mono_ms))
        aiko = replace(
            frame.aiko,
            candidate_urges=tuple(urge.urge_id for urge in active),
            selected_urge=selected,
            cooldowns=dict(budget.remaining),
        )
        temporal = replace(
            frame.temporal,
            urge_ttl_ms=ttl,
            cooldown_remaining_ms=budget.next_replenish_ms,
            next_reconsideration_ms=next_ms,
        )
        continuity = replace(
            frame.continuity,
            cue_pressure=str(sum(1 for urge in active if urge.cue_id)),
        )
        stamped = replace(
            frame, aiko=aiko, temporal=temporal, continuity=continuity,
        )
        return stamped

    def diagnostics(self) -> dict[str, object]:
        wait = self.wait.current
        now_ms = time.monotonic() * 1000.0
        return {
            "notices": [item.to_payload() for item in self.urges.last_notices()],
            "urges": [item.to_payload() for item in self.urges.all_urges()[-12:]],
            "wait": wait.to_payload() if wait is not None else None,
            "budget": self.budget.snapshot(now_ms).to_payload(),
        }

"""Assemble one LiveSituationFrame from typed snapshots and impulses.

Callers inject clocks so identical evidence produces identical frames.
Concepts and ritual priors may appear on Continuity; they never mint
present-tense situation evidence on their own.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Sequence

from app.core.activity.evidence import (
    ActivityEvidence,
    classify_app,
)
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
)
from app.core.infra import timephrase
from app.core.live.epochs import EpochKind, classify_epoch
from app.core.live.frame import (
    AikoNow,
    AttentionSection,
    BehaviorCommitment,
    ConstraintSection,
    ContinuitySection,
    InferredField,
    InteractionNow,
    LiveEpochRecord,
    LiveSituationFrame,
    SharedSituation,
    SLEEP_SPEECH_FORBID,
    TemporalState,
)
from app.core.live.impulse import LiveImpulse


HYSTERESIS_MARGIN = 0.2
ATTENTION_ENTER_THRESHOLD = 0.5
ATTENTION_EXIT_THRESHOLD = 0.35
ATTENTION_MIN_HOLD_MS = 800
SHARED_HOLD_MS = 15_000
WORLD_HOLD_MS = 45_000
SPEECH_HOLD_MS = 8_000
TYPING_HOLD_MS = 4_000
COMMITMENT_WAKE_ON = (
    "user.typing_started",
    "user.speech_started",
    "world.activity_changed",
)
PHASE_IMMEDIATE_MS = 3_000
PHASE_RECENT_MS = 30_000
PHASE_SETTLED_MS = 300_000

_ANIME_TOKENS = ("anime", "episode", "watch")


@dataclass(frozen=True, slots=True)
class LiveAssembleInput:
    snapshot: ConversationSituationSnapshot
    impulses: tuple[LiveImpulse, ...]
    now: datetime
    monotonic_ms: float
    live_quiet: bool = False
    capture_available: bool = False
    turn_in_progress: bool = False
    live_voice_session_active: bool = False
    mode_generation: int = 0
    last_user_intent_ms: float | None = None
    last_aiko_spoke_ms: float | None = None
    last_semantic_action_ms: float | None = None
    ritual_priors: tuple[str, ...] = ()
    trigger_kind: str = "heartbeat"
    tts_active: bool = False
    activity_evidence: ActivityEvidence | None = None


def _elapsed_ms(now_ms: float, then_ms: float | None) -> int:
    if then_ms is None:
        return 0
    return max(0, int(now_ms - then_ms))


def _phase_label(since_intent_ms: int) -> str:
    if since_intent_ms <= PHASE_IMMEDIATE_MS:
        return "immediate"
    if since_intent_ms <= PHASE_RECENT_MS:
        return "recent"
    if since_intent_ms <= PHASE_SETTLED_MS:
        return "settled"
    return "prolonged"


def _latest(impulses: Sequence[LiveImpulse], kinds: Iterable[str]) -> LiveImpulse | None:
    wanted = set(kinds)
    found: LiveImpulse | None = None
    for item in impulses:
        if item.kind in wanted:
            found = item
    return found


def _iso(now: datetime) -> str:
    stamp = now.isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z") if stamp.endswith("+00:00") else stamp


def _app_kind(app: str) -> str:
    return classify_app(app)


def _looks_like_anime(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return any(token in lowered for token in _ANIME_TOKENS)


def _sleep_status(snapshot: ConversationSituationSnapshot) -> str:
    raw = snapshot.sleep if isinstance(snapshot.sleep, dict) else {}
    status = str(raw.get("status") or "awake").strip().lower()
    return status or "awake"


def _typing_active(impulses: Sequence[LiveImpulse], now_ms: float) -> bool:
    started = _latest(impulses, ("user.typing_started",))
    stopped = _latest(impulses, ("user.typing_stopped",))
    if started is None:
        return False
    if stopped is not None and stopped.sequence >= started.sequence:
        return False
    if started.is_expired(now_ms):
        return False
    return (now_ms - started.monotonic_ms) <= TYPING_HOLD_MS


def _speech_active(
    impulses: Sequence[LiveImpulse],
    *,
    now_ms: float,
    voice_session: bool,
) -> tuple[bool, str]:
    final = _latest(impulses, ("user.speech_final",))
    if final is not None and (now_ms - final.monotonic_ms) <= SPEECH_HOLD_MS:
        return True, "final"
    start = _latest(impulses, ("user.voice_start",))
    stop = _latest(impulses, ("user.voice_stop", "user.stop"))
    if start is None:
        return False, ""
    if stop is not None and stop.sequence >= start.sequence:
        return False, ""
    if voice_session:
        return True, "listening"
    return False, ""


def _last_user_meaning(impulses: Sequence[LiveImpulse]) -> str:
    meaning = _latest(impulses, ("user.message_sent", "user.speech_final"))
    if meaning is None:
        return ""
    return str(meaning.payload.get("text") or "").strip()


def _floor_owner(
    *,
    typing: bool,
    speech: bool,
    turn_in_progress: bool,
) -> str:
    user_floor = typing or speech
    if user_floor and turn_in_progress:
        return "transition"
    if user_floor:
        return "user"
    if turn_in_progress:
        return "aiko"
    return "neither"


def _observation_confidence(
    base: float,
    activity_evidence: ActivityEvidence | None,
) -> float:
    if activity_evidence is None or not activity_evidence.present:
        return base
    if activity_evidence.stale:
        return 0.0
    if activity_evidence.confidence <= 0.0:
        return base
    return min(base, float(activity_evidence.confidence))


def _build_hypothesis(
    snapshot: ConversationSituationSnapshot,
    *,
    now: datetime,
    ritual_priors: Sequence[str],
    evidence: ActivityEvidence | None = None,
) -> tuple[InferredField, str]:
    """World-truth: observations beat dialogue; priors cannot mint present tense."""
    app = str(snapshot.user_active_app or "")
    app_kind = _app_kind(app)
    inferred = snapshot.inferred
    activity = evidence
    missing: list[str] = []
    if not app:
        missing.append("c6_app")
    os_idle = (
        str(activity.os_idle or "missing")
        if activity is not None and activity.present
        else "missing"
    )
    if os_idle == "missing" or (
        activity is not None and activity.present and activity.stale
    ):
        missing.append("os_idle")
    if activity is not None and activity.present and activity.stale:
        missing.append("c6")

    if app_kind == "coding":
        label = "user_coding"
        sharing = "user_only"
        confidence = _observation_confidence(0.7, activity)
        evidence_ids = ("observation:app",)
        conflicts: tuple[str, ...] = ()
        if any(_looks_like_anime(prior) for prior in ritual_priors):
            conflicts = ("prior_cannot_override_observation",)
        return (
            InferredField(
                label=label,
                confidence=confidence,
                evidence_ids=evidence_ids,
                last_confirmed_at=_iso(now),
                conflicts=conflicts,
                missing=tuple(missing),
            ),
            sharing,
        )

    if (
        inferred is not None
        and inferred.shared
        and inferred.shared_activity
        and snapshot.shared_commitment_active
        and not snapshot.inferred_stale
    ):
        if app_kind == "coding" and _looks_like_anime(inferred.shared_activity):
            return (
                InferredField(
                    label="user_coding",
                    confidence=0.7,
                    evidence_ids=("observation:app",),
                    last_confirmed_at=_iso(now),
                    conflicts=("stale_or_unfounded_shared_activity",),
                    missing=tuple(missing),
                ),
                "user_only",
            )
        evidence = tuple(
            f"message:{mid}" for mid in inferred.evidence_message_ids[:8]
        ) or ("situation:inferred",)
        return (
            InferredField(
                label=inferred.shared_activity,
                confidence=0.8 if snapshot.world_compatible else 0.35,
                evidence_ids=evidence,
                last_confirmed_at=inferred.updated_at,
                missing=tuple(missing),
            ),
            "shared",
        )

    if app_kind == "media":
        label = "user_watching_media"
        if any(_looks_like_anime(prior) for prior in ritual_priors):
            # Prior may disambiguate a media observation; it still cannot
            # raise sharing to 'shared' without conversational evidence.
            label = "user_watching_media"
        return (
            InferredField(
                label=label,
                confidence=0.55,
                evidence_ids=("observation:app",),
                last_confirmed_at=_iso(now),
                missing=tuple(missing),
            ),
            "user_only",
        )

    if app:
        return (
            InferredField(
                label="user_using_app",
                confidence=0.45,
                evidence_ids=("observation:app",),
                last_confirmed_at=_iso(now),
                missing=tuple(missing),
            ),
            "user_only",
        )

    world_activity = str(snapshot.world.activity or "").strip().lower()
    if world_activity and world_activity not in {"", "idle", "relaxing"}:
        return (
            InferredField(
                label=snapshot.world.activity,
                confidence=0.4,
                evidence_ids=("observation:world",),
                last_confirmed_at=snapshot.world.updated_at or _iso(now),
                missing=tuple(missing),
            ),
            "aiko_only",
        )

    # Ritual priors alone: display on continuity, no present-tense label.
    return (
        InferredField(
            label="",
            confidence=0.0,
            evidence_ids=(),
            last_confirmed_at="",
            missing=tuple(missing),
        ),
        "unknown",
    )


def _candidate_attention(
    *,
    sleep_status: str,
    typing: bool,
    speech: bool,
    shared_commitment: bool,
    world_activity: str,
    inferred_label: str,
    inferred_confidence: float,
) -> tuple[str, str, str, float]:
    if sleep_status in SLEEP_SPEECH_FORBID:
        return "none", "resting", "sleep", 1.0
    if typing:
        return "user", "engaged", "user_typing", 0.95
    if speech:
        return "user", "engaged", "user_speech", 0.95
    if shared_commitment and inferred_label:
        return "shared_activity", "engaged", "shared_activity", max(
            0.5, inferred_confidence,
        )
    activity = (world_activity or "").strip().lower()
    if activity and activity not in {"", "idle", "relaxing"}:
        return "world_entity", "monitoring", "world_activity", 0.55
    return "none", "casual", "none", 0.2


def _hold_ms_for(target: str) -> int:
    if target == "shared_activity":
        return SHARED_HOLD_MS
    if target == "world_entity":
        return WORLD_HOLD_MS
    return ATTENTION_MIN_HOLD_MS


def _thresholds(candidate: AttentionSection) -> AttentionSection:
    return replace(
        candidate,
        enter_threshold=ATTENTION_ENTER_THRESHOLD,
        exit_threshold=ATTENTION_EXIT_THRESHOLD,
        hysteresis_margin=HYSTERESIS_MARGIN,
    )


def _enter_attention(candidate: AttentionSection, now_ms: float) -> AttentionSection:
    return replace(
        _thresholds(candidate),
        entered_at_monotonic_ms=now_ms,
        last_meaningful_change_ms=now_ms,
        held_until_monotonic_ms=now_ms + _hold_ms_for(candidate.target),
        challenger_target="",
        dwell_ms=0,
    )


def _target_disappeared(
    previous: AttentionSection,
    candidate: AttentionSection,
) -> bool:
    if previous.target == "shared_activity" and candidate.target != "shared_activity":
        return True
    if previous.target == "world_entity" and candidate.target in {"", "none"}:
        return True
    return False


def _apply_hysteresis(
    candidate: AttentionSection,
    previous: AttentionSection | None,
    *,
    now_ms: float,
    critical: bool,
) -> AttentionSection:
    """Schmitt-trigger hold: separate enter/exit, min dwell, switch margin."""
    candidate = _thresholds(candidate)
    if previous is None or previous.target in {"", "none"}:
        if (
            not critical
            and candidate.target not in {"", "none"}
            and candidate.evidence_confidence < ATTENTION_ENTER_THRESHOLD
        ):
            return _enter_attention(
                replace(
                    candidate,
                    target="none",
                    mode="casual",
                    reason_code="below_enter",
                    target_id="",
                    evidence_confidence=candidate.evidence_confidence,
                ),
                now_ms,
            )
        return _enter_attention(candidate, now_ms)
    same = candidate.target == previous.target
    if same:
        if (
            not critical
            and candidate.evidence_confidence < previous.exit_threshold
        ):
            return _enter_attention(
                replace(
                    candidate,
                    target="none",
                    mode="casual",
                    reason_code="below_exit",
                    target_id="",
                ),
                now_ms,
            )
        dwell = _elapsed_ms(now_ms, previous.entered_at_monotonic_ms)
        refresh_hold = candidate.evidence_confidence >= previous.enter_threshold
        held_until = previous.held_until_monotonic_ms
        if refresh_hold:
            held_until = max(
                held_until,
                now_ms + _hold_ms_for(previous.target),
            )
        return replace(
            previous,
            mode=candidate.mode,
            evidence_confidence=candidate.evidence_confidence,
            reason_code=candidate.reason_code,
            target_id=candidate.target_id,
            dwell_ms=dwell,
            last_meaningful_change_ms=previous.last_meaningful_change_ms,
            held_until_monotonic_ms=held_until,
            challenger_target="",
            enter_threshold=previous.enter_threshold,
            exit_threshold=previous.exit_threshold,
            hysteresis_margin=previous.hysteresis_margin,
        )
    if critical or _target_disappeared(previous, candidate):
        return _enter_attention(candidate, now_ms)
    hold_remaining = max(0, int(previous.held_until_monotonic_ms - now_ms))
    dwell = _elapsed_ms(now_ms, previous.entered_at_monotonic_ms)
    if hold_remaining > 0:
        return replace(
            previous,
            challenger_target=candidate.target,
            dwell_ms=dwell,
        )
    if candidate.evidence_confidence < ATTENTION_ENTER_THRESHOLD:
        return replace(
            previous,
            challenger_target=candidate.target,
            dwell_ms=dwell,
        )
    margin = previous.evidence_confidence + previous.hysteresis_margin
    if candidate.evidence_confidence < margin:
        return replace(
            previous,
            challenger_target=candidate.target,
            dwell_ms=dwell,
        )
    return _enter_attention(candidate, now_ms)


def _commitment_from_attention(
    attention: AttentionSection,
    *,
    sleep_status: str,
) -> BehaviorCommitment:
    if sleep_status in SLEEP_SPEECH_FORBID:
        intention = "remain_present"
        target = "none"
    elif attention.target == "user":
        intention = "attend"
        target = "user"
    elif attention.target in {"shared_activity", "world_entity"}:
        intention = "remain_present"
        target = attention.target
    else:
        intention = "wait"
        target = attention.target or "none"
    return BehaviorCommitment(
        intention=intention,
        target=target,
        entered_at_monotonic_ms=attention.entered_at_monotonic_ms,
        minimum_hold_until_ms=attention.held_until_monotonic_ms,
        evidence_confidence=attention.evidence_confidence,
        enter_threshold=attention.enter_threshold,
        exit_threshold=attention.exit_threshold,
        switch_margin=attention.hysteresis_margin,
        wake_on=COMMITMENT_WAKE_ON,
    )


def _speech_forbid(
    *,
    sleep_status: str,
    live_quiet: bool,
    floor_owner: str,
    turn_in_progress: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if sleep_status in SLEEP_SPEECH_FORBID:
        reasons.append(f"sleep_{sleep_status}")
    if live_quiet:
        reasons.append("live_quiet")
    if floor_owner in {"user", "transition"}:
        reasons.append("user_floor")
    if turn_in_progress:
        reasons.append("turn_in_progress")
    return tuple(reasons)


def assemble_live_situation(
    inp: LiveAssembleInput,
    *,
    generation: int,
    previous_attention: AttentionSection | None = None,
    epoch_kind: EpochKind | None = None,
) -> LiveSituationFrame:
    snapshot = inp.snapshot
    now_ms = float(inp.monotonic_ms)
    sleep_status = _sleep_status(snapshot)
    typing = _typing_active(inp.impulses, now_ms)
    speech, endpointing = _speech_active(
        inp.impulses,
        now_ms=now_ms,
        voice_session=inp.live_voice_session_active,
    )
    last_meaning = _last_user_meaning(inp.impulses)
    floor = _floor_owner(
        typing=typing,
        speech=speech,
        turn_in_progress=inp.turn_in_progress,
    )
    intent_ms = inp.last_user_intent_ms
    if typing or speech or last_meaning:
        latest = _latest(
            inp.impulses,
            (
                "user.typing_started",
                "user.message_sent",
                "user.speech_final",
                "user.voice_start",
            ),
        )
        if latest is not None:
            intent_ms = latest.monotonic_ms
    since_intent = _elapsed_ms(now_ms, intent_ms)
    speech_impulse = _latest(
        inp.impulses, ("user.speech_final", "user.voice_start"),
    )
    since_speech = _elapsed_ms(
        now_ms,
        None if speech_impulse is None else speech_impulse.monotonic_ms,
    )
    evidence = inp.activity_evidence
    inferred_field, sharing = _build_hypothesis(
        snapshot,
        now=inp.now,
        ritual_priors=inp.ritual_priors,
        evidence=evidence,
    )
    target, mode, reason, confidence = _candidate_attention(
        sleep_status=sleep_status,
        typing=typing,
        speech=speech,
        shared_commitment=snapshot.shared_commitment_active,
        world_activity=snapshot.world.activity,
        inferred_label=inferred_field.label,
        inferred_confidence=inferred_field.confidence,
    )
    target_id = ""
    if target == "shared_activity" and inferred_field.label:
        target_id = f"activity:{inferred_field.label}"
    elif target == "world_entity":
        target_id = f"world:{snapshot.world.activity or snapshot.world.location_slug}"
    candidate = AttentionSection(
        target=target,
        target_id=target_id,
        mode=mode,
        evidence_confidence=confidence,
        reason_code=reason,
        hysteresis_margin=HYSTERESIS_MARGIN,
    )
    attention = _apply_hysteresis(
        candidate,
        previous_attention,
        now_ms=now_ms,
        critical=typing or speech or sleep_status in SLEEP_SPEECH_FORBID,
    )
    stale: list[str] = []
    if snapshot.inferred_stale:
        stale.append("conversation_situation")
    if evidence is not None and evidence.present and evidence.stale:
        stale.append("c6")
        stale.append("os_idle")
    elif evidence is None or not evidence.present:
        if "os_idle" in inferred_field.missing:
            stale.append("os_idle")
    os_idle = "missing"
    duration_s = 0
    session_count = 0
    idle_span_s = 0
    lock_span_s = 0
    if evidence is not None and evidence.present:
        os_idle = str(evidence.os_idle or "missing")
        duration_s = int(evidence.duration_seconds or 0)
        session_count = int(evidence.session_count or 0)
        idle_span_s = int(evidence.idle_span_seconds or 0)
        lock_span_s = int(evidence.lock_span_seconds or 0)
    forbid = _speech_forbid(
        sleep_status=sleep_status,
        live_quiet=inp.live_quiet,
        floor_owner=floor,
        turn_in_progress=inp.turn_in_progress,
    )
    classified = epoch_kind or classify_epoch(inp.trigger_kind)
    situation_age_ms = int(max(0.0, snapshot.since_user_activity_ms))
    typing_started = _latest(inp.impulses, ("user.typing_started",))
    typing_active_ms = (
        _elapsed_ms(now_ms, typing_started.monotonic_ms)
        if typing and typing_started is not None
        else 0
    )
    frame = LiveSituationFrame(
        generation=int(generation),
        observed_at=_iso(inp.now),
        monotonic_ms=now_ms,
        session_id=str(snapshot.session_id),
        mode_generation=int(inp.mode_generation),
        interaction=InteractionNow(
            floor_owner=floor,
            typing_active=typing,
            speech_active=speech,
            endpointing=endpointing,
            last_user_meaning=last_meaning,
            turn_active=inp.turn_in_progress,
            tts_active=inp.tts_active,
            playback_active=False,
            capture_available=bool(inp.capture_available),
            voice_session_active=inp.live_voice_session_active,
            connection_generation=int(inp.mode_generation),
            silence_since_user_intent_ms=since_intent,
            silence_since_aiko_speech_ms=_elapsed_ms(
                now_ms, inp.last_aiko_spoke_ms,
            ),
            silence_since_shared_activity_ms=situation_age_ms,
        ),
        shared=SharedSituation(
            user_active_app=snapshot.user_active_app,
            os_idle=os_idle,
            session_duration_s=duration_s,
            session_count=session_count,
            idle_span_s=idle_span_s,
            lock_span_s=lock_span_s,
            inferred=inferred_field,
            sharing=sharing,
            scene_name=snapshot.world.scene_name,
            location_slug=snapshot.world.location_slug,
            location_name=snapshot.world.location_name,
            world_activity=snapshot.world.activity,
            world_posture=snapshot.world.posture,
            inferred_stale=snapshot.inferred_stale,
            world_compatible=snapshot.world_compatible,
            conflict_reason=snapshot.conflict_reason,
            shared_commitment_active=snapshot.shared_commitment_active,
        ),
        attention=attention,
        commitment=_commitment_from_attention(
            attention, sleep_status=sleep_status,
        ),
        aiko=AikoNow(
            mood_label=snapshot.mood_label,
            vitality_band=snapshot.vitality_band,
            posture=snapshot.world.posture,
            activity=snapshot.world.activity,
        ),
        continuity=ContinuitySection(
            arc=snapshot.arc,
            arc_confidence=float(snapshot.arc_confidence or 0.0),
            situation_concepts=tuple(
                prior for prior in inp.ritual_priors if prior
            ),
        ),
        temporal=TemporalState(
            typing_active_ms=typing_active_ms,
            typing_idle_ms=0 if typing else since_intent,
            user_speech_active_ms=since_speech if speech else 0,
            since_user_speech_ms=since_speech,
            since_user_intent_ms=since_intent,
            since_aiko_spoke_ms=_elapsed_ms(now_ms, inp.last_aiko_spoke_ms),
            since_semantic_action_ms=_elapsed_ms(
                now_ms, inp.last_semantic_action_ms,
            ),
            situation_age_ms=situation_age_ms,
            since_confirming_evidence_ms=situation_age_ms,
            attention_dwell_ms=int(attention.dwell_ms),
            min_hold_remaining_ms=max(
                0, int(attention.held_until_monotonic_ms - now_ms),
            ),
            phase=_phase_label(since_intent),
        ),
        constraints=ConstraintSection(
            allowed_actions=(),
            speech_forbid_reasons=forbid,
            dnd=bool(inp.live_quiet),
            privacy="local_state",
            stale_sources=tuple(dict.fromkeys(stale)),
            capture_available=bool(inp.capture_available),
            sleep_status=sleep_status,
        ),
        epoch=LiveEpochRecord(
            kind=classified,
            reason=str(inp.trigger_kind or "heartbeat"),
            frame_generation=int(generation),
            at=_iso(inp.now),
        ),
    )
    return frame


class LiveSituationAssembler:
    """Stateful generation + hysteresis wrapper around assemble_live_situation."""

    def __init__(self) -> None:
        self._generation = 0
        self._previous_attention: AttentionSection | None = None
        self._last_frame: LiveSituationFrame | None = None
        self._last_sleep_status = ""
        self._last_shared = False

    @property
    def last_frame(self) -> LiveSituationFrame | None:
        return self._last_frame

    @property
    def generation(self) -> int:
        return self._generation

    def situation_changed(self, inp: LiveAssembleInput) -> bool:
        sleep_status = _sleep_status(inp.snapshot)
        shared = bool(inp.snapshot.shared_commitment_active)
        changed = (
            bool(self._last_sleep_status)
            and sleep_status != self._last_sleep_status
        ) or (
            self._last_frame is not None and shared != self._last_shared
        )
        return changed

    def assemble(self, inp: LiveAssembleInput) -> LiveSituationFrame:
        changed = self.situation_changed(inp)
        kind = classify_epoch(inp.trigger_kind, situation_changed=changed)
        if kind != "data_only":
            self._generation += 1
        elif self._generation == 0:
            self._generation = 1
        now = inp.now if inp.now.tzinfo is not None else timephrase.utcnow()
        payload = inp if inp.now is now else replace(inp, now=now)
        frame = assemble_live_situation(
            payload,
            generation=self._generation,
            previous_attention=self._previous_attention,
            epoch_kind=kind,
        )
        self._previous_attention = frame.attention
        self._last_frame = frame
        self._last_sleep_status = frame.constraints.sleep_status
        self._last_shared = frame.shared.shared_commitment_active
        return frame

    def remember(self, frame: LiveSituationFrame) -> None:
        """Keep an inclination-stamped frame as the inspectable last view."""
        self._last_frame = frame
        self._previous_attention = frame.attention

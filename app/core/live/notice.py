"""Observation → interpretation → notice. Most observations never notice."""
from __future__ import annotations

from app.core.activity.evidence import CODING_CONFIDENCE_FLOOR
from app.core.live.activity_notices import (
    ACTIVITY_TRIGGERS,
    activity_transition_notices,
)
from app.core.live.frame import LiveSituationFrame, SLEEP_SPEECH_FORBID
from app.core.live.urge import LiveNotice


_NOTICE_TRIGGERS = {
    "user.typing_started": ("user_typing", "user", "user_started_typing"),
    "user.typing_stopped": ("user_typing_stopped", "user", "user_stopped_typing"),
    "user.message_sent": ("user_meaning", "user", "user_sent_text"),
    "user.speech_final": ("user_meaning", "user", "user_spoke"),
    "user.voice_start": ("user_speech", "user", "user_started_speaking"),
    "user.voice_stop": ("user_speech_stopped", "user", "user_stopped_speaking"),
    "silence.wake": ("silence", "presence", "silence_timer"),
    "idle.reconsider": ("wait_expired", "presence", "wait_expired"),
    "sleep.status_changed": ("sleep", "sleep", "sleep_status"),
    "situation.shared_commitment_changed": (
        "shared_commitment",
        "shared_activity",
        "shared_commitment",
    ),
    "user.session_changed": ("session", "session", "session_changed"),
    "aiko.affect_changed": ("affect_changed", "presence", "affect_changed"),
    "aiko.vitality_changed": ("vitality_changed", "presence", "vitality_changed"),
}

_EXPRESSIVE_KINDS = frozenset({
    "user_focus",
    "shared_commitment",
    "silence",
})


def _activity_speech_suppressed(frame: LiveSituationFrame) -> bool:
    os_idle = str(frame.shared.os_idle or "missing")
    if os_idle in {"idle", "locked"}:
        return True
    if "c6" in frame.constraints.stale_sources:
        return True
    inferred = frame.shared.inferred
    if inferred.conflicts:
        return False
    if (
        inferred.confidence < CODING_CONFIDENCE_FLOOR
        and inferred.confidence > 0.0
        and os_idle == "active"
    ):
        return True
    return False


def notices_from_trigger(
    trigger_kind: str,
    frame: LiveSituationFrame,
    *,
    previous: LiveSituationFrame | None = None,
) -> tuple[LiveNotice, ...]:
    """Return notices that crossed the behavioral threshold.

    Heartbeat with no situation change produces nothing. A coding
    observation with an anime ritual prior still does not notice delight.
    Weak or locked C6 evidence does not open expressive speech.
    """
    token = str(trigger_kind or "").strip()
    if token in {"", "heartbeat"}:
        return ()
    suppress = _activity_speech_suppressed(frame)
    notices: list[LiveNotice] = []
    notices.extend(
        activity_transition_notices(
            token, frame, previous, suppress=suppress,
        )
    )
    if token not in ACTIVITY_TRIGGERS:
        mapped = _NOTICE_TRIGGERS.get(token)
        if mapped is not None:
            kind, subject, reason = mapped
            if (
                kind == "shared_commitment"
                and frame.shared.inferred.label == "user_coding"
            ):
                if not suppress:
                    notices.append(
                        LiveNotice(
                            notice_id=f"n:{token}",
                            kind="user_focus",
                            subject="coding",
                            source=token,
                            source_ids=(token, "observation:app"),
                            reason_code="world_truth_coding",
                        )
                    )
            elif kind in _EXPRESSIVE_KINDS and suppress and not token.startswith("user."):
                pass
            else:
                notices.append(
                    LiveNotice(
                        notice_id=f"n:{token}",
                        kind=kind,
                        subject=subject,
                        source=token,
                        source_ids=(token,),
                        reason_code=reason,
                    )
                )
    if frame.constraints.sleep_status in SLEEP_SPEECH_FORBID:
        notices.append(
            LiveNotice(
                notice_id="n:sleep_constraint",
                kind="sleep",
                subject="sleep",
                source="sleep",
                source_ids=("sleep",),
                reason_code=f"sleep_{frame.constraints.sleep_status}",
            )
        )
    if previous is not None and token not in ACTIVITY_TRIGGERS:
        if (
            previous.shared.shared_commitment_active
            != frame.shared.shared_commitment_active
            and token != "situation.shared_commitment_changed"
            and not suppress
        ):
            notices.append(
                LiveNotice(
                    notice_id="n:shared_delta",
                    kind="shared_commitment",
                    subject="shared_activity",
                    source="situation.shared_commitment_changed",
                    source_ids=("situation.shared_commitment_changed",),
                    reason_code="shared_commitment",
                )
            )
    return tuple(notices)

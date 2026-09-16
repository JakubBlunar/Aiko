"""Title-free activity transition notices. Never app window titles."""
from __future__ import annotations

from app.core.activity.evidence import CODING_CONFIDENCE_FLOOR, classify_app
from app.core.live.frame import LiveSituationFrame
from app.core.live.labels import cap_live_subject
from app.core.live.urge import LiveNotice

ACTIVITY_TRIGGERS = frozenset({
    "activity.session_changed",
    "activity.idle",
    "activity.lock",
})
FOCUS_BOUNDARY_S = 20 * 60
MEDIA_FOCUS_S = 30
_AWAY = frozenset({"idle", "locked"})


def _os_idle(frame: LiveSituationFrame) -> str:
    return str(frame.shared.os_idle or "missing")


def _away(frame: LiveSituationFrame) -> bool:
    return _os_idle(frame) in _AWAY


def _category(frame: LiveSituationFrame) -> str:
    return classify_app(frame.shared.user_active_app)


def _stale(frame: LiveSituationFrame) -> bool:
    return "c6" in frame.constraints.stale_sources


def _coding_focus(frame: LiveSituationFrame) -> bool:
    inferred = frame.shared.inferred
    return (
        inferred.label == "user_coding"
        and inferred.confidence >= CODING_CONFIDENCE_FLOOR
    )


def _media_focus(frame: LiveSituationFrame) -> bool:
    if _category(frame) != "media":
        return False
    if int(frame.shared.session_duration_s or 0) < MEDIA_FOCUS_S:
        return False
    confidence = float(frame.shared.inferred.confidence or 0.0)
    if 0.0 < confidence < CODING_CONFIDENCE_FLOOR:
        return False
    return True


def _is_focus(frame: LiveSituationFrame) -> bool:
    if _os_idle(frame) != "active" or _stale(frame):
        return False
    return _coding_focus(frame) or _media_focus(frame)


def _sustained_focus(frame: LiveSituationFrame) -> bool:
    if int(frame.shared.session_duration_s or 0) < FOCUS_BOUNDARY_S:
        return False
    if _os_idle(frame) != "active" or _stale(frame):
        return False
    return _coding_focus(frame) or _category(frame) in {"coding", "media"}


def _same_session(
    previous: LiveSituationFrame,
    frame: LiveSituationFrame,
) -> bool:
    prev_app = str(previous.shared.user_active_app or "")
    app = str(frame.shared.user_active_app or "")
    return bool(
        prev_app
        and app == prev_app
        and not _away(previous)
        and not _away(frame)
        and _category(previous) == _category(frame)
    )


def _make(*, kind: str, subject: str, source: str, reason: str) -> LiveNotice | None:
    capped = cap_live_subject(subject)
    if not capped:
        return None
    return LiveNotice(
        notice_id=f"n:{kind}:{capped}",
        kind=kind,
        subject=capped,
        source=source,
        source_ids=(source, f"transition:{kind}"),
        reason_code=reason,
    )


def _append(notices: list[LiveNotice], item: LiveNotice | None) -> None:
    if item is not None:
        notices.append(item)


def activity_transition_notices(
    trigger_kind: str,
    frame: LiveSituationFrame,
    previous: LiveSituationFrame | None,
    *,
    suppress: bool,
) -> tuple[LiveNotice, ...]:
    """Deterministic C6/sleep/shared transitions. No titles, no LLM."""
    token = str(trigger_kind or "").strip()
    if token not in ACTIVITY_TRIGGERS:
        return ()
    notices: list[LiveNotice] = []

    if token in {"activity.idle", "activity.lock"}:
        if previous is not None and _sustained_focus(previous):
            _append(notices, _make(
                kind="focus_boundary",
                subject="presence",
                source=token,
                reason="focus_boundary",
            ))
        return tuple(notices)

    if (
        previous is not None
        and _away(previous)
        and _os_idle(frame) == "active"
        and not _stale(frame)
    ):
        _append(notices, _make(
            kind="returned_to_machine",
            subject="machine",
            source=token,
            reason="returned_to_machine",
        ))

    if (
        previous is not None
        and _sustained_focus(previous)
        and _away(frame)
    ):
        _append(notices, _make(
            kind="focus_boundary",
            subject="presence",
            source=token,
            reason="focus_boundary",
        ))

    prev_cat = _category(previous) if previous is not None else ""
    curr_cat = _category(frame)
    category_changed = bool(
        previous is not None
        and prev_cat
        and curr_cat
        and prev_cat != curr_cat
        and _os_idle(frame) == "active"
        and not _stale(frame)
        and not suppress
    )
    if category_changed:
        _append(notices, _make(
            kind="app_category_changed",
            subject=curr_cat,
            source=token,
            reason="app_category_changed",
        ))

    if not suppress and not category_changed and _is_focus(frame):
        same = previous is not None and _same_session(previous, frame)
        if not same:
            subject = "coding" if _coding_focus(frame) else "media"
            _append(notices, _make(
                kind="focus_started",
                subject=subject,
                source=token,
                reason="focus_started",
            ))

    if (
        not suppress
        and previous is not None
        and str(frame.shared.sharing or "") == "shared"
        and bool(frame.shared.shared_commitment_active)
        and frame.shared.inferred.label != "user_coding"
        and (
            _away(previous)
            or str(previous.shared.sharing or "") != "shared"
        )
    ):
        _append(notices, _make(
            kind="shared_activity_resumed",
            subject="shared_activity",
            source=token,
            reason="shared_activity_resumed",
        ))

    return tuple(notices)


__all__ = [
    "ACTIVITY_TRIGGERS",
    "FOCUS_BOUNDARY_S",
    "MEDIA_FOCUS_S",
    "activity_transition_notices",
]

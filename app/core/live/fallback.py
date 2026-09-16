"""One-shot Live fallback after a typed execute failure.

Capability no-ops stay deterministic and never wake the 4B. A typed
failure may arm one fallback menu; the same action_id cannot retry.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.core.live.proposal import LIVE_POLICY_INTENTS


FALLBACK_FAILURES = (
    "capability_absent",
    "floor_preempted",
    "budget_exhausted",
)
FALLBACK_FAILURE_SET = frozenset(FALLBACK_FAILURES)

_FLOOR_REASONS = frozenset({
    "floor_busy",
    "turn_or_tts",
    "user_active",
    "user_turn",
    "floor_preempted",
})

FALLBACK_MENU: dict[str, tuple[str, ...]] = {
    "budget_exhausted": ("wait", "noop", "remain_present"),
    "floor_preempted": ("wait", "noop", "remain_present", "yield_floor"),
    "capability_absent": ("wait", "noop", "remain_present"),
}

_SILENT_DEGRADE = frozenset({
    "capability_absent",
    "repeat_suppressed",
    "invalid",
    "missing_text",
    "speech_forbidden",
    "sleep",
    "dnd",
    "missing_urge",
    "weak_urge",
    "not_allowed",
})


def classify_execute_failure(
    reason: object,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Map execute leftovers onto the three typed failures, or empty."""
    payload = extra if isinstance(extra, Mapping) else {}
    if payload.get("budget_exhausted") or str(reason or "") == "budget_exhausted":
        return "budget_exhausted"
    token = str(
        payload.get("micro_skipped")
        or payload.get("main_wake_rejected")
        or payload.get("overlay_skipped")
        or reason
        or ""
    ).strip()
    if token in _FLOOR_REASONS:
        return "floor_preempted"
    if token in {"capability_absent", "capability"}:
        return "capability_absent"
    return ""


def should_arm_fallback(
    failure: str,
    *,
    intent: str = "",
) -> bool:
    """Most degradation is already handled. Only typed stalls wake the 4B."""
    token = str(failure or "").strip()
    if token not in FALLBACK_FAILURE_SET:
        return False
    if token == "capability_absent":
        return False
    if str(intent or "") in {"wait", "noop"}:
        return False
    return True


def legal_fallbacks(failure: str) -> tuple[str, ...]:
    token = str(failure or "").strip()
    allowed = FALLBACK_MENU.get(token, ("wait",))
    return tuple(item for item in allowed if item in LIVE_POLICY_INTENTS)


def is_silent_degrade(reason: object) -> bool:
    return str(reason or "").strip() in _SILENT_DEGRADE


def render_fallback_prompt(payload: Mapping[str, Any] | None) -> str:
    """Compact FALLBACK region. No titles, no scripts."""
    if not isinstance(payload, Mapping) or not payload:
        return ""
    action_id = str(payload.get("action_id") or "").strip() or "none"
    failure = str(payload.get("failure") or "").strip() or "unknown"
    original = str(payload.get("original_intent") or "").strip() or "unknown"
    legal = payload.get("legal") or ()
    if isinstance(legal, str):
        menu = legal
    else:
        menu = ",".join(str(item) for item in legal if str(item).strip())
    if not menu:
        menu = "wait"
    return (
        f"FALLBACK failure={failure} action_id={action_id} "
        f"original_intent={original}\n"
        f"legal={menu}\n"
        "Choose exactly one legal intent or wait. Do not retry this action_id."
    )


__all__ = [
    "FALLBACK_FAILURES",
    "FALLBACK_FAILURE_SET",
    "FALLBACK_MENU",
    "classify_execute_failure",
    "is_silent_degrade",
    "legal_fallbacks",
    "render_fallback_prompt",
    "should_arm_fallback",
]

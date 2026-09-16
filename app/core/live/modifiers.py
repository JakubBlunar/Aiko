"""Clamped Live behavior modifiers. Concepts tune; they do not override truth."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.core.activity.evidence import CODING_CONFIDENCE_FLOOR
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.presence import (
    PRESENCE_STYLE_SET,
    STYLE_INTENSITY_CAP,
    STYLE_SPEECH_CAP,
    quieter_speech,
)
from app.core.live.urge import URGE_RANK
from app.core.live.urge_store import LiveUrgeStore


SPEECH_BUDGETS = ("forbidden", "rare", "normal", "open")
_ANIME_TOKENS = ("anime", "episode", "watch")
_FOCUS_LABELS = frozenset({"user_coding", "user_focused"})

_SPEECH_RANK = {"forbidden": 0, "rare": 1, "normal": 2, "open": 3}


@dataclass(frozen=True, slots=True)
class LiveBehaviorModifiers:
    speech_budget: str = "normal"
    min_interruption_score: float = 0.0
    attention_preference: str = ""
    max_reaction_intensity: float = 1.0
    questions_allowed: bool = True
    min_gap_after_speech_ms: int = 0
    reason_code: str = "default"

    def to_payload(self) -> dict[str, Any]:
        return {
            "speech_budget": self.speech_budget,
            "min_interruption_score": self.min_interruption_score,
            "attention_preference": self.attention_preference,
            "max_reaction_intensity": self.max_reaction_intensity,
            "questions_allowed": self.questions_allowed,
            "min_gap_after_speech_ms": self.min_gap_after_speech_ms,
            "reason_code": self.reason_code,
        }

    def rank_bonus(self) -> dict[str, int]:
        bonus: dict[str, int] = {}
        if self.speech_budget in {"forbidden", "rare"}:
            bonus["remain_present"] = 30
            bonus["share_delight"] = -25
            bonus["ask_about_result"] = -40
            bonus["comfort"] = -10
        if not self.questions_allowed:
            bonus["ask_about_result"] = bonus.get("ask_about_result", 0) - 20
        return bonus


def _clamp_speech(value: str) -> str:
    token = str(value or "normal")
    if token not in SPEECH_BUDGETS:
        return "normal"
    return token


def _quieter(current: str, candidate: str) -> str:
    if _SPEECH_RANK.get(candidate, 2) < _SPEECH_RANK.get(current, 2):
        return candidate
    return current


def _looks_like_anime(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in _ANIME_TOKENS)


def modifiers_from_situation(
    frame: LiveSituationFrame,
    picks: Sequence[Any] = (),
    *,
    live_quiet: bool = False,
    min_gap_after_speech_ms: int = 0,
) -> LiveBehaviorModifiers:
    """World-truth first. Concepts may quiet speech; they cannot raise it."""
    speech = "normal"
    questions = True
    intensity = 1.0
    try:
        gap = max(0, int(min_gap_after_speech_ms or 0))
    except (TypeError, ValueError):
        gap = 0
    attention = ""
    reason = "default"

    sleep = frame.constraints.sleep_status
    if live_quiet or frame.constraints.dnd or sleep in SLEEP_SPEECH_FORBID:
        return LiveBehaviorModifiers(
            speech_budget="forbidden",
            min_interruption_score=1.0,
            max_reaction_intensity=0.2,
            questions_allowed=False,
            min_gap_after_speech_ms=max(gap, 15_000),
            reason_code="constraint",
        )

    os_idle = str(frame.shared.os_idle or "missing")
    if os_idle == "locked":
        return LiveBehaviorModifiers(
            speech_budget="forbidden",
            min_interruption_score=1.0,
            max_reaction_intensity=0.2,
            questions_allowed=False,
            min_gap_after_speech_ms=max(gap, 15_000),
            reason_code="constraint",
        )

    inferred = str(frame.shared.inferred.label or "")
    inferred_confidence = float(frame.shared.inferred.confidence or 0.0)
    c6_stale = "c6" in frame.constraints.stale_sources
    coding_fresh = (
        inferred in _FOCUS_LABELS
        and not c6_stale
        and os_idle != "idle"
        and (
            inferred_confidence >= CODING_CONFIDENCE_FLOOR
            or os_idle == "missing"
        )
    )
    if coding_fresh:
        speech = "rare"
        questions = False
        intensity = 0.4
        gap = max(gap, 12_000)
        attention = "user"
        reason = "world_truth_coding"

    labels = [str(getattr(pick, "label", "") or "") for pick in picks]
    kinds = [str(getattr(pick, "kind", "") or "") for pick in picks]
    anime_concept = any(
        kind == "ritual" and _looks_like_anime(label)
        for kind, label in zip(kinds, labels, strict=True)
    )
    inferred_anime = _looks_like_anime(inferred) or inferred == "watching_anime_together"
    sharing = str(frame.shared.sharing or "")
    if inferred in _FOCUS_LABELS:
        # A ritual prior cannot mint an anime speech opening while coding.
        pass
    elif inferred_anime or (anime_concept and sharing == "shared"):
        speech = _quieter(speech, "rare")
        questions = False
        intensity = min(intensity, 0.5)
        gap = max(gap, 8_000)
        attention = "shared_activity"
        reason = "shared_media_quiet"

    return LiveBehaviorModifiers(
        speech_budget=_clamp_speech(speech),
        min_interruption_score=0.6 if speech == "rare" else 0.0,
        attention_preference=attention,
        max_reaction_intensity=intensity,
        questions_allowed=questions,
        min_gap_after_speech_ms=max(0, int(gap)),
        reason_code=reason,
    )


def apply_presence_style(
    modifiers: LiveBehaviorModifiers,
    style: str,
) -> LiveBehaviorModifiers:
    """Lower speech/reaction intensity only. Never raise a budget."""
    token = str(style or "neutral")
    if token not in PRESENCE_STYLE_SET:
        token = "neutral"
    speech = quieter_speech(
        modifiers.speech_budget, STYLE_SPEECH_CAP.get(token, "open"),
    )
    intensity = min(
        float(modifiers.max_reaction_intensity),
        float(STYLE_INTENSITY_CAP.get(token, 1.0)),
    )
    return LiveBehaviorModifiers(
        speech_budget=_clamp_speech(speech),
        min_interruption_score=modifiers.min_interruption_score,
        attention_preference=modifiers.attention_preference,
        max_reaction_intensity=intensity,
        questions_allowed=modifiers.questions_allowed,
        min_gap_after_speech_ms=modifiers.min_gap_after_speech_ms,
        reason_code=modifiers.reason_code,
    )


def apply_modifiers_to_urges(
    store: LiveUrgeStore,
    modifiers: LiveBehaviorModifiers,
) -> None:
    """Park or withdraw expressive urges. Nothing here executes."""
    if modifiers.speech_budget == "forbidden":
        store.withdraw_expressive(reason="speech_budget")
        return
    if not modifiers.questions_allowed:
        for urge in list(store.active()):
            if urge.kind == "ask_about_result":
                store.park(urge.urge_id)
    store.recompete(rank_bonus=modifiers.rank_bonus())


def effective_rank(kind: str, bonus: Mapping[str, int] | None = None) -> int:
    extra = int((bonus or {}).get(kind, 0))
    return int(URGE_RANK.get(kind, 0)) + extra

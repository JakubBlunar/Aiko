"""Model-free Live decision epochs.

Phase 2 records when a later policy tick would be warranted. Nothing
here calls an LLM or executes an action.
"""
from __future__ import annotations

from typing import Literal

EpochKind = Literal["immediate", "coalesced_transition", "data_only"]

IMMEDIATE_KINDS = frozenset({
    "user.message_sent",
    "user.speech_final",
    "user.voice_start",
    "user.voice_stop",
    "user.stop",
    "user.session_changed",
})

COALESCED_KINDS = frozenset({
    "silence.wake",
    "sleep.status_changed",
    "situation.shared_commitment_changed",
    "activity.session_changed",
    "activity.idle",
    "activity.lock",
    "idle.reconsider",
})

DATA_ONLY_KINDS = frozenset({
    "user.typing_started",
    "user.typing_stopped",
    "heartbeat",
})


def classify_epoch(
    kind: str,
    *,
    situation_changed: bool = False,
) -> EpochKind:
    """Classify one trigger. High-rate typing is data-only."""
    token = str(kind or "").strip()
    if token in IMMEDIATE_KINDS:
        return "immediate"
    if token.startswith("aiko.action_"):
        return "data_only"
    if token in {"aiko.affect_changed", "aiko.vitality_changed"}:
        return "data_only"
    if token in COALESCED_KINDS or situation_changed:
        return "coalesced_transition"
    return "data_only"

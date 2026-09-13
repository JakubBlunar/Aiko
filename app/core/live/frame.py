"""Immutable Live situation frame (Phase 2+3).

Seven first-class sections plus a monotonic generation. Urge fields
are inspectable inclination; nothing here grants action permission.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SLEEP_SPEECH_FORBID = frozenset({"asleep", "winding_down", "woken"})
ATTENTION_TARGETS = frozenset({
    "user",
    "cursor",
    "world_entity",
    "shared_activity",
    "none",
})
ATTENTION_MODES = frozenset({
    "engaged",
    "monitoring",
    "casual",
    "distracted",
    "resting",
})
FLOOR_OWNERS = frozenset({"user", "aiko", "neither", "transition"})
SHARING_LABELS = frozenset({"shared", "user_only", "aiko_only", "unknown"})
TEMPORAL_PHASES = frozenset({"immediate", "recent", "settled", "prolonged"})


@dataclass(frozen=True, slots=True)
class InferredField:
    """One hypothesis with the evidence contract the assembler must fill."""

    label: str = ""
    confidence: float = 0.0
    evidence_ids: tuple[str, ...] = ()
    last_confirmed_at: str = ""
    decay: str = "evidence_age"
    conflicts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InteractionNow:
    floor_owner: str = "neither"
    typing_active: bool = False
    speech_active: bool = False
    endpointing: str = ""
    last_user_meaning: str = ""
    turn_active: bool = False
    tts_active: bool = False
    playback_active: bool = False
    capture_available: bool = False
    voice_session_active: bool = False
    connection_generation: int = 0
    silence_since_user_intent_ms: int = 0
    silence_since_aiko_speech_ms: int = 0
    silence_since_shared_activity_ms: int = 0

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SharedSituation:
    user_active_app: str = ""
    os_idle: str = "missing"
    session_duration_s: int = 0
    session_count: int = 0
    idle_span_s: int = 0
    lock_span_s: int = 0
    inferred: InferredField = field(default_factory=InferredField)
    sharing: str = "unknown"
    scene_name: str = ""
    location_slug: str = ""
    location_name: str = ""
    world_activity: str = ""
    world_posture: str = ""
    inferred_stale: bool = True
    world_compatible: bool = False
    conflict_reason: str = ""
    shared_commitment_active: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass(frozen=True, slots=True)
class AttentionSection:
    target: str = "none"
    target_id: str = ""
    mode: str = "casual"
    evidence_confidence: float = 0.0
    entered_at_monotonic_ms: float = 0.0
    held_until_monotonic_ms: float = 0.0
    last_meaningful_change_ms: float = 0.0
    reason_code: str = "none"
    challenger_target: str = ""
    hysteresis_margin: float = 0.2
    dwell_ms: int = 0
    enter_threshold: float = 0.5
    exit_threshold: float = 0.35

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BehaviorCommitment:
    """Held intention with Schmitt-trigger enter/exit bounds.

    Confidence is evidence confidence, never a policy-model score.
    """

    intention: str = "wait"
    target: str = "none"
    entered_at_monotonic_ms: float = 0.0
    minimum_hold_until_ms: float = 0.0
    evidence_confidence: float = 0.0
    enter_threshold: float = 0.5
    exit_threshold: float = 0.35
    switch_margin: float = 0.2
    wake_on: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AikoNow:
    mood_label: str = ""
    vitality_band: str = ""
    posture: str = ""
    activity: str = ""
    current_actions: tuple[str, ...] = ()
    recent_actions: tuple[str, ...] = ()
    cooldowns: dict[str, int] = field(default_factory=dict)
    candidate_urges: tuple[str, ...] = ()
    selected_urge: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContinuitySection:
    relationship_phase: str = ""
    arc: str = ""
    arc_confidence: float = 0.0
    goals: tuple[str, ...] = ()
    cue_pressure: str = ""
    behavior_rails: tuple[str, ...] = ()
    situation_concepts: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TemporalState:
    typing_active_ms: int = 0
    typing_idle_ms: int = 0
    user_speech_active_ms: int = 0
    since_user_speech_ms: int = 0
    since_user_intent_ms: int = 0
    since_aiko_spoke_ms: int = 0
    since_semantic_action_ms: int = 0
    situation_age_ms: int = 0
    since_confirming_evidence_ms: int = 0
    attention_dwell_ms: int = 0
    min_hold_remaining_ms: int = 0
    urge_ttl_ms: int = 0
    cooldown_remaining_ms: int = 0
    next_reconsideration_ms: int = 0
    phase: str = "settled"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConstraintSection:
    allowed_actions: tuple[str, ...] = ()
    speech_forbid_reasons: tuple[str, ...] = ()
    dnd: bool = False
    privacy: str = "local_state"
    stale_sources: tuple[str, ...] = ()
    resource_contention: str = ""
    capture_available: bool = False
    sleep_status: str = "awake"
    speech_budget: str = "normal"
    questions_allowed: bool = True
    min_gap_after_speech_ms: int = 0
    max_reaction_intensity: float = 1.0

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LiveEpochRecord:
    kind: str = "data_only"
    reason: str = "heartbeat"
    frame_generation: int = 0
    at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LiveSituationFrame:
    """Inspectable answer to 'what is happening now?'"""

    generation: int
    observed_at: str
    monotonic_ms: float
    session_id: str
    mode_generation: int
    interaction: InteractionNow
    shared: SharedSituation
    attention: AttentionSection
    aiko: AikoNow
    continuity: ContinuitySection
    temporal: TemporalState
    constraints: ConstraintSection
    epoch: LiveEpochRecord = field(default_factory=LiveEpochRecord)
    commitment: BehaviorCommitment = field(default_factory=BehaviorCommitment)

    def to_payload(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "observed_at": self.observed_at,
            "monotonic_ms": self.monotonic_ms,
            "session_id": self.session_id,
            "mode_generation": self.mode_generation,
            "interaction": self.interaction.to_payload(),
            "shared": self.shared.to_payload(),
            "attention": self.attention.to_payload(),
            "commitment": self.commitment.to_payload(),
            "aiko": self.aiko.to_payload(),
            "continuity": self.continuity.to_payload(),
            "temporal": self.temporal.to_payload(),
            "constraints": self.constraints.to_payload(),
            "epoch": self.epoch.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class LiveSituationOverlay:
    """Live fields joined onto ConversationSituationSnapshot."""

    floor_owner: str = "neither"
    typing_active: bool = False
    attention_target: str = "none"
    attention_mode: str = "casual"
    live_frame_generation: int = 0


def overlay_from_frame(frame: LiveSituationFrame) -> LiveSituationOverlay:
    return LiveSituationOverlay(
        floor_owner=frame.interaction.floor_owner,
        typing_active=frame.interaction.typing_active,
        attention_target=frame.attention.target,
        attention_mode=frame.attention.mode,
        live_frame_generation=frame.generation,
    )

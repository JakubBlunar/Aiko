"""Pure state and propensity rules for Aiko's persisted sleep lifecycle."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any, Literal

from app.core.infra import timephrase


SleepStatus = Literal["awake", "winding_down", "asleep", "woken"]
SleepKind = Literal["overnight", "nap"]
SleepAction = Literal[
    "wind_down",
    "fall_asleep",
    "cancel",
    "wake",
    "stay_asleep",
    "fully_awake",
    "back_to_sleep",
]

AWAKE = "awake"
WINDING_DOWN = "winding_down"
ASLEEP = "asleep"
WOKEN = "woken"

OVERNIGHT = "overnight"
NAP = "nap"

# Room activities that mark a resting Aiko. Cleared back to ``idle`` the
# moment the lifecycle reaches ``awake`` — both the worker's autonomous
# flip and the startup self-heal read this same set, so a projection of
# ``waking_up`` can never outlive the state it describes.
REST_ACTIVITIES = frozenset({"napping", "waking_up"})

VALID_STATUSES = frozenset({AWAKE, WINDING_DOWN, ASLEEP, WOKEN})
VALID_KINDS = frozenset({OVERNIGHT, NAP})
VALID_ACTIONS = frozenset(
    {
        "wind_down",
        "fall_asleep",
        "cancel",
        "wake",
        "stay_asleep",
        "fully_awake",
        "back_to_sleep",
    }
)

_ALLOWED: dict[str, frozenset[str]] = {
    AWAKE: frozenset({"wind_down"}),
    WINDING_DOWN: frozenset({"fall_asleep", "cancel"}),
    ASLEEP: frozenset({"wake", "stay_asleep"}),
    WOKEN: frozenset({"fully_awake", "back_to_sleep"}),
}

_RESTFUL_PERIODS = frozenset({"night", "late_night", "early_morning"})


class InvalidSleepTransition(ValueError):
    """Raised when a proposal is not legal from the durable current state."""


def _iso(when: datetime) -> str:
    return timephrase.to_aware(when).isoformat(timespec="seconds")


def clean_reason_text(value: str | None, *, max_chars: int = 240) -> str:
    """Return bounded storage-safe reason prose, dropping relative deictics."""
    text = " ".join(str(value or "").split()).strip()
    if not text or timephrase.has_relative_deictic(text):
        return ""
    return text[: max(0, int(max_chars))]


@dataclass(frozen=True, slots=True)
class SleepState:
    """Singleton state read by turns, workers, WebSocket clients, and MCP."""

    status: SleepStatus
    generation: int
    entered_at: str
    updated_at: str
    current_episode_id: int | None = None
    sleep_kind: SleepKind | None = None
    reason_code: str = ""
    reason_text: str = ""
    last_woken_at: str | None = None
    previous_world: dict[str, Any] | None = None

    @property
    def sleeping(self) -> bool:
        return self.status == ASLEEP

    @property
    def sleep_active(self) -> bool:
        return self.status in {WINDING_DOWN, ASLEEP, WOKEN}

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SleepEpisode:
    """One bounded sleep period, including user interruptions."""

    id: int
    kind: SleepKind
    reason_code: str
    reason_text: str
    started_at: str
    ended_at: str | None
    outcome: str
    previous_world: dict[str, Any]
    interruptions: tuple[dict[str, Any], ...]
    dream_memory_id: int | None = None
    diary_memory_id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["interruptions"] = [dict(row) for row in self.interruptions]
        return payload


@dataclass(frozen=True, slots=True)
class PropensityInputs:
    """Cheap, deterministic inputs for an autonomous wind-down decision."""

    energy: float
    circadian_baseline: float
    idle_hours: float
    awake_hours: float
    circadian_period: str
    naps_enabled: bool = True
    shared_situation_active: bool = False
    intentional_world_hold: bool = False


@dataclass(frozen=True, slots=True)
class PropensityDecision:
    should_wind_down: bool
    sleep_kind: SleepKind | None
    score: float
    reason_code: str


def initial_state(now: datetime | None = None) -> SleepState:
    stamp = _iso(now or timephrase.utcnow())
    return SleepState(
        status=AWAKE,
        generation=0,
        entered_at=stamp,
        updated_at=stamp,
    )


def can_transition(status: str, action: str) -> bool:
    return action in _ALLOWED.get(status, frozenset())


def transition(
    state: SleepState,
    action: SleepAction | str,
    *,
    now: datetime | None = None,
    sleep_kind: SleepKind | str | None = None,
    reason_code: str = "",
    reason_text: str = "",
    episode_id: int | None = None,
    previous_world: dict[str, Any] | None = None,
) -> SleepState:
    """Apply one legal edge without I/O.

    ``stay_asleep`` deliberately advances no lifecycle state. The store still
    records the accompanying interruption against the active episode.
    """
    proposal = str(action or "").strip().lower()
    if proposal not in VALID_ACTIONS or not can_transition(state.status, proposal):
        raise InvalidSleepTransition(
            f"cannot apply sleep action {proposal!r} from {state.status!r}"
        )

    stamp = _iso(now or timephrase.utcnow())
    generation = int(state.generation) + 1
    if proposal == "stay_asleep":
        return replace(state, generation=generation, updated_at=stamp)

    if proposal == "wind_down":
        kind = str(sleep_kind or OVERNIGHT).strip().lower()
        if kind not in VALID_KINDS:
            kind = OVERNIGHT
        return SleepState(
            status=WINDING_DOWN,
            generation=generation,
            entered_at=stamp,
            updated_at=stamp,
            current_episode_id=episode_id,
            sleep_kind=kind,  # type: ignore[arg-type]
            reason_code=str(reason_code or "tired").strip()[:64],
            reason_text=clean_reason_text(reason_text),
            previous_world=dict(previous_world or {}),
        )

    if proposal == "fall_asleep":
        return replace(
            state,
            status=ASLEEP,
            generation=generation,
            entered_at=stamp,
            updated_at=stamp,
            current_episode_id=episode_id or state.current_episode_id,
        )

    if proposal == "wake":
        return replace(
            state,
            status=WOKEN,
            generation=generation,
            entered_at=stamp,
            updated_at=stamp,
            last_woken_at=stamp,
        )

    if proposal == "back_to_sleep":
        return replace(
            state,
            status=ASLEEP,
            generation=generation,
            entered_at=stamp,
            updated_at=stamp,
        )

    # ``cancel`` and ``fully_awake`` both end the active lifecycle.
    return SleepState(
        status=AWAKE,
        generation=generation,
        entered_at=stamp,
        updated_at=stamp,
        last_woken_at=state.last_woken_at,
    )


def evaluate_propensity(
    inputs: PropensityInputs,
    *,
    overnight_threshold: float = 0.58,
    nap_energy_threshold: float = 0.22,
    min_idle_hours: float = 0.25,
    min_awake_hours: float = 4.0,
) -> PropensityDecision:
    """Choose whether a quiet, awake Aiko should begin winding down."""
    if inputs.shared_situation_active:
        return PropensityDecision(False, None, 0.0, "shared_situation")
    if inputs.intentional_world_hold:
        return PropensityDecision(False, None, 0.0, "intentional_world_hold")

    energy = max(0.0, min(1.0, float(inputs.energy)))
    baseline = max(0.0, min(1.0, float(inputs.circadian_baseline)))
    idle_h = max(0.0, float(inputs.idle_hours))
    awake_h = max(0.0, float(inputs.awake_hours))
    if idle_h < max(0.0, float(min_idle_hours)):
        return PropensityDecision(False, None, 0.0, "not_quiet_long_enough")
    if awake_h < max(0.0, float(min_awake_hours)):
        return PropensityDecision(False, None, 0.0, "minimum_awake_hold")

    period = str(inputs.circadian_period or "").strip().lower()
    overnight = period in _RESTFUL_PERIODS or baseline <= 0.35
    idle_factor = min(1.0, idle_h / 2.0)
    awake_factor = min(1.0, awake_h / 16.0)
    score = round(
        0.50 * (1.0 - energy)
        + 0.30 * (1.0 - baseline)
        + 0.10 * idle_factor
        + 0.10 * awake_factor,
        4,
    )
    if overnight and score >= max(0.0, float(overnight_threshold)):
        return PropensityDecision(True, OVERNIGHT, score, "circadian_low")

    nap_due = (
        bool(inputs.naps_enabled)
        and not overnight
        and energy <= max(0.0, float(nap_energy_threshold))
        and idle_h >= 0.5
    )
    if nap_due:
        return PropensityDecision(True, NAP, score, "depleted")
    return PropensityDecision(False, None, score, "below_threshold")


def should_finish_waking(
    *,
    energy: float,
    woken_minutes: float,
    engaged_turns: int = 0,
    energy_threshold: float = 0.48,
    max_woken_minutes: float = 90.0,
) -> bool:
    """Deterministic escape hatch so ``woken`` cannot persist forever."""
    return (
        float(energy) >= float(energy_threshold)
        or int(engaged_turns) >= 2
        or float(woken_minutes) >= float(max_woken_minutes)
    )


def should_wake_overnight(
    *,
    now_local: datetime,
    sleep_started_local: datetime,
    wake_hour: float,
    wake_minute: int,
    min_sleep_hours: float,
    max_sleep_hours: float,
) -> bool:
    """Decide whether an overnight sleep has earned a wake.

    The earliest of two conditions wins:

    * **Morning target** — once the local clock passes the configured wake
      hour on the day after sleep started (or, for a late-night sleep that
      crosses midnight, the very next day), and she has slept at least
      ``min_sleep_hours``, the sleep is due to end. This is the "wakes up
      in the morning" behaviour.
    * **Hard cap** — ``max_sleep_hours`` of total sleep, so no sleep can
      run away (the "whole day" failure mode).

    Both anchors are taken from the sleep onset in local time, so the rule
    is deterministic from timestamps alone — no LLM, no I/O.
    """
    slept_h = max(
        0.0, (now_local - sleep_started_local).total_seconds() / 3600.0
    )
    if slept_h >= max(0.0, float(max_sleep_hours)):
        return True
    if slept_h < max(0.0, float(min_sleep_hours)):
        return False
    target = time(
        int(max(0, min(23, int(float(wake_hour))))),
        int(max(0, min(59, int(wake_minute)))),
    )
    # The wake instant is the first occurrence of the target wall-clock
    # time at or after sleep began. A sleep that started at 23:30 with a
    # 07:00 target therefore wakes at 07:00 the following day (7.5h);
    # a sleep that started at 00:30 wakes at 07:00 the same day (6.5h).
    candidate = sleep_started_local.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if candidate < sleep_started_local:
        candidate += timedelta(days=1)
    return now_local >= candidate


__all__ = [
    "ASLEEP",
    "AWAKE",
    "NAP",
    "OVERNIGHT",
    "REST_ACTIVITIES",
    "VALID_ACTIONS",
    "VALID_KINDS",
    "VALID_STATUSES",
    "WINDING_DOWN",
    "WOKEN",
    "InvalidSleepTransition",
    "PropensityDecision",
    "PropensityInputs",
    "SleepEpisode",
    "SleepState",
    "can_transition",
    "clean_reason_text",
    "evaluate_propensity",
    "initial_state",
    "should_finish_waking",
    "should_wake_overnight",
    "transition",
]

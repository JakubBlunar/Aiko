"""Live worker-ownership matrix.

Idle quiet gate stays ``_live_voice_session_active`` only. Do not add
``_live_mode_enabled``. Do not punch per-worker holes in ``_is_user_idle``.
Live posture is who owns ongoing behavior, not whether the microphone is
open. Silence-timer ``ProactiveDirector`` stays starved under Live;
``CueUrgeAdapter`` peeks only and never ``take_pool_cue``.
"""
from __future__ import annotations

from typing import Literal

LiveWorkerRole = Literal[
    "suspend",
    "keep_running",
    "impulse_only",
    "fulfilment_owned",
    "heartbeat",
    "must_not_speak",
]

WORKER_POSTURE: dict[str, LiveWorkerRole] = {
    "away_activity_movers": "suspend",
    "garden_circadian_movers": "suspend",
    "plant_growth_item_only": "keep_running",
    "sleep_lifecycle": "keep_running",
    "proactive_director_silence": "impulse_only",
    "cue_pool": "fulfilment_owned",
    "affect_vitality_situation_memory": "heartbeat",
    "speech_paths": "must_not_speak",
}

WORKER_ENFORCED_BY: dict[str, str] = {
    "away_activity_movers": "WorldMutationGuard",
    "garden_circadian_movers": "WorldMutationGuard",
    "plant_growth_item_only": "WorldMutationGuard.item_only",
    "sleep_lifecycle": "SleepSnapshot",
    "proactive_director_silence": "Live starve + silence.wake",
    "cue_pool": "CueUrgeAdapter peek-only",
    "affect_vitality_situation_memory": "_live_heartbeat_tick",
    "speech_paths": "Live arbiter -> BrainLoop",
}

SPEC_ROWS: tuple[str, ...] = (
    "away_activity_movers",
    "garden_circadian_movers",
    "plant_growth_item_only",
    "sleep_lifecycle",
    "proactive_director_silence",
    "cue_pool",
    "affect_vitality_situation_memory",
    "speech_paths",
)


def role_for(name: str) -> LiveWorkerRole:
    return WORKER_POSTURE[name]

"""VitalityWorker — idle recovery path for K68 embodied vitality.

Thin :class:`IdleWorker` that relaxes Aiko's body-energy scalar toward
the circadian baseline during quiet windows and broadcasts the new level
so the avatar **visibly droops while she's left alone** (and the next
turn reads the recovered energy). Matches the
[`DayColorWorker`](day_color_worker.py) /
[`MemoryDecayWorker`](../memory/memory_decay_worker.py) shape exactly so
it slots into the :class:`IdleWorkerScheduler` with no special handling.

Hybrid design, mirroring K27: this worker is the **regular idle cadence**
(recover + broadcast every ``vitality_check_interval_seconds``), while
the provider in
[`inner_life_part1.py`](../session/inner_life_part1.py) has a cheap lazy
fallback that runs the same :func:`vitality.step_recover` on the next
turn — so a user who returns mid-recovery still sees the right level
without waiting for an idle tick. The per-turn spend / interest-boost
lives in [`post_turn_mixin.py`](../session/post_turn_mixin.py).

Storage on ``kv_meta`` (no schema change): one JSON key
``aiko.vitality`` (shared with the provider + post-turn writer).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.affect import vitality as _vit
from app.core.affect import vitality_rhythm as _vr
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings


log = logging.getLogger("app.vitality_worker")


class VitalityWorker:
    """IdleWorker that recovers body-energy toward the circadian baseline.

    Cheap tick: one ``kv_get`` + one float relax + (only when the level
    actually moved) one ``kv_set`` + one broadcast. The broadcast is
    debounced upstream by the controller's ``_notify_vitality`` (≥ 0.03
    step), so a string of tiny idle ticks won't flood the WS.
    """

    name = "vitality"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        notify: Callable[[float], None] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._agent = agent_settings
        self._memory = memory_settings
        self._notify = notify

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(self._agent, "vitality_check_interval_seconds", 900)
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._agent, "vitality_enabled", True)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent, "vitality_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        try:
            now = datetime.now().astimezone()
            baseline, _rhythm = _vr.current_baseline(
                self._chat_db,
                now,
                enabled=bool(
                    getattr(self._agent, "vitality_rhythm_enabled", True)
                ),
                exception_chance=float(
                    getattr(
                        self._memory, "vitality_rhythm_exception_chance", 0.3
                    )
                ),
            )
            try:
                raw = self._chat_db.kv_get(_vit.KV_VITALITY)
            except Exception:
                log.debug("vitality worker kv_get failed", exc_info=True)
                return {"skipped": True, "reason": "kv_get_failed"}
            state = _vit.deserialize(raw, baseline=baseline, now=now)
            new_state = _vit.step_recover(
                state, baseline, now,
                half_life_hours=float(
                    getattr(
                        self._memory, "vitality_recover_half_life_hours", 2.0
                    )
                ),
            )
            moved = abs(new_state.energy - state.energy) > 1e-6
            try:
                self._chat_db.kv_set(
                    _vit.KV_VITALITY, _vit.serialize(new_state),
                )
            except Exception:
                log.debug("vitality worker kv_set failed", exc_info=True)
                return {"skipped": True, "reason": "kv_set_failed"}

            if moved and self._notify is not None:
                try:
                    self._notify(new_state.energy)
                except Exception:
                    log.debug("vitality worker notify raised", exc_info=True)

            if moved:
                log.info(
                    "vitality recovered: energy=%.3f -> %.3f baseline=%.3f",
                    float(state.energy), float(new_state.energy), baseline,
                )
            return {
                "recovered": moved,
                "energy": round(float(new_state.energy), 4),
                "baseline": round(float(baseline), 4),
            }
        except Exception:
            log.warning("vitality worker run failed", exc_info=True)
            return {"skipped": True, "reason": "error"}


__all__ = ["VitalityWorker"]

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
from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings


log = logging.getLogger("app.vitality_worker")

# Energy is a continuous ``[0, 1]`` scalar, so its probe cannot use the
# discrete pressure floor: recovery is asymptotic, so "would this step
# move the level at all" is true almost always, and flooring on it
# pinned the worker at 0.5 and had it running every 91 s against a
# 900 s heartbeat. Scale by the size of the pending correction instead,
# reporting zero below a move nothing downstream would notice — the
# controller debounces the WS broadcast at a 0.03 step, and 0.005 of
# energy is 0.0025 of the gesture-amplitude multiplier. Saturating at
# 0.05 puts one heartbeat's worth of recovery from a half-scale deficit
# near the top of the range.
_MOVE_DEADBAND = 0.005
_MOVE_SATURATION = 0.05


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
        return bool(getattr(self._agent, "vitality_enabled", True))

    def _half_life_hours(self) -> float:
        return float(
            getattr(self._memory, "vitality_recover_half_life_hours", 2.0)
        )

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """How much recovery this run would actually apply.

        The pending correction, not the gap and not the elapsed time.
        Recovery is elapsed-time exponential against ``last_update_at``,
        so the move is ``gap x (1 - 2^-(elapsed/half_life))`` — it grows
        with both, and is zero if either is. That last part is what
        keeps this from being staleness wearing pressure's clothes
        (failure mode 3): energy already at its baseline reports zero no
        matter how long the worker has waited, where a staleness-shaped
        signal would climb to 1.0 with nothing to do.

        It also means the probe can safely under-report. A run that
        waited longer simply applies more of the curve, so a delayed
        admission catches up rather than losing ground, and the
        effective half-life does not depend on the cadence at all.

        Uses :func:`vitality_rhythm.peek_baseline` rather than
        ``current_baseline``: the latter rolls and persists today's
        rhythm on first touch, and a probe must not be the thing that
        decides it.
        """
        if not bool(getattr(self._agent, "vitality_enabled", True)):
            return WorkSignal(pressure=0.0, reason="disabled")
        try:
            local = timephrase.now()
            baseline, _rhythm = _vr.peek_baseline(
                self._chat_db,
                local,
                enabled=bool(
                    getattr(self._agent, "vitality_rhythm_enabled", True)
                ),
            )
            raw = self._chat_db.kv_get(_vit.KV_VITALITY)
        except Exception:
            log.debug("vitality worker demand probe failed", exc_info=True)
            return None
        state = _vit.deserialize(raw, baseline=baseline, now=local)
        new_state = _vit.step_recover(
            state, baseline, local, half_life_hours=self._half_life_hours(),
        )
        move = abs(float(new_state.energy) - float(state.energy))
        if move <= _MOVE_DEADBAND:
            return WorkSignal(pressure=0.0, reason="at baseline")
        gap = abs(float(state.energy) - float(baseline))
        return WorkSignal(
            pressure=min(1.0, move / _MOVE_SATURATION),
            reason=f"gap {gap:.2f}, move {move:.3f}",
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent, "vitality_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        try:
            now = timephrase.now()
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
                half_life_hours=self._half_life_hours(),
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

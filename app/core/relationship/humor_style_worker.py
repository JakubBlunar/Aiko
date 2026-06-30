"""HumorStyleDecayWorker — slow forgetting for K74.

Thin :class:`IdleWorker` that pulls the learned humor-style weights a
fraction of the way back toward uniform on each tick, so a register that
stops landing fades over a long half-life. Mirrors
[`AffectionStyleDecayWorker`](affection_style_worker.py) exactly — the
only path that moves the weights *toward* uniform (the per-turn learner
in ``post_turn_mixin`` and the K32 reaction booster in ``world_mixin``
only ever move them away).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import default_is_ready
from app.core.relationship import humor_style as _hs

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings


log = logging.getLogger("app.humor_style_worker")


class HumorStyleDecayWorker:
    """IdleWorker that decays the K74 weights toward uniform."""

    name = "humor_style_decay"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        settings: "AgentSettings",
    ) -> None:
        self._chat_db = chat_db
        self._settings = settings

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._settings,
                "humor_style_decay_interval_seconds",
                21600,
            )
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._settings, "humor_style_enabled", True)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._settings, "humor_style_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        half_life = float(
            getattr(
                self._settings, "humor_style_decay_half_life_days", 30.0,
            )
        )
        if half_life <= 0.0:
            return {"skipped": True, "reason": "decay_disabled"}
        try:
            stored = self._chat_db.kv_get(_hs.KV_HUMOR_STYLE)
        except Exception:
            log.debug("humor_style worker: kv_get failed", exc_info=True)
            return {"skipped": True, "reason": "kv_get_failed"}
        if not stored:
            return {"decayed": False, "reason": "empty"}

        state = _hs.deserialize(stored)
        now = datetime.now(timezone.utc)
        floor = float(getattr(self._settings, "humor_style_floor", 0.05))
        new_state = _hs.decay_toward_uniform(
            state, now, half_life_days=half_life, floor=floor,
        )
        moved = any(
            abs(new_state.weight_of(k) - state.weight_of(k)) > 1e-6
            for k in _hs.HUMOR_KINDS
        )
        if not moved:
            return {"decayed": False, "reason": "no_change"}
        try:
            self._chat_db.kv_set(_hs.KV_HUMOR_STYLE, _hs.serialize(new_state))
        except Exception:
            log.warning("humor_style worker: kv_set failed", exc_info=True)
            return {"skipped": True, "reason": "kv_set_failed"}
        log.info(
            "humor_style decayed: top=%s weights=%s",
            _hs.top_kind(new_state),
            {k: round(new_state.weight_of(k), 3) for k in _hs.HUMOR_KINDS},
        )
        return {"decayed": True, "top": _hs.top_kind(new_state)}


__all__ = ["HumorStyleDecayWorker"]

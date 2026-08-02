"""AffectionStyleDecayWorker — slow forgetting for J11.

Thin :class:`IdleWorker` that pulls the learned affection-style weights
a fraction of the way back toward uniform on each tick, so a preference
that stops being reinforced fades over a long half-life. Matches the
[`DayColorWorker`](../affect/day_color_worker.py) /
[`MemoryDecayWorker`](../memory/memory_decay_worker.py) shape exactly
(class-level ``name``, ``interval_seconds`` property,
``is_ready(now, last_run_at)``, ``run() -> dict``) so it slots into the
existing :class:`IdleWorkerScheduler` with no special handling.

The actual elapsed-time accounting lives in
:func:`affection_style.decay_toward_uniform` (reads ``state.updated_at``),
so running every 6h applies the right fraction whether the app was up
the whole time or just came back online after a few days. Storage is
the single ``aiko.affection_style`` kv_meta key — no schema change.

This is the *only* path that moves the weights toward uniform; the
per-turn learning in ``post_turn_mixin`` and the reaction booster in
``world_mixin`` only ever move them away from it.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import WorkSignal
from app.core.relationship import affection_style as _af
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings


log = logging.getLogger("app.affection_style_worker")


class AffectionStyleDecayWorker:
    """IdleWorker that decays the J11 weights toward uniform."""

    name = "affection_style_decay"

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
        # Floored at 60s in the AgentSettings parser so a buggy override
        # can't spin the scheduler.
        return float(
            getattr(
                self._settings,
                "affection_style_decay_interval_seconds",
                21600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        return bool(
            getattr(self._settings, "affection_style_enabled", True)
        )

    def _peek_decay(self) -> tuple[str | None, "_af.AffectionStyleState | None"]:
        """Dry-run one decay step. Returns ``(skip_reason, new_state)``.

        Exactly one of the two is ``None``. Shared by the probe and
        ``run()``, which is only safe because
        :func:`affection_style.decay_toward_uniform` returns a new state
        instead of mutating — the probe computes the same answer and
        throws it away, and only ``run()`` writes it back.
        """
        half_life = float(
            getattr(
                self._settings,
                "affection_style_decay_half_life_days",
                30.0,
            )
        )
        if half_life <= 0.0:
            return "decay_disabled", None

        try:
            stored = self._chat_db.kv_get(_af.KV_AFFECTION_STYLE)
        except Exception:
            log.debug("affection_style worker: kv_get failed", exc_info=True)
            return "kv_get_failed", None

        # Nothing learned yet -> nothing to decay. Avoids writing a
        # uniform row on every tick for a brand-new install.
        if not stored:
            return "empty", None

        state = _af.deserialize(stored)
        floor = float(getattr(self._settings, "affection_style_floor", 0.05))
        new_state = _af.decay_toward_uniform(
            state,
            timephrase.utcnow(),
            half_life_days=half_life,
            floor=floor,
        )

        # Detect a meaningful move so we don't churn kv_meta on a tick
        # where the elapsed time rounded to no change.
        moved = any(
            abs(new_state.weight_of(k) - state.weight_of(k)) > 1e-6
            for k in _af.AFFECTION_KINDS
        )
        if not moved:
            return "no_change", None
        return None, new_state

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Ask the decay maths whether the weights would actually move.

        Decay is elapsed-time exponential against ``state.updated_at``,
        not a fixed step per run, so admitting on pressure rather than
        on a fixed 6h cadence does not change the effective half-life —
        whenever the worker does run, it applies the whole elapsed
        interval.
        """
        if not bool(
            getattr(self._settings, "affection_style_enabled", True)
        ):
            return WorkSignal(pressure=0.0, reason="disabled")
        reason, new_state = self._peek_decay()
        if new_state is None:
            return WorkSignal(pressure=0.0, reason=reason or "no_change")
        return WorkSignal(pressure=1.0, reason="decay due")

    def run(self) -> dict[str, Any]:
        """Apply one decay step toward uniform.

        Best-effort: every failure path returns a stats dict rather than
        raising, so a transient ``kv`` hiccup doesn't burn the worker's
        retry budget. Only writes when the weights actually moved (the
        common "already uniform / no elapsed time" tick is a cheap
        read + compare).
        """
        if not bool(
            getattr(self._settings, "affection_style_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}

        reason, new_state = self._peek_decay()
        if new_state is None:
            if reason in ("decay_disabled", "kv_get_failed"):
                return {"skipped": True, "reason": reason}
            return {"decayed": False, "reason": reason}

        try:
            self._chat_db.kv_set(
                _af.KV_AFFECTION_STYLE, _af.serialize(new_state),
            )
        except Exception:
            log.warning(
                "affection_style worker: kv_set failed", exc_info=True,
            )
            return {"skipped": True, "reason": "kv_set_failed"}

        log.info(
            "affection_style decayed: top=%s weights=%s",
            _af.top_kind(new_state),
            {k: round(new_state.weight_of(k), 3) for k in _af.AFFECTION_KINDS},
        )
        return {"decayed": True, "top": _af.top_kind(new_state)}


__all__ = ["AffectionStyleDecayWorker"]

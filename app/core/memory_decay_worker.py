"""Memory salience decay worker (schema v8 / E1+E2).

Thin :class:`IdleWorker` that calls :meth:`MemoryStore.decay` with the
current :class:`MemorySettings` per-tier rates + revival coefficients.
The actual elapsed-time accounting lives inside ``decay()`` itself,
which reads ``memory.last_decay_run_at`` from the ``kv_meta`` table --
so running every hour applies 1/24 of the daily rate, and coming back
online after 3 days produces 3 days' worth (clamped to
``decay_max_catchup_days``).

Replaces the legacy ``SessionController._memory_decay_loop`` daemon
thread; consolidating into the scheduler means decay shares the same
quiet-window gate as the promotion worker.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory_store import MemoryStore
    from app.core.settings import MemorySettings


log = logging.getLogger("app.memory_decay_worker")


class MemoryDecayWorker:
    """IdleWorker wrapping :meth:`MemoryStore.decay`."""

    name = "memory_decay"

    def __init__(
        self,
        store: "MemoryStore",
        settings: "MemorySettings",
    ) -> None:
        self._store = store
        self._settings = settings

    @property
    def interval_seconds(self) -> float:
        return float(self._settings.decay_worker_interval_seconds)

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._settings.tiers_enabled:
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._settings.tiers_enabled:
            return {"skipped": True, "reason": "tiers_disabled"}
        rates = {
            "scratchpad": float(self._settings.decay_rate_scratchpad),
            "long_term": float(self._settings.decay_rate_long_term),
            "archive": float(self._settings.decay_rate_archive),
        }
        try:
            stats = self._store.decay(
                decay_rates=rates,
                revival_coefficient=float(self._settings.revival_coefficient),
                revival_decay_per_day=float(self._settings.revival_decay_per_day),
                max_catchup_days=float(self._settings.decay_max_catchup_days),
            )
        except Exception:
            log.warning("memory decay failed", exc_info=True)
            raise
        log.info("memory_decay sweep: %s", stats)
        return dict(stats) if isinstance(stats, dict) else {}


__all__ = ["MemoryDecayWorker"]

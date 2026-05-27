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
    """IdleWorker wrapping :meth:`MemoryStore.decay`.

    Also piggy-backs the F2 knowledge-gap expiry pass (90-day TTL on
    unresolved unpinned gaps) so we don't need a second worker just to
    sweep stale journal rows.
    """

    name = "memory_decay"

    def __init__(
        self,
        store: "MemoryStore",
        settings: "MemorySettings",
        *,
        knowledge_gap_store: "Any | None" = None,
    ) -> None:
        self._store = store
        self._settings = settings
        # F2: optional handle to the knowledge-gap store. Wired by
        # ``SessionController`` so the decay worker can also run the
        # 90-day expiry pass on the journal. ``None`` keeps tests and
        # lean deployments running without the extra hook.
        self._knowledge_gap_store = knowledge_gap_store

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
        # F2: piggyback gap expiry. Best-effort — if it fails, the
        # decay sweep result still counts as a successful tick.
        out: dict[str, Any] = dict(stats) if isinstance(stats, dict) else {}
        gap_store = self._knowledge_gap_store
        if gap_store is not None:
            try:
                pruned = gap_store.prune_expired()
                if pruned:
                    out["knowledge_gaps_expired"] = int(pruned)
                    log.info(
                        "memory_decay: expired %d stale knowledge gap(s)",
                        pruned,
                    )
            except Exception:
                log.debug("knowledge gap expiry failed", exc_info=True)
        return out


__all__ = ["MemoryDecayWorker"]

"""Compute-lane retention for the activity event tables (H33 shape 14)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal

if TYPE_CHECKING:
    from app.core.activity.store import ActivityStore

log = logging.getLogger("app.activity.prune_worker")

_INTERVAL_SECONDS = 6.0 * 3600.0
_SATURATION_ROWS = 500


class ActivityPruneWorker:
    """IdleWorker whose ``demand()`` is "old activity rows exist"."""

    name = "activity_prune"

    def __init__(
        self,
        store: "ActivityStore",
        *,
        keep_days_provider: Callable[[], int],
        interval_seconds: float = _INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._keep_days_provider = keep_days_provider
        self._interval = max(60.0, float(interval_seconds))

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        return self._store is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        keep_days = int(self._keep_days_provider() or 0)
        if keep_days <= 0:
            return WorkSignal(pressure=0.0, reason="retention disabled")
        stale = self._store.stale_event_count(keep_days)
        if stale <= 0:
            return WorkSignal(pressure=0.0, reason="no expired activity rows")
        return WorkSignal(
            pressure=min(1.0, stale / float(_SATURATION_ROWS)),
            reason="%d expired activity rows" % stale,
            needs_llm=False,
        )

    def run(self) -> dict[str, Any] | None:
        keep_days = int(self._keep_days_provider() or 0)
        result = self._store.prune(keep_days)
        log.info(
            "activity prune: events=%s sessions=%s keep_days=%s",
            result.get("events"),
            result.get("sessions"),
            keep_days,
        )
        return result

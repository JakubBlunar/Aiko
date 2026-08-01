"""Plant stage promotion worker (Aiko's garden).

Background :class:`IdleWorker` that walks every ``kind == "plant"`` item
in :class:`WorldStore` and asks :func:`world_store.promote_stage` whether
it's due to advance. Stages move slowly (hours per step; see
``_STAGE_MIN_AGE_HOURS`` in :mod:`app.core.world.world_store`) so wallclock
growth feels gentle rather than gamey.

The hourly ``interval_seconds`` is a heartbeat, not a cadence: the
:meth:`demand` probe counts how many plants would actually advance and
the scheduler decides from that (P36). The worker is cheap either way --
one SQLite UPDATE per promoted plant.

Notifications go out through the same ``world_updated`` WS path used by
manual edits via ``session._notify_world({"item": …})``; the UI updates
in place without a refetch.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal, pressure_from_count

if TYPE_CHECKING:
    from app.core.world.world_store import WorldStore


log = logging.getLogger("app.plant_growth_worker")


class PlantGrowthWorker:
    """Promote one stage per due plant per sweep."""

    name = "plant_growth"

    def __init__(
        self,
        store: "WorldStore",
        *,
        notify: Callable[[dict[str, Any]], None] | None = None,
        interval_seconds: float = 3600.0,
    ) -> None:
        self._store = store
        self._notify = notify
        self._interval_seconds = float(interval_seconds)

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        # No hard veto: this worker has no feature flag and no cold-start
        # guard. The interval that used to gate here is now the heartbeat
        # and the demand() probe decides the rest (P36).
        return True

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """How many plants would actually advance a stage right now.

        The hourly sweep existed because there was no way to ask; it
        scanned every plant to usually promote none. This is the same
        walk minus the writes -- ``stage_promotion_due`` is the
        read-only half of ``promote_stage``, which matters because
        ``list_items`` returns live mirror references. No LLM.
        """
        from app.core.world.world_store import stage_promotion_due

        try:
            plants = self._store.list_items(kind="plant")
        except Exception:
            log.debug("plant growth: demand list_items failed", exc_info=True)
            return None
        due = 0
        for item in plants:
            try:
                if stage_promotion_due(item, now=now) is not None:
                    due += 1
            except Exception:
                log.debug(
                    "plant growth: demand probe raised for id=%s",
                    getattr(item, "id", "?"), exc_info=True,
                )
        return WorkSignal(
            pressure=pressure_from_count(due, saturation=3),
            reason=f"{due} due",
        )

    def run(self) -> dict[str, Any]:
        from app.core.world.world_store import promote_stage

        promoted: list[dict[str, Any]] = []
        scanned = 0
        try:
            plants = self._store.list_items(kind="plant")
        except Exception:
            log.debug("plant growth: list_items failed", exc_info=True)
            return {"scanned": 0, "promoted": 0}
        for item in plants:
            scanned += 1
            try:
                next_stage = promote_stage(item)
            except Exception:
                log.debug(
                    "plant growth: promote_stage raised for id=%s",
                    item.id,
                    exc_info=True,
                )
                continue
            if next_stage is None:
                continue
            try:
                updated = self._store.update_item(item.id, state=dict(item.state))
            except Exception:
                log.debug(
                    "plant growth: update_item failed for id=%s",
                    item.id,
                    exc_info=True,
                )
                continue
            if updated is None:
                continue
            payload = updated.to_dict()
            promoted.append(
                {
                    "id": int(item.id),
                    "name": item.name,
                    "stage": next_stage,
                }
            )
            if self._notify is not None:
                try:
                    self._notify({"item": payload})
                except Exception:
                    log.debug(
                        "plant growth: notify raised for id=%s",
                        item.id,
                        exc_info=True,
                    )
        result = {"scanned": scanned, "promoted": len(promoted), "items": promoted}
        if promoted:
            log.info("plant_growth sweep: %s", result)
        return result


__all__ = ["PlantGrowthWorker"]

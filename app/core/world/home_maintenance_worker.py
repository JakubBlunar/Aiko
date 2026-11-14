"""Passive household cleanup; no invented activity, memory write, or model call."""
from __future__ import annotations

from app.core.proactive.idle_worker import WorkSignal


class HomeMaintenanceWorker:
    name = "home_maintenance"
    interval_seconds = 60.0
    sleep_policy = "continue"

    def __init__(self, world, notify):
        self._world, self._notify = world, notify

    def is_ready(self, *, now, last_run_at):
        return True

    def demand(self, *, now, last_run_at):
        due = self._world.home_lifecycle_maintenance_due(now=now)
        return WorkSignal(pressure=0.3 if due else 0.0, reason="bin_due" if due else "caught_up")

    def run(self):
        result = self._world.maintain_home_lifecycle()
        if result["discarded"]:
            self._notify({"snapshot": self._world.snapshot()})
        return result

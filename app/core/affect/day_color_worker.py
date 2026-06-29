"""DayColorWorker — canonical roll path for K27.

Thin :class:`IdleWorker` that rolls a fresh palette entry once per
local day. Matches the [`MemoryDecayWorker`](../memory/memory_decay_worker.py)
shape exactly so it slots into the existing :class:`IdleWorkerScheduler`
with no special handling: class-level ``name``, ``interval_seconds``
property, ``is_ready(now, last_run_at)``, ``run() -> dict``.

The worker is the **canonical** path — it fires once an hour and is
the right place for the daily roll because (a) it shares the same
quiet-window gate as the rest of the scheduler, and (b) the per-tick
budget keeps it from competing with heavier work like F5 / belief
extraction. But because :class:`IdleWorkerScheduler` only runs while
the user is idle, a user who wakes Aiko at 08:30 and starts chatting
immediately could read yesterday's colour until the next idle
window. That's why
[`_render_day_color_block`](../session/inner_life_providers_mixin.py)
also has a cheap lazy fallback that runs the same
:func:`day_color.roll_for_today` when it sees stale state. Hybrid
design: this worker is the regular cadence, the provider is the
seatbelt for the first-turn-after-midnight case.

Storage on ``kv_meta`` (no schema change): two keys --
``aiko.day_color`` (the palette name) and ``aiko.day_color_set_at``
(the ISO timestamp of the roll). Same shape as
:data:`MemoryStore._KV_LAST_DECAY`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.affect import day_color
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings


log = logging.getLogger("app.day_color_worker")


# ``kv_meta`` keys are namespaced under ``aiko.*`` to keep K27 state
# from colliding with the existing ``memory.*`` (MemoryStore) and
# ``goals.*`` (onboarding seed) namespaces. Exported so the provider
# and the MCP debug tool can share the exact same key strings.
KV_DAY_COLOR = "aiko.day_color"
KV_DAY_COLOR_SET_AT = "aiko.day_color_set_at"


class DayColorWorker:
    """IdleWorker that rolls a fresh daily colour at local midnight.

    Cheap on the common-case "today's roll is already set" tick:
    one ``kv_get`` read + one date comparison + a structured INFO log
    line announcing the no-op. Only writes when ``is_stale`` says the
    stored date isn't today.
    """

    name = "day_color"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        settings: "AgentSettings",
    ) -> None:
        self._chat_db = chat_db
        self._settings = settings

    def _weather_weights(self) -> "dict[str, float] | None":
        """H11: bias the daily roll toward the real-world weather.

        Returns ``None`` (uniform roll) when weather sync is off or no
        snapshot is cached, so the day-colour is unbiased on installs
        without the weather feature. Best-effort — any failure falls
        back to uniform.
        """
        if not bool(getattr(self._settings, "weather_sync_enabled", False)):
            return None
        try:
            from app.core.world.weather_worker import load_weather_snapshot

            snap = load_weather_snapshot(self._chat_db)
            if not snap:
                return None
            return day_color.weather_palette_weights(snap.get("condition"))
        except Exception:
            return None

    @property
    def interval_seconds(self) -> float:
        # Hourly check; the actual roll only fires once per local day.
        # Floored at 60s in :class:`AgentSettings` parser so a buggy
        # override can't spin the scheduler.
        return float(
            getattr(
                self._settings, "day_color_check_interval_seconds", 3600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._settings, "day_color_enabled", True)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        """Roll if today's colour isn't set; otherwise no-op.

        Returns a stable-shape stats dict so the scheduler's per-tick
        ``names=...`` summary stays grep-friendly. Best-effort: any
        failure path returns a ``{"error": ...}`` dict rather than
        raising, otherwise the IdleWorkerScheduler would burn the
        worker's retry budget on a transient ``kv_set`` hiccup.
        """
        if not bool(getattr(self._settings, "day_color_enabled", True)):
            return {"skipped": True, "reason": "disabled"}

        try:
            stored_at = self._chat_db.kv_get(KV_DAY_COLOR_SET_AT)
        except Exception:
            log.debug("day_color worker: kv_get failed", exc_info=True)
            return {"skipped": True, "reason": "kv_get_failed"}

        now = datetime.now().astimezone()
        if not day_color.is_stale(stored_at, now):
            return {"rolled": False, "reason": "fresh"}

        # Capture the prior name purely for the log line -- no
        # behaviour depends on it.
        try:
            prev = self._chat_db.kv_get(KV_DAY_COLOR)
        except Exception:
            prev = None

        try:
            chosen = day_color.roll_for_today(
                now=now, weights=self._weather_weights(),
            )
        except Exception:
            log.warning("day_color worker: roll failed", exc_info=True)
            return {"skipped": True, "reason": "roll_failed"}

        try:
            self._chat_db.kv_set(KV_DAY_COLOR, chosen.name)
            self._chat_db.kv_set(KV_DAY_COLOR_SET_AT, now.isoformat())
        except Exception:
            log.warning(
                "day_color worker: kv_set failed for %s", chosen.name,
                exc_info=True,
            )
            return {"skipped": True, "reason": "kv_set_failed"}

        log.info(
            "day_color rolled: name=%s prev=%s set_at=%s",
            chosen.name, prev, now.isoformat(),
        )
        return {
            "rolled": True,
            "name": chosen.name,
            "prev": prev,
        }


__all__ = ["DayColorWorker", "KV_DAY_COLOR", "KV_DAY_COLOR_SET_AT"]

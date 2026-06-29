"""WeatherWorker -- H11 passive ambient weather feed.

Thin :class:`IdleWorker` that pulls the user's home-location conditions
on a low cadence during quiet windows and stashes a normalized snapshot
in ``kv_meta`` for the ambient prompt cue + persona overlay to read,
fanning a ``weather_updated`` WS frame out via the injected ``notify``
callback (mirrors :class:`~app.core.world.room_evolution_worker` workers
that receive ``notify=self._notify_*`` at registration).

Storage on ``kv_meta`` (no schema change): ``aiko.weather_snapshot``
(the JSON snapshot) + ``aiko.weather_fetched_at`` (ISO timestamp). The
on-demand brain tools (:mod:`app.llm.tools.weather`) do NOT touch this
cache -- they fetch live and never persist.

Every failure path is swallowed and logged at debug -- the worst case
is a stale or missing snapshot, never a crashed scheduler tick.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.llm.weather.providers import WeatherProvider


log = logging.getLogger("app.weather_worker")


# kv_meta keys, namespaced under ``aiko.*`` like the K27 day-colour state.
KV_WEATHER_SNAPSHOT = "aiko.weather_snapshot"
KV_WEATHER_FETCHED_AT = "aiko.weather_fetched_at"


def persist_weather_snapshot(chat_db: "ChatDatabase", snapshot: dict[str, Any]) -> None:
    """Write a snapshot dict to ``kv_meta`` (best-effort)."""
    try:
        chat_db.kv_set(KV_WEATHER_SNAPSHOT, json.dumps(snapshot, default=str))
        chat_db.kv_set(
            KV_WEATHER_FETCHED_AT,
            str(snapshot.get("fetched_at")
                or datetime.now().astimezone().isoformat(timespec="seconds")),
        )
    except Exception:
        log.debug("persist_weather_snapshot failed", exc_info=True)


def load_weather_snapshot(chat_db: "ChatDatabase") -> "dict[str, Any] | None":
    """Read the last-persisted snapshot from ``kv_meta``, or ``None``."""
    try:
        raw = chat_db.kv_get(KV_WEATHER_SNAPSHOT)
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except Exception:
        return None
    return blob if isinstance(blob, dict) else None


class WeatherWorker:
    """IdleWorker that refreshes the home-location weather snapshot."""

    name = "weather"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        provider_getter: Callable[[], "WeatherProvider"],
        home_provider: Callable[[], "tuple[float, float, str] | None"],
        units_provider: Callable[[], str],
        enabled_provider: Callable[[], bool],
        interval_provider: Callable[[], float],
        notify: Callable[[dict[str, Any]], None] | None = None,
        seasonal_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._provider_getter = provider_getter
        self._home_provider = home_provider
        self._units_provider = units_provider
        self._enabled_provider = enabled_provider
        self._interval_provider = interval_provider
        self._notify = notify
        self._seasonal_hook = seasonal_hook

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        try:
            return max(60.0, float(self._interval_provider()))
        except Exception:
            return 1800.0

    def _enabled(self) -> bool:
        try:
            return bool(self._enabled_provider())
        except Exception:
            return False

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        try:
            if self._home_provider() is None:
                return False
        except Exception:
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"fetched": 0, "disabled": True}
        try:
            home = self._home_provider()
        except Exception:
            home = None
        if home is None:
            return {"fetched": 0, "skipped_no_location": True}
        lat, lon, label = home
        try:
            units = self._units_provider()
        except Exception:
            units = "metric"
        try:
            snap = self._provider_getter().current(
                lat, lon, units=units, location_label=label,
            )
        except Exception:
            log.debug("weather fetch failed", exc_info=True)
            return {"fetched": 0, "errored": True}

        blob = snap.to_dict()
        persist_weather_snapshot(self._chat_db, blob)
        if self._notify is not None:
            try:
                self._notify(blob)
            except Exception:
                log.debug("weather notify failed", exc_info=True)
        if self._seasonal_hook is not None:
            try:
                self._seasonal_hook(blob)
            except Exception:
                log.debug("weather seasonal hook failed", exc_info=True)
        log.info(
            "weather fetched: condition=%s temp=%.1f%s season=%s loc=%s",
            blob.get("condition"), blob.get("temperature", 0.0),
            blob.get("temp_unit", "C"), blob.get("season"), label or "?",
        )
        return {
            "fetched": 1,
            "condition": blob.get("condition"),
            "season": blob.get("season"),
        }


__all__ = [
    "WeatherWorker",
    "KV_WEATHER_SNAPSHOT",
    "KV_WEATHER_FETCHED_AT",
    "persist_weather_snapshot",
    "load_weather_snapshot",
]

"""H11 on-demand weather tools (``get_weather`` / ``get_forecast``).

Synchronous brain-lane tools so Aiko can answer "what's the forecast?"
for the configured home location or any named city. Both resolve a
location in this order:

1. an explicit ``location`` argument -> geocoded at call time via the
   decoupled :class:`~app.llm.weather.Geocoder`;
2. the configured home ``latitude`` / ``longitude`` (from
   ``weather.*`` settings);
3. otherwise a :class:`ToolError` telling Aiko to ask which place.

A single fast HTTP GET, so these stay on the brain lane (no task
orchestration). They never touch the ambient ``kv_meta`` cache -- that's
the passive :class:`~app.core.world.weather_worker.WeatherWorker`'s job.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.llm.tools.base import Tool, ToolError, ToolSchema


log = logging.getLogger("app.tools.weather")


# Resolution helpers shared by both tools. ``HomeProvider`` returns the
# configured home ``(lat, lon, label)`` or ``None``.
HomeProvider = Callable[[], "tuple[float, float, str] | None"]


def _resolve_location(
    arguments: dict[str, Any],
    *,
    geocoder: Any,
    home_provider: HomeProvider,
) -> "tuple[float, float, str]":
    """Resolve an explicit ``location`` arg or fall back to home."""
    name = str(arguments.get("location") or "").strip()
    if name:
        try:
            place = geocoder.resolve(name)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(
                f"couldn't look up '{name}' right now: {exc}"
            ) from exc
        if place is None:
            raise ToolError(
                f"I couldn't find a place called '{name}'. "
                "Could you double-check the spelling or try a bigger nearby city?"
            )
        return (place.latitude, place.longitude, place.label or name)
    home = home_provider()
    if home is None:
        raise ToolError(
            "No location is set, so I don't know where to check. "
            "Tell me a city (or set your home location in settings)."
        )
    lat, lon, label = home
    return (lat, lon, label or "your area")


class GetWeatherTool:
    """Current conditions for the home location or a named place."""

    def __init__(
        self,
        *,
        provider: Any,
        geocoder: Any,
        home_provider: HomeProvider,
        units_provider: Callable[[], str],
    ) -> None:
        self._provider = provider
        self._geocoder = geocoder
        self._home_provider = home_provider
        self._units_provider = units_provider

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_weather",
            description=(
                "Get the CURRENT real-world weather. Call this when the user "
                "asks what the weather is like right now -- for their place "
                "(leave 'location' empty to use their configured home) or for "
                "any named city (pass 'location'). Returns temperature, "
                "conditions, humidity and wind."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": (
                            "Optional city/place name, e.g. 'Tokyo' or "
                            "'Paris, France'. Leave empty for the user's "
                            "configured home location."
                        ),
                    },
                },
                "required": [],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        lat, lon, label = _resolve_location(
            arguments, geocoder=self._geocoder, home_provider=self._home_provider,
        )
        units = (self._units_provider() or "metric")
        try:
            snap = self._provider.current(
                lat, lon, units=units, location_label=label,
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"weather lookup failed: {exc}") from exc
        return json.dumps(snap.to_dict(), ensure_ascii=False)


class GetForecastTool:
    """Multi-day forecast for the home location or a named place."""

    def __init__(
        self,
        *,
        provider: Any,
        geocoder: Any,
        home_provider: HomeProvider,
        units_provider: Callable[[], str],
    ) -> None:
        self._provider = provider
        self._geocoder = geocoder
        self._home_provider = home_provider
        self._units_provider = units_provider

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_forecast",
            description=(
                "Get a multi-day real-world weather FORECAST. Call this when "
                "the user asks about the weather for the coming days / this "
                "week / tomorrow -- for their configured home (leave "
                "'location' empty) or any named city (pass 'location'). "
                "Returns daily high/low, conditions and rain chance."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": (
                            "Optional city/place name. Leave empty for the "
                            "user's configured home location."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "How many days to forecast (1-7). Defaults to 3."
                        ),
                    },
                },
                "required": [],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        lat, lon, label = _resolve_location(
            arguments, geocoder=self._geocoder, home_provider=self._home_provider,
        )
        try:
            days = int(arguments.get("days") or 3)
        except (TypeError, ValueError):
            days = 3
        days = max(1, min(7, days))
        units = (self._units_provider() or "metric")
        try:
            fc = self._provider.forecast(
                lat, lon, days=days, units=units, location_label=label,
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"forecast lookup failed: {exc}") from exc
        return json.dumps(fc.to_dict(), ensure_ascii=False)


def build_weather_tools(session: Any) -> list[Tool]:
    """Build the weather tool pair bound to a ``SessionController``.

    Reads the shared provider/geocoder + home location off the session's
    :class:`~app.core.session.weather_mixin.WeatherMixin` helpers, so a
    live ``reconfigure_weather`` is picked up on the next turn's registry
    rebuild without re-instantiating the tools mid-loop.
    """
    provider = session._get_weather_provider()
    geocoder = session._get_geocoder()
    home_provider = session._weather_home
    units_provider = session._weather_units
    return [
        GetWeatherTool(
            provider=provider, geocoder=geocoder,
            home_provider=home_provider, units_provider=units_provider,
        ),
        GetForecastTool(
            provider=provider, geocoder=geocoder,
            home_provider=home_provider, units_provider=units_provider,
        ),
    ]


__all__ = [
    "GetWeatherTool",
    "GetForecastTool",
    "build_weather_tools",
]

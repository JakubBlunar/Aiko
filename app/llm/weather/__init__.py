"""Pluggable weather + geocoding backends.

H11 "real-world co-location" feeds two consumers from one swappable
backend layer: the passive ambient
:class:`~app.core.world.weather_worker.WeatherWorker` and the on-demand
brain tools in :mod:`app.llm.tools.weather`. The weather and geocoding
backends are deliberately separate protocols so either can be swapped
without touching the other -- see :mod:`app.llm.weather.providers`.
"""
from __future__ import annotations

from app.llm.weather.providers import (
    Forecast,
    ForecastDay,
    Geocoder,
    GeoPlace,
    OpenMeteoGeocoder,
    OpenMeteoProvider,
    WeatherProvider,
    WeatherSnapshot,
    build_geocoder,
    build_weather_provider,
    condition_from_wmo,
    season_for,
)

__all__ = [
    "Forecast",
    "ForecastDay",
    "Geocoder",
    "GeoPlace",
    "OpenMeteoGeocoder",
    "OpenMeteoProvider",
    "WeatherProvider",
    "WeatherSnapshot",
    "build_geocoder",
    "build_weather_provider",
    "condition_from_wmo",
    "season_for",
]

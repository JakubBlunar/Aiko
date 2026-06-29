"""Pluggable weather + geocoding backends.

H11 "real-world co-location" has two consumers: the passive ambient
:class:`~app.core.world.weather_worker.WeatherWorker` (low-frequency fetch
of the user's home sky -> ``kv_meta`` snapshot + prompt cue + persona
overlay) and the on-demand brain tools
:class:`~app.llm.tools.weather.GetWeatherTool` /
:class:`~app.llm.tools.weather.GetForecastTool` ("what's the forecast in
Tokyo?"). This package factors both network calls behind small protocols so
the backend can be swapped without touching either consumer.

Two intentionally **decoupled** protocols:

* :class:`WeatherProvider` operates purely on latitude/longitude. It never
  resolves place names, so swapping the weather backend (Open-Meteo ->
  some keyed provider) leaves geocoding untouched.
* :class:`Geocoder` turns a free-text place name into a :class:`GeoPlace`
  (lat/lon + label). The home city is geocoded once at settings-save time
  and the coordinates are cached; the brain tool geocodes arbitrary names
  at call time. Swapping the geocoder leaves the weather backend untouched.

Open-Meteo is the keyless default for both (free, no-auth HTTP GETs, like
DuckDuckGo for search). ``api_key`` / ``api_key_env`` are carried for a
future keyed provider and resolved with the same precedence helper as
search.
"""
from __future__ import annotations

import logging
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.infra.settings import WeatherSettings


log = logging.getLogger("app.llm.weather")


_OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Administrative-noise words dropped from a geocoding disambiguation hint
# so "Zilina region" / "okres Zilina" matches the bare admin name. Both
# English and a few common Slovak/Czech/German terms (the user base skews
# Central-European) are covered; unknown words simply don't match, which
# is harmless.
_GEO_STOPWORDS = frozenset({
    "region", "district", "province", "county", "state", "area", "the",
    "okres", "kraj", "oblast", "obec", "mesto",  # sk / cz
    "bezirk", "kreis", "land",                    # de
    "departement", "departement",                 # fr
})

# Coarse condition buckets. These drive both the prompt cue wording and the
# persona-window overlay (rain/snow/sun/fog/cloud/storm), so keep the set
# small and stable -- a new backend maps its native codes onto these.
CONDITION_CLEAR = "clear"
CONDITION_CLOUDY = "cloudy"
CONDITION_FOG = "fog"
CONDITION_RAIN = "rain"
CONDITION_SNOW = "snow"
CONDITION_STORM = "storm"
CONDITIONS = (
    CONDITION_CLEAR,
    CONDITION_CLOUDY,
    CONDITION_FOG,
    CONDITION_RAIN,
    CONDITION_SNOW,
    CONDITION_STORM,
)

UNITS_METRIC = "metric"
UNITS_IMPERIAL = "imperial"

# WMO weather interpretation codes -> coarse condition bucket.
# https://open-meteo.com/en/docs (weather_code table).
_WMO_TO_CONDITION: dict[int, str] = {
    0: CONDITION_CLEAR,
    1: CONDITION_CLEAR,
    2: CONDITION_CLOUDY,
    3: CONDITION_CLOUDY,
    45: CONDITION_FOG,
    48: CONDITION_FOG,
    51: CONDITION_RAIN,
    53: CONDITION_RAIN,
    55: CONDITION_RAIN,
    56: CONDITION_RAIN,
    57: CONDITION_RAIN,
    61: CONDITION_RAIN,
    63: CONDITION_RAIN,
    65: CONDITION_RAIN,
    66: CONDITION_RAIN,
    67: CONDITION_RAIN,
    71: CONDITION_SNOW,
    73: CONDITION_SNOW,
    75: CONDITION_SNOW,
    77: CONDITION_SNOW,
    80: CONDITION_RAIN,
    81: CONDITION_RAIN,
    82: CONDITION_RAIN,
    85: CONDITION_SNOW,
    86: CONDITION_SNOW,
    95: CONDITION_STORM,
    96: CONDITION_STORM,
    99: CONDITION_STORM,
}

# Short human descriptions per WMO code for the brain tool / prompt cue.
_WMO_TO_DESCRIPTION: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def condition_from_wmo(code: int | None) -> str:
    """Map a WMO weather code onto a coarse :data:`CONDITIONS` bucket."""
    if code is None:
        return CONDITION_CLOUDY
    try:
        return _WMO_TO_CONDITION.get(int(code), CONDITION_CLOUDY)
    except (TypeError, ValueError):
        return CONDITION_CLOUDY


def describe_wmo(code: int | None) -> str:
    """Short human description for a WMO weather code."""
    if code is None:
        return "unknown"
    try:
        return _WMO_TO_DESCRIPTION.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def season_for(latitude: float, now: datetime | None = None) -> str:
    """Meteorological season for a latitude (hemisphere-aware).

    Northern hemisphere: Dec-Feb winter, Mar-May spring, Jun-Aug summer,
    Sep-Nov autumn. Southern hemisphere is shifted by six months.
    """
    moment = now or datetime.now()
    month = moment.month
    northern = (
        "winter", "winter", "spring", "spring", "spring", "summer",
        "summer", "summer", "autumn", "autumn", "autumn", "winter",
    )
    season = northern[month - 1]
    if latitude < 0:
        flip = {
            "winter": "summer",
            "summer": "winter",
            "spring": "autumn",
            "autumn": "spring",
        }
        season = flip[season]
    return season


def temp_unit_symbol(units: str) -> str:
    return "F" if str(units).lower() == UNITS_IMPERIAL else "C"


# ── normalized result shapes ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GeoPlace:
    """One resolved location (backend-neutral)."""

    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""
    timezone: str = ""

    @property
    def label(self) -> str:
        """Coarse human label, city-granularity only (never an address)."""
        parts = [p for p in (self.name, self.admin1, self.country) if p]
        # Drop a duplicated admin1==name (e.g. "Tokyo, Tokyo").
        deduped: list[str] = []
        for p in parts:
            if p not in deduped:
                deduped.append(p)
        return ", ".join(deduped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "country": self.country,
            "admin1": self.admin1,
            "timezone": self.timezone,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class WeatherSnapshot:
    """Current conditions at one location (backend-neutral)."""

    condition: str
    description: str
    temperature: float
    apparent_temperature: float
    humidity: int
    wind_speed: float
    is_day: bool
    weather_code: int
    season: str
    units: str
    location_label: str
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "description": self.description,
            "temperature": self.temperature,
            "apparent_temperature": self.apparent_temperature,
            "humidity": self.humidity,
            "wind_speed": self.wind_speed,
            "is_day": self.is_day,
            "weather_code": self.weather_code,
            "season": self.season,
            "units": self.units,
            "temp_unit": temp_unit_symbol(self.units),
            "location_label": self.location_label,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> "WeatherSnapshot":
        return cls(
            condition=str(blob.get("condition") or CONDITION_CLOUDY),
            description=str(blob.get("description") or ""),
            temperature=float(blob.get("temperature") or 0.0),
            apparent_temperature=float(
                blob.get("apparent_temperature")
                if blob.get("apparent_temperature") is not None
                else blob.get("temperature") or 0.0
            ),
            humidity=int(blob.get("humidity") or 0),
            wind_speed=float(blob.get("wind_speed") or 0.0),
            is_day=bool(blob.get("is_day", True)),
            weather_code=int(blob.get("weather_code") or 0),
            season=str(blob.get("season") or ""),
            units=str(blob.get("units") or UNITS_METRIC),
            location_label=str(blob.get("location_label") or ""),
            fetched_at=str(blob.get("fetched_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class ForecastDay:
    """One day of forecast (backend-neutral)."""

    date: str
    condition: str
    description: str
    temp_max: float
    temp_min: float
    precipitation_probability: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "condition": self.condition,
            "description": self.description,
            "temp_max": self.temp_max,
            "temp_min": self.temp_min,
            "precipitation_probability": self.precipitation_probability,
        }


@dataclass(frozen=True, slots=True)
class Forecast:
    """Multi-day forecast at one location."""

    location_label: str
    units: str
    days: list[ForecastDay] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_label": self.location_label,
            "units": self.units,
            "temp_unit": temp_unit_symbol(self.units),
            "days": [d.to_dict() for d in self.days],
        }


# ── protocols ───────────────────────────────────────────────────────────


@runtime_checkable
class WeatherProvider(Protocol):
    """A weather backend keyed purely on latitude/longitude."""

    def current(
        self, latitude: float, longitude: float, *, units: str = UNITS_METRIC,
        location_label: str = "",
    ) -> WeatherSnapshot:
        """Return the current conditions at a coordinate."""
        ...

    def forecast(
        self, latitude: float, longitude: float, *, days: int = 3,
        units: str = UNITS_METRIC, location_label: str = "",
    ) -> Forecast:
        """Return an ``days``-day forecast at a coordinate."""
        ...


@runtime_checkable
class Geocoder(Protocol):
    """A place-name -> coordinate backend (decoupled from the weather one)."""

    def resolve(self, name: str) -> "GeoPlace | None":
        """Resolve a free-text place name to a :class:`GeoPlace` or ``None``."""
        ...


# ── Open-Meteo (keyless default) ────────────────────────────────────────


class OpenMeteoProvider:
    """Open-Meteo current + forecast (keyless ``/v1/forecast``)."""

    name = "open_meteo"

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout = max(1.0, float(timeout_seconds))

    def _units_params(self, units: str) -> dict[str, str]:
        if str(units).lower() == UNITS_IMPERIAL:
            return {
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            }
        return {}

    def current(
        self, latitude: float, longitude: float, *, units: str = UNITS_METRIC,
        location_label: str = "",
    ) -> WeatherSnapshot:
        import requests

        params = {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "is_day,weather_code,wind_speed_10m"
            ),
            "timezone": "auto",
        }
        params.update(self._units_params(units))
        resp = requests.get(
            _OPEN_METEO_FORECAST_URL, params=params, timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("open-meteo: unexpected response body")
        cur = body.get("current")
        if not isinstance(cur, dict):
            raise ValueError("open-meteo: missing current block")
        code = cur.get("weather_code")
        return WeatherSnapshot(
            condition=condition_from_wmo(code),
            description=describe_wmo(code),
            temperature=float(cur.get("temperature_2m") or 0.0),
            apparent_temperature=float(
                cur.get("apparent_temperature")
                if cur.get("apparent_temperature") is not None
                else cur.get("temperature_2m") or 0.0
            ),
            humidity=int(cur.get("relative_humidity_2m") or 0),
            wind_speed=float(cur.get("wind_speed_10m") or 0.0),
            is_day=bool(cur.get("is_day", 1)),
            weather_code=int(code or 0),
            season=season_for(float(latitude)),
            units=str(units).lower(),
            location_label=location_label,
            fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def forecast(
        self, latitude: float, longitude: float, *, days: int = 3,
        units: str = UNITS_METRIC, location_label: str = "",
    ) -> Forecast:
        import requests

        n = max(1, min(16, int(days)))
        params = {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "timezone": "auto",
            "forecast_days": n,
        }
        params.update(self._units_params(units))
        resp = requests.get(
            _OPEN_METEO_FORECAST_URL, params=params, timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("open-meteo: unexpected response body")
        daily = body.get("daily")
        if not isinstance(daily, dict):
            raise ValueError("open-meteo: missing daily block")
        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_probability_max") or []
        out: list[ForecastDay] = []
        for i, date in enumerate(dates):
            code = codes[i] if i < len(codes) else None
            out.append(
                ForecastDay(
                    date=str(date),
                    condition=condition_from_wmo(code),
                    description=describe_wmo(code),
                    temp_max=float(highs[i]) if i < len(highs) and highs[i] is not None else 0.0,
                    temp_min=float(lows[i]) if i < len(lows) and lows[i] is not None else 0.0,
                    precipitation_probability=int(precip[i]) if i < len(precip) and precip[i] is not None else 0,
                )
            )
        return Forecast(
            location_label=location_label,
            units=str(units).lower(),
            days=out,
        )


def _geo_norm(text: str) -> str:
    """Lower-case + strip diacritics so "Žilina" matches "Zilina"."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


class OpenMeteoGeocoder:
    """Open-Meteo geocoding (keyless ``/v1/search``).

    Accepts a plain place name ("Tokyo") or a comma-separated query that
    carries region / country disambiguation hints ("Kamenná Poruba,
    Žilina, Slovakia"). The leading segment is the name sent to the API
    (which matches on name only); the trailing segments are matched
    locally against each candidate's admin levels + country so the right
    village wins when several share a name in different regions.
    """

    name = "open_meteo"

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout = max(1.0, float(timeout_seconds))

    def resolve(self, name: str) -> "GeoPlace | None":
        import requests

        query = (name or "").strip()
        if not query:
            return None
        # Split "name, region, country" -> search the bare name, keep the
        # rest as disambiguation hints.
        parts = [p.strip() for p in query.split(",") if p.strip()]
        search_name = parts[0] if parts else query
        hints = parts[1:]
        # Pull several candidates when we have hints so the local filter
        # has something to choose between; a bare name keeps the cheap
        # single-result request.
        count = 10 if hints else 1
        params = {
            "name": search_name,
            "count": count,
            "language": "en",
            "format": "json",
        }
        resp = requests.get(
            _OPEN_METEO_GEOCODE_URL, params=params, timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            return None
        results = body.get("results")
        if not isinstance(results, list) or not results:
            return None
        top = self._pick_best(results, hints)
        if not isinstance(top, dict):
            return None
        try:
            return GeoPlace(
                name=str(top.get("name") or search_name),
                latitude=float(top.get("latitude")),
                longitude=float(top.get("longitude")),
                country=str(top.get("country") or ""),
                admin1=str(top.get("admin1") or ""),
                timezone=str(top.get("timezone") or ""),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pick_best(
        results: list[Any], hints: list[str],
    ) -> "dict[str, Any] | None":
        """Choose the candidate best matching the region/country hints.

        Falls back to the first (most-relevant, population-ordered)
        result when there are no usable hints or nothing matches.
        """
        cands = [r for r in results if isinstance(r, dict)]
        if not cands:
            return None
        if not hints:
            return cands[0]
        # Flatten hints into matchable words, dropping admin-noise words.
        hint_words: list[str] = []
        for h in hints:
            for w in _geo_norm(h).split():
                if len(w) >= 3 and w not in _GEO_STOPWORDS:
                    hint_words.append(w)
        if not hint_words:
            return cands[0]
        best = cands[0]
        best_score = -1
        for r in cands:
            haystack = _geo_norm(
                " ".join(
                    str(r.get(k) or "")
                    for k in (
                        "admin1", "admin2", "admin3", "admin4",
                        "country", "country_code",
                    )
                )
            )
            score = sum(1 for w in hint_words if w in haystack)
            if score > best_score:
                best_score = score
                best = r
        return best


# ── factory ─────────────────────────────────────────────────────────────


def resolve_api_key(api_key: str, api_key_env: str) -> str:
    """Resolve a credential: explicit value wins, else the named env var."""
    explicit = (api_key or "").strip()
    if explicit:
        return explicit
    env_name = (api_key_env or "").strip()
    if env_name:
        return (os.environ.get(env_name, "") or "").strip()
    return ""


def build_weather_provider(settings: "WeatherSettings | None") -> WeatherProvider:
    """Pick a weather provider from settings.

    Open-Meteo is the only backend today (keyless). A future keyed
    provider would branch here on ``settings.provider``, mirroring
    :func:`app.llm.search.build_search_provider`.
    """
    timeout = 10.0
    if settings is not None:
        timeout = float(getattr(settings, "timeout_seconds", 10.0))
    return OpenMeteoProvider(timeout_seconds=timeout)


def build_geocoder(settings: "WeatherSettings | None") -> Geocoder:
    """Pick a geocoder from settings (decoupled from the weather provider)."""
    timeout = 10.0
    if settings is not None:
        timeout = float(getattr(settings, "timeout_seconds", 10.0))
    return OpenMeteoGeocoder(timeout_seconds=timeout)


__all__ = [
    "CONDITIONS",
    "CONDITION_CLEAR",
    "CONDITION_CLOUDY",
    "CONDITION_FOG",
    "CONDITION_RAIN",
    "CONDITION_SNOW",
    "CONDITION_STORM",
    "UNITS_METRIC",
    "UNITS_IMPERIAL",
    "GeoPlace",
    "WeatherSnapshot",
    "ForecastDay",
    "Forecast",
    "WeatherProvider",
    "Geocoder",
    "OpenMeteoProvider",
    "OpenMeteoGeocoder",
    "build_weather_provider",
    "build_geocoder",
    "condition_from_wmo",
    "describe_wmo",
    "season_for",
    "temp_unit_symbol",
    "resolve_api_key",
]

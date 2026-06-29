"""Tests for the pluggable weather + geocoding providers + factories."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
from typing import Any

from app.llm.weather.providers import (
    CONDITION_CLEAR,
    CONDITION_RAIN,
    CONDITION_SNOW,
    CONDITION_STORM,
    UNITS_IMPERIAL,
    Forecast,
    GeoPlace,
    OpenMeteoGeocoder,
    OpenMeteoProvider,
    WeatherSnapshot,
    build_geocoder,
    build_weather_provider,
    condition_from_wmo,
    describe_wmo,
    resolve_api_key,
    season_for,
)


class _FakeResponse:
    def __init__(self, body: dict[str, Any], *, status: int = 200) -> None:
        self._body = body
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self) -> dict[str, Any]:
        return self._body


class _RequestsPatch:
    """Context-style helper to swap the ``requests`` module for one test."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.captured: dict[str, Any] = {}
        self._real = sys.modules.get("requests")

    def __enter__(self) -> "_RequestsPatch":
        mod = types.ModuleType("requests")

        def _get(url, params=None, timeout=None):  # noqa: A002
            self.captured["url"] = url
            self.captured["params"] = params
            self.captured["timeout"] = timeout
            return self._response

        mod.get = _get  # type: ignore[attr-defined]
        sys.modules["requests"] = mod
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._real is not None:
            sys.modules["requests"] = self._real
        else:
            sys.modules.pop("requests", None)
        return False


class WmoMappingTests(unittest.TestCase):
    def test_clear(self) -> None:
        self.assertEqual(condition_from_wmo(0), CONDITION_CLEAR)
        self.assertEqual(condition_from_wmo(1), CONDITION_CLEAR)

    def test_rain_drizzle_showers(self) -> None:
        for code in (51, 61, 65, 80, 82):
            self.assertEqual(condition_from_wmo(code), CONDITION_RAIN)

    def test_snow(self) -> None:
        for code in (71, 75, 85, 86):
            self.assertEqual(condition_from_wmo(code), CONDITION_SNOW)

    def test_storm(self) -> None:
        for code in (95, 96, 99):
            self.assertEqual(condition_from_wmo(code), CONDITION_STORM)

    def test_unknown_and_none_default_cloudy(self) -> None:
        self.assertEqual(condition_from_wmo(None), "cloudy")
        self.assertEqual(condition_from_wmo(12345), "cloudy")

    def test_describe(self) -> None:
        self.assertEqual(describe_wmo(0), "clear sky")
        self.assertEqual(describe_wmo(95), "thunderstorm")
        self.assertEqual(describe_wmo(None), "unknown")


class SeasonTests(unittest.TestCase):
    def test_northern_hemisphere(self) -> None:
        self.assertEqual(season_for(45.0, datetime(2026, 1, 15)), "winter")
        self.assertEqual(season_for(45.0, datetime(2026, 7, 15)), "summer")

    def test_southern_hemisphere_flips(self) -> None:
        self.assertEqual(season_for(-33.0, datetime(2026, 1, 15)), "summer")
        self.assertEqual(season_for(-33.0, datetime(2026, 7, 15)), "winter")


class OpenMeteoProviderTests(unittest.TestCase):
    def _current_body(self) -> dict[str, Any]:
        return {
            "current": {
                "temperature_2m": 12.5,
                "relative_humidity_2m": 80,
                "apparent_temperature": 10.0,
                "is_day": 1,
                "weather_code": 61,
                "wind_speed_10m": 9.3,
            }
        }

    def test_current_maps_fields(self) -> None:
        with _RequestsPatch(_FakeResponse(self._current_body())) as rp:
            snap = OpenMeteoProvider().current(
                51.5, -0.12, location_label="London",
            )
        self.assertEqual(snap.condition, CONDITION_RAIN)
        self.assertEqual(snap.description, "light rain")
        self.assertEqual(snap.temperature, 12.5)
        self.assertEqual(snap.humidity, 80)
        self.assertEqual(snap.location_label, "London")
        self.assertEqual(snap.units, "metric")
        self.assertTrue(snap.is_day)
        # metric => no unit override params
        self.assertNotIn("temperature_unit", rp.captured["params"])

    def test_imperial_units_param(self) -> None:
        with _RequestsPatch(_FakeResponse(self._current_body())) as rp:
            snap = OpenMeteoProvider().current(
                40.0, -74.0, units=UNITS_IMPERIAL,
            )
        self.assertEqual(snap.units, "imperial")
        self.assertEqual(rp.captured["params"]["temperature_unit"], "fahrenheit")
        self.assertEqual(rp.captured["params"]["wind_speed_unit"], "mph")

    def test_current_raises_on_missing_block(self) -> None:
        with _RequestsPatch(_FakeResponse({"foo": "bar"})):
            with self.assertRaises(Exception):
                OpenMeteoProvider().current(1.0, 2.0)

    def test_current_raises_on_http_error(self) -> None:
        with _RequestsPatch(_FakeResponse(self._current_body(), status=500)):
            with self.assertRaises(Exception):
                OpenMeteoProvider().current(1.0, 2.0)

    def test_forecast_maps_days(self) -> None:
        body = {
            "daily": {
                "time": ["2026-06-29", "2026-06-30"],
                "weather_code": [0, 71],
                "temperature_2m_max": [25.0, 1.0],
                "temperature_2m_min": [15.0, -3.0],
                "precipitation_probability_max": [5, 90],
            }
        }
        with _RequestsPatch(_FakeResponse(body)) as rp:
            fc = OpenMeteoProvider().forecast(
                51.5, -0.12, days=2, location_label="London",
            )
        self.assertIsInstance(fc, Forecast)
        self.assertEqual(len(fc.days), 2)
        self.assertEqual(fc.days[0].condition, CONDITION_CLEAR)
        self.assertEqual(fc.days[1].condition, CONDITION_SNOW)
        self.assertEqual(fc.days[1].precipitation_probability, 90)
        self.assertEqual(rp.captured["params"]["forecast_days"], 2)

    def test_forecast_clamps_days(self) -> None:
        body = {"daily": {"time": [], "weather_code": []}}
        with _RequestsPatch(_FakeResponse(body)) as rp:
            OpenMeteoProvider().forecast(1.0, 2.0, days=99)
        self.assertEqual(rp.captured["params"]["forecast_days"], 16)


class OpenMeteoGeocoderTests(unittest.TestCase):
    def test_resolves_top_result(self) -> None:
        body = {
            "results": [
                {
                    "name": "Tokyo",
                    "latitude": 35.69,
                    "longitude": 139.69,
                    "country": "Japan",
                    "admin1": "Tokyo",
                    "timezone": "Asia/Tokyo",
                }
            ]
        }
        with _RequestsPatch(_FakeResponse(body)) as rp:
            place = OpenMeteoGeocoder().resolve("Tokyo")
        self.assertIsInstance(place, GeoPlace)
        assert place is not None
        self.assertEqual(place.name, "Tokyo")
        self.assertAlmostEqual(place.latitude, 35.69)
        # admin1 == name is deduped in the label
        self.assertEqual(place.label, "Tokyo, Japan")
        self.assertEqual(rp.captured["params"]["name"], "Tokyo")

    def test_empty_name_returns_none(self) -> None:
        place = OpenMeteoGeocoder().resolve("   ")
        self.assertIsNone(place)

    def test_no_results_returns_none(self) -> None:
        with _RequestsPatch(_FakeResponse({"results": []})):
            self.assertIsNone(OpenMeteoGeocoder().resolve("Nowhereville"))

    def test_bare_name_requests_single_result(self) -> None:
        body = {"results": [{"name": "Tokyo", "latitude": 1.0, "longitude": 2.0}]}
        with _RequestsPatch(_FakeResponse(body)) as rp:
            OpenMeteoGeocoder().resolve("Tokyo")
        self.assertEqual(rp.captured["params"]["count"], 1)
        self.assertEqual(rp.captured["params"]["name"], "Tokyo")

    def test_comma_query_searches_bare_name_and_pulls_candidates(self) -> None:
        body = {"results": [{"name": "X", "latitude": 1.0, "longitude": 2.0}]}
        with _RequestsPatch(_FakeResponse(body)) as rp:
            OpenMeteoGeocoder().resolve("Kamenná Poruba, Žilina, Slovakia")
        # Only the bare name reaches the API; hints are matched locally.
        self.assertEqual(rp.captured["params"]["name"], "Kamenná Poruba")
        self.assertEqual(rp.captured["params"]["count"], 10)

    def test_region_hint_disambiguates_same_name_villages(self) -> None:
        # Two villages share a name; the hint picks the right region even
        # across diacritics ("Zilina" hint vs "Žilina" admin1) and the
        # admin-noise word "region".
        body = {
            "results": [
                {
                    "name": "Kamenná Poruba",
                    "latitude": 48.80,
                    "longitude": 18.40,
                    "country": "Slovakia",
                    "admin1": "Trenčín",
                },
                {
                    "name": "Kamenná Poruba",
                    "latitude": 49.18,
                    "longitude": 18.55,
                    "country": "Slovakia",
                    "admin1": "Žilina",
                },
            ]
        }
        with _RequestsPatch(_FakeResponse(body)):
            place = OpenMeteoGeocoder().resolve(
                "Kamenná Poruba, Zilina region, Slovakia"
            )
        assert place is not None
        self.assertEqual(place.admin1, "Žilina")
        self.assertAlmostEqual(place.latitude, 49.18)

    def test_no_hint_match_falls_back_to_first(self) -> None:
        body = {
            "results": [
                {"name": "A", "latitude": 1.0, "longitude": 2.0, "admin1": "First"},
                {"name": "A", "latitude": 3.0, "longitude": 4.0, "admin1": "Second"},
            ]
        }
        with _RequestsPatch(_FakeResponse(body)):
            place = OpenMeteoGeocoder().resolve("A, Nonexistent Region")
        assert place is not None
        self.assertAlmostEqual(place.latitude, 1.0)


class SnapshotRoundTripTests(unittest.TestCase):
    def test_to_from_dict(self) -> None:
        snap = WeatherSnapshot(
            condition=CONDITION_RAIN,
            description="rain",
            temperature=12.0,
            apparent_temperature=10.0,
            humidity=88,
            wind_speed=5.0,
            is_day=True,
            weather_code=63,
            season="autumn",
            units="metric",
            location_label="London",
            fetched_at="2026-06-29T18:00:00+02:00",
        )
        blob = snap.to_dict()
        self.assertEqual(blob["temp_unit"], "C")
        restored = WeatherSnapshot.from_dict(blob)
        self.assertEqual(restored.condition, snap.condition)
        self.assertEqual(restored.humidity, snap.humidity)
        self.assertEqual(restored.location_label, snap.location_label)


class FactoryTests(unittest.TestCase):
    def test_build_weather_provider_default(self) -> None:
        self.assertIsInstance(build_weather_provider(None), OpenMeteoProvider)

    def test_build_geocoder_default(self) -> None:
        self.assertIsInstance(build_geocoder(None), OpenMeteoGeocoder)


class ResolveApiKeyTests(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        self.assertEqual(resolve_api_key("explicit", "SOME_ENV"), "explicit")

    def test_empty_when_nothing(self) -> None:
        self.assertEqual(resolve_api_key("", ""), "")


if __name__ == "__main__":
    unittest.main()

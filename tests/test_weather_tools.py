"""Tests for the H11 brain weather tools (get_weather / get_forecast)."""
from __future__ import annotations

import json
import unittest

from app.llm.tools.base import ToolError
from app.llm.tools.weather import GetForecastTool, GetWeatherTool
from app.llm.weather.providers import (
    Forecast,
    ForecastDay,
    GeoPlace,
    WeatherSnapshot,
)


class _FakeProvider:
    def __init__(self) -> None:
        self.current_calls: list[tuple] = []
        self.forecast_calls: list[tuple] = []

    def current(self, lat, lon, *, units="metric", location_label=""):
        self.current_calls.append((lat, lon, units, location_label))
        return WeatherSnapshot(
            condition="clear", description="clear sky", temperature=20.0,
            apparent_temperature=19.0, humidity=50, wind_speed=3.0,
            is_day=True, weather_code=0, season="summer", units=units,
            location_label=location_label, fetched_at="2026-06-29T12:00:00+02:00",
        )

    def forecast(self, lat, lon, *, days=3, units="metric", location_label=""):
        self.forecast_calls.append((lat, lon, days, units, location_label))
        return Forecast(
            location_label=location_label, units=units,
            days=[
                ForecastDay("2026-06-29", "clear", "clear sky", 25.0, 15.0, 5)
                for _ in range(days)
            ],
        )


class _FakeGeocoder:
    def __init__(self, place: GeoPlace | None) -> None:
        self._place = place
        self.calls: list[str] = []

    def resolve(self, name: str):
        self.calls.append(name)
        return self._place


_TOKYO = GeoPlace(
    name="Tokyo", latitude=35.69, longitude=139.69, country="Japan",
    admin1="Tokyo", timezone="Asia/Tokyo",
)


def _weather_tool(*, home, place=None, units="metric"):
    return GetWeatherTool(
        provider=_FakeProvider(),
        geocoder=_FakeGeocoder(place),
        home_provider=lambda: home,
        units_provider=lambda: units,
    )


class GetWeatherToolTests(unittest.TestCase):
    def test_schema(self) -> None:
        tool = _weather_tool(home=(1.0, 2.0, "Home"))
        schema = tool.schema()
        self.assertEqual(schema.name, "get_weather")
        self.assertIn("location", schema.parameters["properties"])

    def test_uses_home_when_no_location(self) -> None:
        prov = _FakeProvider()
        tool = GetWeatherTool(
            provider=prov, geocoder=_FakeGeocoder(None),
            home_provider=lambda: (51.5, -0.1, "London"),
            units_provider=lambda: "metric",
        )
        out = json.loads(tool.run({}))
        self.assertEqual(out["condition"], "clear")
        self.assertEqual(prov.current_calls[0][0], 51.5)
        self.assertEqual(prov.current_calls[0][3], "London")

    def test_geocodes_explicit_location(self) -> None:
        prov = _FakeProvider()
        geo = _FakeGeocoder(_TOKYO)
        tool = GetWeatherTool(
            provider=prov, geocoder=geo,
            home_provider=lambda: (51.5, -0.1, "London"),
            units_provider=lambda: "metric",
        )
        out = json.loads(tool.run({"location": "Tokyo"}))
        self.assertEqual(geo.calls, ["Tokyo"])
        # Provider was called with Tokyo's coords, not home.
        self.assertAlmostEqual(prov.current_calls[0][0], 35.69)
        self.assertEqual(out["location_label"], "Tokyo, Japan")

    def test_error_when_no_location_and_no_home(self) -> None:
        tool = _weather_tool(home=None)
        with self.assertRaises(ToolError):
            tool.run({})

    def test_error_when_geocode_misses(self) -> None:
        tool = _weather_tool(home=(1.0, 2.0, "Home"), place=None)
        with self.assertRaises(ToolError):
            tool.run({"location": "Nowhereville"})


class GetForecastToolTests(unittest.TestCase):
    def _tool(self, *, home, place=None):
        return GetForecastTool(
            provider=_FakeProvider(),
            geocoder=_FakeGeocoder(place),
            home_provider=lambda: home,
            units_provider=lambda: "metric",
        )

    def test_schema(self) -> None:
        schema = self._tool(home=(1.0, 2.0, "Home")).schema()
        self.assertEqual(schema.name, "get_forecast")
        self.assertIn("days", schema.parameters["properties"])

    def test_default_days(self) -> None:
        prov = _FakeProvider()
        tool = GetForecastTool(
            provider=prov, geocoder=_FakeGeocoder(None),
            home_provider=lambda: (1.0, 2.0, "Home"),
            units_provider=lambda: "metric",
        )
        out = json.loads(tool.run({}))
        self.assertEqual(len(out["days"]), 3)
        self.assertEqual(prov.forecast_calls[0][2], 3)

    def test_days_clamped(self) -> None:
        prov = _FakeProvider()
        tool = GetForecastTool(
            provider=prov, geocoder=_FakeGeocoder(None),
            home_provider=lambda: (1.0, 2.0, "Home"),
            units_provider=lambda: "metric",
        )
        tool.run({"days": 99})
        self.assertEqual(prov.forecast_calls[0][2], 7)

    def test_geocodes_explicit_location(self) -> None:
        prov = _FakeProvider()
        geo = _FakeGeocoder(_TOKYO)
        tool = GetForecastTool(
            provider=prov, geocoder=geo,
            home_provider=lambda: None,
            units_provider=lambda: "metric",
        )
        out = json.loads(tool.run({"location": "Tokyo", "days": 2}))
        self.assertEqual(len(out["days"]), 2)
        self.assertAlmostEqual(prov.forecast_calls[0][0], 35.69)


if __name__ == "__main__":
    unittest.main()

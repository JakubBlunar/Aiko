"""Tests for the H11 WeatherWorker idle worker."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any

from app.core.world.weather_worker import (
    KV_WEATHER_SNAPSHOT,
    WeatherWorker,
    load_weather_snapshot,
    persist_weather_snapshot,
)
from app.llm.weather.providers import WeatherSnapshot


class _FakeDb:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.kv.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.kv[key] = value


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float, str]] = []

    def current(self, lat, lon, *, units="metric", location_label=""):
        self.calls.append((lat, lon, units))
        return WeatherSnapshot(
            condition="rain",
            description="light rain",
            temperature=11.0,
            apparent_temperature=9.0,
            humidity=82,
            wind_speed=6.0,
            is_day=True,
            weather_code=61,
            season="autumn",
            units=units,
            location_label=location_label,
            fetched_at="2026-06-29T18:00:00+02:00",
        )

    def forecast(self, *a, **k):  # pragma: no cover - unused here
        raise NotImplementedError


def _build(
    *,
    db: _FakeDb,
    provider: _FakeProvider,
    home: "tuple[float, float, str] | None",
    enabled: bool = True,
    notify=None,
    seasonal=None,
) -> WeatherWorker:
    return WeatherWorker(
        chat_db=db,
        provider_getter=lambda: provider,
        home_provider=lambda: home,
        units_provider=lambda: "metric",
        enabled_provider=lambda: enabled,
        interval_provider=lambda: 1800.0,
        notify=notify,
        seasonal_hook=seasonal,
    )


class WeatherWorkerTests(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_persist_and_load_round_trip(self) -> None:
        db = _FakeDb()
        persist_weather_snapshot(db, {"condition": "snow", "fetched_at": "x"})
        loaded = load_weather_snapshot(db)
        assert loaded is not None
        self.assertEqual(loaded["condition"], "snow")

    def test_not_ready_when_disabled(self) -> None:
        worker = _build(
            db=_FakeDb(), provider=_FakeProvider(),
            home=(51.5, -0.1, "London"), enabled=False,
        )
        self.assertFalse(worker.is_ready(now=self._now(), last_run_at=None))

    def test_not_ready_without_home(self) -> None:
        worker = _build(
            db=_FakeDb(), provider=_FakeProvider(), home=None, enabled=True,
        )
        self.assertFalse(worker.is_ready(now=self._now(), last_run_at=None))

    def test_ready_when_enabled_and_home(self) -> None:
        worker = _build(
            db=_FakeDb(), provider=_FakeProvider(),
            home=(51.5, -0.1, "London"), enabled=True,
        )
        self.assertTrue(worker.is_ready(now=self._now(), last_run_at=None))

    def test_run_fetches_persists_notifies(self) -> None:
        db = _FakeDb()
        provider = _FakeProvider()
        notified: list[dict[str, Any]] = []
        worker = _build(
            db=db, provider=provider, home=(51.5, -0.1, "London"),
            notify=notified.append,
        )
        result = worker.run()
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["condition"], "rain")
        # Provider was called with the home coords.
        self.assertEqual(provider.calls[0][0], 51.5)
        # Snapshot persisted to kv_meta.
        stored = json.loads(db.kv[KV_WEATHER_SNAPSHOT])
        self.assertEqual(stored["condition"], "rain")
        # Listener fired with the snapshot.
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["season"], "autumn")

    def test_run_skips_without_home(self) -> None:
        worker = _build(db=_FakeDb(), provider=_FakeProvider(), home=None)
        result = worker.run()
        self.assertEqual(result["fetched"], 0)
        self.assertTrue(result["skipped_no_location"])

    def test_run_seasonal_hook_called(self) -> None:
        seen: list[dict[str, Any]] = []
        worker = _build(
            db=_FakeDb(), provider=_FakeProvider(),
            home=(51.5, -0.1, "London"), seasonal=seen.append,
        )
        worker.run()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["condition"], "rain")


if __name__ == "__main__":
    unittest.main()

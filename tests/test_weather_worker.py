"""Tests for the H11 WeatherWorker idle worker."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.world.weather_worker import (
    KV_WEATHER_FETCHED_AT,
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


class DemandTests(unittest.TestCase):
    """Pressure comes from the snapshot's age, not from ``last_run_at``."""

    def _probe(self, worker: WeatherWorker, *, now: datetime | None = None):
        return worker.demand(
            now=now or datetime.now(timezone.utc), last_run_at=None,
        )

    def _aged(self, db: _FakeDb, seconds: float) -> None:
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db.kv[KV_WEATHER_FETCHED_AT] = stamp.isoformat()

    def test_no_snapshot_is_maximum_pressure(self) -> None:
        worker = _build(
            db=_FakeDb(), provider=_FakeProvider(),
            home=(51.5, -0.1, "London"),
        )
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "no snapshot")

    def test_a_snapshot_inside_its_interval_reports_nothing(self) -> None:
        """The P44 regression: a 30-minute cadence became a 10-minute one.

        The probe returned ``compute_staleness`` directly, and since
        urgency is ``0.7 * pressure + 0.3 * staleness`` that collapsed
        to ``urgency = staleness`` — admission the moment staleness
        crossed 0.35, i.e. at 0.35 x interval, on a public API. Pressure
        is now measured from the *due* point, so a snapshot that is
        merely fresh has nothing to say and the heartbeat sets the pace.
        """
        db = _FakeDb()
        worker = _build(
            db=db, provider=_FakeProvider(), home=(51.5, -0.1, "London"),
        )
        for age in (0.0, 630.0, 900.0, 1799.0):  # 1800s interval
            self._aged(db, age)
            with self.subTest(age=age):
                self.assertEqual(self._probe(worker).pressure, 0.0)

    def test_pressure_rises_once_the_snapshot_is_overdue(self) -> None:
        db = _FakeDb()
        worker = _build(
            db=db, provider=_FakeProvider(), home=(51.5, -0.1, "London"),
        )
        self._aged(db, 2700.0)  # half an interval past due
        half = self._probe(worker).pressure
        self.assertGreater(half, 0.4)
        self.assertLess(half, 0.6)

    def test_an_overdue_snapshot_saturates(self) -> None:
        db = _FakeDb()
        self._aged(db, 9000.0)
        worker = _build(
            db=db, provider=_FakeProvider(), home=(51.5, -0.1, "London"),
        )
        self.assertEqual(self._probe(worker).pressure, 1.0)

    def test_a_failed_fetch_does_not_look_satisfied(self) -> None:
        # Keying off the kv timestamp rather than last_run_at is the
        # whole point: a run that advanced the scheduler's clock without
        # reaching the API must still read as stale.
        db = _FakeDb()
        self._aged(db, 9000.0)
        worker = _build(
            db=db, provider=_FakeProvider(), home=(51.5, -0.1, "London"),
        )
        signal = worker.demand(
            now=datetime.now(timezone.utc),
            last_run_at=datetime.now(timezone.utc),
        )
        self.assertEqual(signal.pressure, 1.0)

    def test_disabled_and_homeless_report_no_pressure(self) -> None:
        off = _build(
            db=_FakeDb(), provider=_FakeProvider(),
            home=(51.5, -0.1, "London"), enabled=False,
        )
        self.assertEqual(self._probe(off).reason, "disabled")
        homeless = _build(
            db=_FakeDb(), provider=_FakeProvider(), home=None,
        )
        self.assertEqual(self._probe(homeless).reason, "no location")

    def test_the_probe_never_touches_the_network(self) -> None:
        db = _FakeDb()
        provider = _FakeProvider()
        worker = _build(
            db=db, provider=provider, home=(51.5, -0.1, "London"),
        )
        self._probe(worker)
        self._probe(worker)
        self.assertEqual(provider.calls, [])
        self.assertEqual(db.kv, {})

    def test_the_probe_stays_on_the_compute_lane(self) -> None:
        worker = _build(
            db=_FakeDb(), provider=_FakeProvider(),
            home=(51.5, -0.1, "London"),
        )
        signal = self._probe(worker)
        self.assertFalse(signal.needs_llm)
        self.assertEqual(signal.lane, "compute")


if __name__ == "__main__":
    unittest.main()

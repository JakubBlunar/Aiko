"""Expired sleep must not permanently exclude synthesis and away workers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.affect import vitality
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.idle_worker import WorkSignal
from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
from app.core.world.sleep_lifecycle_worker import SleepLifecycleWorker
from app.core.world.sleep_store import SleepStore


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "chat.db")
    store = SleepStore(db)
    clock = {"now": datetime(2026, 9, 7, 20, tzinfo=timezone.utc)}
    monkeypatch.setattr(timephrase, "now", lambda: clock["now"])
    monkeypatch.setattr(timephrase, "utcnow", lambda: clock["now"])
    settings = SimpleNamespace(
        sleep_enabled=True, sleep_nap_max_hours=2.0, sleep_overnight_max_hours=10.0,
        sleep_max_woken_minutes=90.0, sleep_back_to_sleep_minutes=20.0,
    )
    worker = SleepLifecycleWorker(
        sleep_store=store, chat_db=db, world_store=None, world_guard=None,
        idle_depth_provider=lambda: 7200.0, settings_provider=lambda: settings,
    )
    monkeypatch.setattr(worker, "_energy_and_baseline", lambda *_args: (0.1, 0.1, "night"))
    db.kv_set(vitality.KV_VITALITY, '{"energy":0.1}')
    yield store, worker, clock, db


def start_sleep(store, clock, kind):
    state = store.transition("wind_down", now=clock["now"], sleep_kind=kind)
    return store.transition("fall_asleep", expected_generation=state.generation, now=clock["now"])


@pytest.mark.parametrize("kind,hours", [("nap", 2), ("overnight", 10)])
def test_expired_sleep_cannot_recycle_and_reset_the_waking_timeout(lifecycle, kind, hours):
    store, worker, clock, db = lifecycle
    start_sleep(store, clock, kind)
    clock["now"] += timedelta(hours=hours)
    assert worker.run()["status"] == "woken"
    # Before the fix each 20-minute step settled back to sleep; the following
    # tick immediately woke the same expired episode, restarting the timeout.
    for _ in range(4):
        clock["now"] += timedelta(minutes=20)
        assert worker.run()["status"] == "woken"
        clock["now"] += timedelta(seconds=15)
        assert worker.run()["status"] == "woken"
    clock["now"] += timedelta(minutes=10)
    assert worker.run()["status"] == "awake"
    assert store.list_episodes(limit=1)[0].ended_at is not None
    assert db.kv_get(vitality.KV_VITALITY) == '{"energy":0.1}'


@pytest.mark.parametrize("kind", ["nap", "overnight"])
def test_early_interruption_can_still_settle_back_to_sleep(lifecycle, kind):
    store, worker, clock, _db = lifecycle
    state = start_sleep(store, clock, kind)
    clock["now"] += timedelta(minutes=10)
    store.transition("wake", expected_generation=state.generation, now=clock["now"])
    clock["now"] += timedelta(minutes=21)
    assert worker.run() == {"status": "asleep", "reason": "settled_back"}
    clock["now"] += timedelta(seconds=15)
    assert worker.run() == {"status": "asleep", "reason": "sleeping"}


class PendingWorker:
    interval_seconds = 60.0

    def __init__(self, name):
        self.name = name
        self.probes = 0
        self.runs = 0

    def is_ready(self, **kwargs):
        return True

    def demand(self, **kwargs):
        self.probes += 1
        return WorkSignal(1.0, "pending backlog", needs_llm=True)

    def run(self):
        self.runs += 1


def test_stale_episode_recovery_releases_away_demand_while_synthesis_runs(lifecycle):
    store, worker, clock, db = lifecycle
    start_sleep(store, clock, "overnight")
    clock["now"] += timedelta(days=6)
    scheduler = IdleWorkerScheduler(
        idle_depth_provider=lambda: 5400.0, sleep_state_provider=store.get_state,
        tick_budget_ms=6000, compute_budget_ms=6000,
    )
    jobs = [PendingWorker("concept_synthesis"), PendingWorker("away_activity")]
    jobs[0].sleep_policy = ConceptSynthesisWorker.sleep_policy
    for job in jobs:
        scheduler.register(job)
    scheduler._tick()
    assert [(j.probes, j.runs) for j in jobs] == [(1, 1), (0, 0)]
    assert worker.run() == {"status": "awake", "reason": "episode_timeout"}
    assert store.list_episodes(limit=1)[0].outcome == "episode_timeout"
    assert db.kv_get(vitality.KV_VITALITY) == '{"energy":0.1}'
    clock["now"] += timedelta(minutes=1)
    scheduler._tick()
    assert [(j.probes, j.runs) for j in jobs] == [(2, 2), (1, 1)]
    assert scheduler.get_status()["idle_depth"] == "overnight"


@pytest.mark.parametrize("status", ["winding_down", "asleep", "woken"])
def test_synthesis_is_admitted_during_sleep_but_away_activity_is_not(status):
    scheduler = IdleWorkerScheduler(
        idle_depth_provider=lambda: 2400.0, sleep_state_provider=lambda: status,
    )
    synthesis = PendingWorker("concept_synthesis")
    synthesis.sleep_policy = ConceptSynthesisWorker.sleep_policy
    away = PendingWorker("away_activity")
    scheduler.register(synthesis)
    scheduler.register(away)
    scheduler._tick()
    assert synthesis.probes == 1 and synthesis.runs == 1
    assert away.probes == 0 and away.runs == 0


def test_depth_changes_are_visible_even_without_due_workers(caplog):
    depth = {"seconds": 109.0}
    scheduler = IdleWorkerScheduler(idle_depth_provider=lambda: depth["seconds"])
    with caplog.at_level("INFO", logger="app.idle_worker_scheduler"):
        for seconds in [109, 301, 301, 901, 3601, 10]:
            depth["seconds"] = seconds
            scheduler._tick()
    lines = [r.message for r in caplog.records if r.message.startswith("idle_workers depth:")]
    assert len(lines) == 5
    assert "away -> long_away" in lines[2]
    assert "long_away -> overnight" in lines[3]
    assert "overnight -> just_left" in lines[4]
    assert "sleep=awake" in lines[2]

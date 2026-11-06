from datetime import timedelta
from types import SimpleNamespace

import numpy as np

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_decay_worker import MemoryDecayWorker
from app.core.memory.memory_origin import memory_origin
from app.core.memory.plan_reports import plan_progress
from app.core.memory.plan_timing import historical_plan
from app.core.rag.rag_retriever import _temporal_filter_drops
from tests.test_memory_temporal import _StubMemorySettings


def test_retired_plan_is_not_an_observation_and_repeated_sweeps_do_not_refresh_it(tmp_path):
    path = tmp_path / "plans.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    try:
        now = timephrase.utcnow()
        plan = store.add("Noah intended to visit the museum", "event", np.ones(4),
                         temporal_type="future_plan",
                         event_time=(now - timedelta(days=20)).isoformat())
        worker = MemoryDecayWorker(store, _StubMemorySettings())
        first = worker.run()
        saved = store.get(plan.id)
        assert saved.temporal_type == "future_plan" and saved.tier == "archive"
        assert memory_origin(saved).mode == "intention"
        assert plan_progress(saved, store)["status"] == "unknown"
        assert historical_plan(saved) and _temporal_filter_drops(saved, now)
        receipt = dict(saved.metadata["plan_retirement"])
        until = saved.relevance_until
        assert first["future_plans_to_past"] == 0
        worker.run()
        assert store.get(plan.id).metadata["plan_retirement"] == receipt
        assert store.get(plan.id).relevance_until == until
        rescheduled = store.update(plan.id, event_time=(now + timedelta(days=7)).isoformat())
        assert not historical_plan(rescheduled)
    finally:
        store.close()


def test_plain_old_plan_and_real_event_keep_distinct_time_language():
    now = timephrase.utcnow()
    old = (now - timedelta(days=2)).isoformat()
    plan = timephrase.temporal_suffix(temporal_type="future_plan", event_time=old,
                                     created_at=old, now=now)
    assert "outcome unknown" in plan and "done" not in plan
    event = SimpleNamespace(temporal_type="past_event", relevance_until=old)
    assert _temporal_filter_drops(event, now)

import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.plan_reports import (
    admit_report, offer_intentions, plan_progress, report_current,
)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "plans.db"
    db = ChatDatabase(path)
    pid = db.add_message("s", "user", "I plan a mountain walk.")
    store = MemoryStore(path)
    plan = store.add("Noah plans a mountain walk", "event", np.ones(4),
                     source_message_id=pid, temporal_type="future_plan",
                     event_time="2099-01-01T10:00:00Z")
    yield db, store, plan
    store.close()


def admit(setup, text="I completed the mountain walk.", status="reported_completed",
          temporal="past_event", offered=True, role="user"):
    db, store, plan = setup
    mid = db.add_message("s", role, text)
    payload = {"prior_memory_id": plan.id, "status": status,
               "report": {"message_id": mid, "quote": text}}
    offered_plans = offer_intentions(store) if offered else {}
    return admit_report(payload, [db.get_message_row(mid)], offered_plans,
                        store, session_key="s", subject="user", temporal_type=temporal)


def save(setup, value):
    _, store, _ = setup
    return store.add("Noah reported the mountain walk's outcome", "event", np.ones(4),
                     metadata={"follow_through": value},
                     source_message_id=value["report"]["message_id"],
                     temporal_type="past_event", skip_dedupe=True)


def test_later_report_links_to_intention_without_rewriting_or_assuming_completion(setup):
    _, store, plan = setup
    assert plan_progress(plan, store)["status"] == "planned"
    value = admit(setup)
    report = save(setup, value)
    progress = plan_progress(plan, store)
    assert progress["status"] == "reported_completed"
    assert progress["report_memory_id"] == report.id
    assert value["ordering"] == "later_message"
    assert plan.content == "Noah plans a mountain walk" and plan.temporal_type == "future_plan"
    assert report_current(value, store)


@pytest.mark.parametrize("text,status,temporal", [
    ("I started the mountain walk.", "reported_action", "past_event"),
    ("I cancelled the mountain walk.", "cancelled", "past_event"),
    ("I rescheduled the mountain walk.", "changed_plan", "future_plan"),
    ("I plan another mountain walk.", "planned", "future_plan"),
    ("I am not sure about the mountain walk.", "unknown", "durable"),
])
def test_distinct_report_states_are_preserved(setup, text, status, temporal):
    value = admit(setup, text, status, temporal)
    save(setup, value)
    assert plan_progress(setup[2], setup[1])["status"] == status


@pytest.mark.parametrize("kwargs", [
    {"text": "I would have completed the mountain walk."},
    {"text": "I did not complete the mountain walk."},
    {"text": "I completed it."}, {"text": "I completed the river drawing."},
    {"text": "I started the mountain walk."}, {"role": "assistant"},
    {"offered": False}, {"temporal": "future_plan"},
])
def test_prediction_wrong_actor_ambiguous_action_and_unoffered_plan_are_refused(setup, kwargs):
    with pytest.raises(ValueError):
        admit(setup, **kwargs)


def test_edited_or_deleted_report_does_not_keep_a_plan_closed(setup):
    db, store, plan = setup
    value = admit(setup)
    saved = save(setup, value)
    db.execute_commit("UPDATE messages SET content=? WHERE id=?",
                      ("A correction to that report", value["report"]["message_id"]))
    assert plan_progress(plan, store)["status"] == "unknown"
    store.delete(saved.id)
    assert plan_progress(plan, store)["status"] == "planned"


def test_actual_extraction_offers_plan_ids_and_persists_the_report(setup):
    from app.core.memory.memory_extractor import MemoryExtractor
    from tests.test_memory_extractor_watermark import _FakeOllama, _FakeEmbedder

    db, store, plan = setup
    text = "I completed the mountain walk."
    mid = db.add_message("s", "user", text)
    candidate = {"content": "Noah completed the mountain walk", "kind": "event",
                 "temporal_type": "past_event", "evidence": [{"message_id": mid, "quote": text}],
                 "follow_through": {"prior_memory_id": plan.id, "status": "reported_completed",
                                    "report": {"message_id": mid, "quote": text}}}
    model = _FakeOllama([json.dumps({"memories": [candidate]})])
    worker = MemoryExtractor(db, store, _FakeEmbedder(), model, model="fake", min_window_messages=2)
    assert worker.extract_for_session("s") == 1
    assert f"[intention:{plan.id}]" in model.prompts[0]
    assert plan_progress(plan, store)["status"] == "reported_completed"


def test_closed_plan_cannot_surface_a_previously_queued_forward_question(setup):
    from app.core.proactive.cue_store import CueStore
    from app.core.session.cue_pool_mixin import CuePoolMixin

    db, store, plan = setup
    save(setup, admit(setup))
    cues = CueStore(db, user_id="user")
    cues.add("forward_curiosity", "mountain walk", "How was the mountain walk?",
              payload={"source": "future_plan", "source_id": str(plan.id)})

    class Host(CuePoolMixin):
        pass

    host = Host()
    host._cue_store, host._memory_store = cues, store
    host._settings = SimpleNamespace(agent=SimpleNamespace())
    assert host.take_pool_cue("forward_curiosity", force=True) is None


def test_plan_report_lookup_uses_its_index_and_does_not_scan_the_memory_table(setup):
    db, _, plan = setup
    query = ("SELECT id, metadata FROM memories WHERE json_valid(metadata) AND "
             "json_extract(metadata, '$.follow_through.prior_memory_id') = ?")
    result = db.execute_fetchall("EXPLAIN QUERY PLAN " + query, (plan.id,))
    assert any("idx_memories_plan_report" in str(row) for row in result)

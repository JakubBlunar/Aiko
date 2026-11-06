import json

import numpy as np

from tests.test_goal_worker import _harness


def setup(payload):
    goals, worker, model, _, _ = _harness(payload=json.dumps(payload))
    goal = goals.add_goal(summary="learn to explain tools clearly with examples")
    return goals, worker, model, goal


def test_unchanged_goal_and_evidence_do_not_generate_another_observation():
    goals, worker, model, goal = setup({"note": "I want to try one concrete example first.",
                                      "note_kind": "intention"})
    assert worker.run()["wrote"] == 1
    progress = goals.list_progress(goal.id)[0]
    assert progress.metadata["reflective_derivation"]["scope"] == "generation_context"
    assert progress.metadata["memory_origin"]["mode"] == "intention"
    assert worker.run()["reason"] == "unchanged_evidence"
    assert model.calls == 1 and len(goals.list_progress(goal.id)) == 1


def test_a_relevant_report_can_ground_a_step_but_a_plan_cannot():
    for temporal, expected in (("past_event", 1), ("future_plan", 0)):
        goals, worker, model, goal = setup({"note": "I have a reported tool example to reflect on.",
                                         "note_kind": "reported_step", "evidence_memory_ids": [2]})
        row = goals._memory_store.add("Noah tried a clear tool example", "event", goal.embedding,
                                       temporal_type=temporal, skip_dedupe=True)
        model.payload = json.dumps({"note": "I have a reported tool example to reflect on.",
                                   "note_kind": "reported_step", "evidence_memory_ids": [row.id]})
        assert worker.run()["wrote"] == expected
        prompt = model.last_messages[-1]["content"]
        assert f"ID {row.id}" in prompt
        assert f"temporal={temporal}" in prompt


def test_input_change_during_generation_defers_the_goal_note():
    goals, worker, model, goal = setup({"note": "I want to try a smaller example."})
    store = goals._memory_store
    row = store.add("Noah tried a tool example", "event", goal.embedding)
    original = model.chat_stream

    def changing(*args, **kwargs):
        store.update(row.id, content="A corrected tool example")
        return original(*args, **kwargs)

    model.chat_stream = changing
    assert worker.run()["reason"] == "changed_sources"
    assert not goals.list_progress(goal.id)


def test_repetition_is_checked_across_goals_and_manual_notes_are_retained():
    text = "I want to try one concrete example first and keep the explanation clear."
    goals, worker, _, goal = setup({"note": text})
    goals.add_progress(goal_id=goal.id, note=text, source="tool")
    assert worker.run()["reason"] == "repeated_meaning"
    assert len(goals.list_progress(goal.id)) == 1
    assert goals.add_progress(goal_id=goal.id, note=text, source="tool")


def test_archiving_a_goal_during_generation_does_not_append_worker_progress():
    goals, worker, model, goal = setup({"note": "I want to compare a smaller example."})
    original = model.chat_stream

    def archiving(*args, **kwargs):
        goals.archive_goal(goal.id)
        return original(*args, **kwargs)

    model.chat_stream = archiving
    assert worker.run()["reason"] == "changed_sources"
    assert not goals.list_progress(goal.id)


def test_whole_qualified_source_packet_is_bounded_without_cutting_the_exception():
    from app.core.memory.reflective_evidence import capture, current

    goals, _, _, goal = setup({"note": ""})
    store = goals._memory_store
    text = "Noah discussed tool examples. " + "Detailed context. " * 16 + "Except during debugging."
    row = store.add(text, "preference", np.ones(len(goal.embedding)))
    packet, receipt, _ = capture([row])
    assert "Except during debugging." in packet
    assert current(receipt, store)
    packet, receipt, _ = capture([row], max_chars=80)
    assert not packet and not receipt["memory_inputs"]
    row = store.update(row.id, metadata={"superseded_by": 999}, metadata_merge=True)
    packet, receipt, _ = capture([row])
    assert not packet and not receipt["memory_inputs"]


def test_new_deliberate_notes_wake_reflection_and_changed_conditions_are_not_duplicates():
    from types import SimpleNamespace
    from app.core.goals.goal_reflection_evidence import repetitive

    goals, worker, model, goal = setup({"note": "I want to try a smaller example first."})
    assert worker.run()["wrote"] == 1
    goals.add_progress(goal_id=goal.id, source="tool",
                       note="I want to explore diagrams for explanations.")
    model.payload = json.dumps({"note": "I want to compare a diagram with a short example."})
    assert worker.run()["wrote"] == 1 and model.calls == 2
    older = SimpleNamespace(content="I want to use a short example first except during grief.")
    assert not repetitive("I want to use a short example first except during debugging.", [older])
    assert not repetitive("I do not want to use a short example first except during grief.",
                          [older])


def test_explicit_outcome_receipt_can_ground_a_reported_outcome():
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.observation_receipts import admit_observation

    goals, worker, model, goal = setup({"note": ""})
    store = goals._memory_store
    db = ChatDatabase(store._db_path)
    text = "The clear tool example helped me."
    mid = db.add_message("s", "user", text)
    receipt = admit_observation({"outcome": {"message_id": mid, "quote": text}},
                                [db.get_message_row(mid)], session_key="s", subject="user")
    row = store.add("Noah reported that the clear tool example helped", "event", goal.embedding,
                     source_message_id=mid, metadata={"observation": receipt})
    model.payload = json.dumps({"note": "I have feedback that a clear tool example helped.",
                               "note_kind": "reported_outcome", "evidence_memory_ids": [row.id]})
    assert worker.run()["wrote"] == 1
    value = goals.list_progress(goal.id)[0].metadata["reflective_derivation"]
    assert value["note_kind"] == "reported_outcome" and value["cited_memory_ids"] == [row.id]

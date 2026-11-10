"""An unsuccessful workflow cannot obtain facts by narrating its requested answer."""

import json
from types import SimpleNamespace as NS

import pytest

import test_work_speech as speech_tests

env = speech_tests.env
make_reports = speech_tests.make_reports


def set_result(env, tid, result):
    # Fixture rows are already terminal; replace only their recorded test data.
    env[0].execute_commit("UPDATE tasks SET result=? WHERE id=?", (json.dumps(result), tid))


@pytest.mark.parametrize("outcome", ["partial", "blocked", "nothing_found"])
def test_no_evidence_result_never_asks_narrator_to_fill_in_the_answer(env, outcome):
    reports, tid, calls, queued, shown, _ = make_reports(env)
    set_result(env, tid, {
        "outcome": outcome, "content": "planner picked an unknown action ('web_search')",
        "steps": [], "verification": {"status": "unverified"},
    })
    state = env[1].get(tid).state
    state["contract"]["objective"] = "Season two is out; find Day One and The Last Stand."
    env[1].update_state(tid, state)
    reports.tick()
    assert not calls and len(queued) == 1
    assert reports.deliver(*queued[0])
    text = shown[0][1]
    assert "don't have a verified answer" in text
    assert "Season" not in text and "Day One" not in text and "confirmed" not in text
    assert not reports.deliver(*queued[0])


@pytest.mark.parametrize("verdict", [False, "true", "malformed", "truncated"])
def test_report_review_rejects_new_facts_and_falls_back_to_saved_result(env, verdict):
    reports, tid, _, queued, shown, _ = make_reports(env)
    calls = []

    def client(messages, **kwargs):
        calls.append(kwargs["surface"])
        if kwargs["surface"] == "work_report":
            return '{"text":"The orchard is blue, and episode one is Day One."}', None
        assert "speaking_context" not in messages[1]["content"]
        assert "proposed_report" in messages[1]["content"]
        if verdict == "malformed":
            return "invalid JSON", None
        return json.dumps({"supported": verdict is True or verdict == "truncated"}), NS(
            done_reason="length" if verdict == "truncated" else "stop",
        )

    reports.client = lambda: NS(chat_json=client)
    reports.tick()
    assert calls == ["work_report", "work_report_review"]
    assert reports.deliver(*queued[0])
    assert shown[0][1] == "The orchard is blue."
    assert "Day One" not in shown[0][1]


def test_partial_evidence_does_not_turn_into_confirmed_success(env):
    reports, tid, _, queued, shown, _ = make_reports(env)
    store = env[1]
    child = store.create(user_id="u", handler_name="fast_tool", title="Search", parent_task_id=tid)
    store.mark_done(child, result={"content": "Only the episode count was announced: ten."})
    set_result(env, tid, {
        "outcome": "partial", "content": "Only the episode count is known.",
        "steps": [{"child_id": child, "status": "done"}],
        "verification": {"status": "unverified"},
    })

    def client(messages, **kwargs):
        if kwargs["surface"] == "work_report":
            assert "Only the episode count was announced" in messages[1]["content"]
            assert "Recent conversation" not in messages[1]["content"]
            return '{"text":"The search confirmed all episode titles."}', None
        assert '"outcome":"partial"' in messages[1]["content"]
        return '{"supported":false}', None

    reports.client = lambda: NS(chat_json=client)
    reports.tick()
    assert reports.deliver(*queued[0])
    assert "don't have a verified answer" in shown[0][1]
    assert "confirmed" not in shown[0][1]

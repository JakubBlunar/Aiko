"""Worker inputs retain recording age, actor and source scope through budgets."""
from types import SimpleNamespace

from app.core.infra import timephrase
from app.core.proactive.diary_worker import build_recent_context
from app.core.proactive.dream_worker import DreamWorker
from app.core.session.chat_turn_mixin import ChatTurnMixin
from tests.test_diary_worker import _FakeLLM, _FakeStore, _make_worker
from tests.test_goal_worker import _harness
from tests.test_shared_moment_detector_llm import _make


def test_diary_keeps_date_and_subject_when_context_is_trimmed():
    row = SimpleNamespace(role="user", content="A long conversation " * 40,
                          created_at="2026-10-01T12:00:00+00:00")
    rendered = build_recent_context([row], "Noah", max_chars=120)
    assert len(rendered) <= 120
    assert rendered.startswith("[")
    assert "Noah:" in rendered
    assert timephrase.age_prefix(row.created_at, timephrase.now()) in rendered


def test_diary_refuses_unanchored_stored_wording():
    store = _FakeStore()
    worker = _make_worker(store=store, llm=_FakeLLM("Today felt quiet without them."))
    result = worker.run()
    assert result["reason"] == "persist_failed"
    assert not store.added


def test_goal_reflection_cannot_store_a_floating_today():
    store, worker, llm, _, _ = _harness(payload='{"note": "Today I learned so much."}')
    goal = store.add_goal(summary="Explore unfamiliar chords slowly")
    result = worker.run()
    assert result["wrote"] == 0
    assert not store.list_progress(goal.id)
    system = llm.last_messages[0]["content"]
    assert system.startswith("Today is ")
    assert timephrase.STORED_TEXT_TIME_RULE in system
    assert "not independent evidence" in system


def test_long_dream_sources_keep_their_age_after_prompt_budgeting():
    row = SimpleNamespace(content="An inner thought " * 40,
                          created_at="2026-10-01T12:00:00+00:00")
    host = SimpleNamespace(_memory_store=SimpleNamespace(list_top=lambda **_: [row]))
    notes = ChatTurnMixin._top_inner_life_contents(host, "reflection")
    payload = DreamWorker._compose_user_payload(None, hours_since_last=8,
        rolling_summary="", callbacks=[], self_memories=notes, affect=None)
    assert timephrase.humanize_past(row.created_at, timephrase.now()) in payload
    assert len(notes[0]) < 300


def test_moment_receipt_names_only_the_history_actually_offered():
    det, llm = _make(min_turn_gap=1)
    det.notify_user_turn()
    llm.chat.return_value = ('{"moment": {"summary": "We laughed over the drawing", '
                             '"vibe": "playful"}}')
    det._llm_max_history_chars = 250
    history = [
        SimpleNamespace(id=1, role="user", content="Old context " * 50,
                        created_at="2026-09-01T12:00:00+00:00"),
        SimpleNamespace(id=2, role="user", content="That drawing made me laugh",
                        created_at="2026-10-01T12:00:00+00:00"),
        SimpleNamespace(id=3, role="assistant", content="The tiny hat was my favourite part",
                        created_at="2026-10-01T12:01:00+00:00"),
    ]
    candidate = det.maybe_run_llm(history_provider=lambda: history, reaction_signal=True,
                                 milestone_signal=False, gift_signal=False,
                                 promise_kept_signal=False, now_monotonic=100.)
    assert candidate.source_message_ids == [2, 3]
    assert candidate.when == history[-1].created_at
    assert candidate.source_scope == "context_window"
    assert "Old context" not in llm.chat.call_args.args[0][1]["content"]

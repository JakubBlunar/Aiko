"""Only completed assistant evidence may enter memory and personality learning."""

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.proactive.reflection_worker import ReflectionWorker
from app.core.session.post_turn_mixin import PostTurnMixin
from app.core.session.prompt_assembler import PromptTelemetry
from app.core.session.turn_runner import TurnRunner
from app.llm.chat_client import ChatUsage
from app.llm.turn_outcome import ProviderOutcomeError, TurnOutcome


@pytest.fixture
def database(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    yield db
    db._get_conn().close()


def _runner(db, text="[[reaction:cheerful]]Hello.", *, reason="stop"):
    client = MagicMock()
    client.last_usage = ChatUsage(completion_tokens=20, done_reason=reason)
    client.chat_stream.return_value = iter([text])
    prompt = MagicMock()
    prompt.assemble_with_budget.return_value = ([], PromptTelemetry())
    runner = TurnRunner(
        client, db, prompt, model="synthetic-model", context_window=8192,
        max_tokens=1024, temperature=0.7, filler_enabled=False,
        source_identity_provider=lambda: {
            "provider_id": "synthetic", "route_revision": "revision-1",
        },
    )
    runner._memory_store = MagicMock()
    runner._post_turn = MagicMock()
    return runner


def test_success_has_atomic_source_identity_before_message_listeners(database):
    observed = []
    database.add_message_listener(lambda row: observed.append(database.get_turn_outcomes("s")))
    result = _runner(database).run("s", "hello")
    assert result.outcome.learning_eligible
    record = database.get_turn_outcomes("s")[0]
    assert record["status"] == "completed"
    assert record["provider_id"] == "synthetic"
    assert record["model"] == "synthetic-model"
    assert record["route_revision"] == "revision-1"
    assert record["assistant_message_id"] == result.assistant_message_id
    assert observed[-1][0] == record


@pytest.mark.parametrize("reason", ["length", "incomplete", "content_filter", "refusal"])
def test_unfinished_or_blocked_answer_keeps_user_but_never_assistant_evidence(database, reason):
    runner = _runner(
        database, "[[reaction:sad]]An unfinished answer. [[remember:self:Provisional stance]]",
        reason=reason,
    )
    result = runner.run("s", "I prefer concise replies.")
    assert not result.outcome.learning_eligible
    assert result.assistant_message_id is None
    assert [(row.role, row.content) for row in database.get_messages("s")] == [
        ("user", "I prefer concise replies."),
    ]
    runner._memory_store.add.assert_not_called()
    runner._post_turn.assert_not_called()
    assert database.get_turn_outcomes("s")[0]["status"] in {"truncated", "provider_blocked"}


def test_cancel_when_client_exits_without_another_delta_is_not_completion(database):
    runner = _runner(database)

    def stream(*args, **kwargs):
        yield "[[reaction:sad]]An unfinished answer"
        runner.request_stop()

    runner._ollama.chat_stream.side_effect = stream
    result = runner.run("s", "hello")
    assert result.aborted
    assert result.outcome.status == "cancelled"
    assert [row.role for row in database.get_messages("s")] == ["user"]
    assert database.get_turn_outcomes("s")[0]["status"] == "cancelled"


@pytest.mark.parametrize("status", ["provider_blocked", "budget_exhausted", "incomplete"])
def test_provider_error_records_outcome_and_keeps_original_user_message(database, status):
    runner = _runner(database)
    runner._ollama.chat_stream.side_effect = ProviderOutcomeError("Synthetic error", outcome=status)
    with pytest.raises(ProviderOutcomeError):
        runner.run("s", "Please stop using pet names.")
    assert database.get_messages("s")[0].content == "Please stop using pet names."
    assert database.get_turn_outcomes("s")[0]["status"] == status


@pytest.mark.parametrize("status", ["provider_blocked", "truncated", "cancelled", "empty"])
def test_noncompleted_post_turn_never_updates_affect_trust_or_schedules_reflection(status):
    host = SimpleNamespace(
        _affect_updater=MagicMock(), _scheduler=MagicMock(), _memory_store=MagicMock(),
        _apply_sleep_decision=MagicMock(),
    )
    PostTurnMixin._post_turn_inner_life(
        host, user_text="hello", reaction="sad", assistant_text="Synthetic partial answer",
        raw_assistant_text="[[remember:self:Synthetic stance]]",
        turn_outcome=TurnOutcome(status=status),
    )
    host._affect_updater.apply_turn.assert_not_called()
    host._scheduler.submit.assert_not_called()
    host._memory_store.add.assert_not_called()
    host._apply_sleep_decision.assert_not_called()


def test_completed_user_preference_remains_readable_after_route_switch(database):
    first = _runner(database)
    first.run("s", "I prefer quiet evenings.")
    second = _runner(database)
    second._model = "another-synthetic-model"
    second._source_identity_provider = lambda: {
        "provider_id": "other", "route_revision": "revision-2",
    }
    second.run("s", "hello again")
    records = database.get_turn_outcomes("s")
    assert [(row["provider_id"], row["model"]) for row in records] == [
        ("synthetic", "synthetic-model"), ("other", "another-synthetic-model"),
    ]
    assert database.get_messages("s")[0].content == "I prefer quiet evenings."


def test_bad_cross_session_source_rolls_back_assistant_message(database):
    user_id = database.add_message("other", "user", "hello")
    outcome = TurnOutcome(status="completed", user_message_id=user_id)
    with pytest.raises(ValueError, match="same session"):
        database.add_message(
            "s", "assistant", "hello",
            outcome=outcome,
        )
    assert database.get_messages("s") == []
    assert outcome.assistant_message_id is None


def test_deferred_memory_has_source_and_cannot_write_after_source_deletion(database):
    result = _runner(database).run("s", "hello")
    store = MemoryStore(database._db_path)
    try:
        memory = store.add(
            "A synthetic observation.", "reflection", np.array([1, 0], dtype=np.float32),
            source_session="s", source_message_id=result.assistant_message_id,
            metadata={"turn_outcome": asdict(result.outcome)}, tier="scratchpad",
        )
        assert memory is not None
        assert memory.metadata["turn_sources"][0]["model"] == "synthetic-model"
        database.execute_commit("DELETE FROM messages WHERE id=?", (result.assistant_message_id,))
        assert store.add(
            "A delayed unsupported observation.", "self", np.array([0, 1], dtype=np.float32),
            metadata={"turn_outcome": asdict(result.outcome)},
        ) is None
    finally:
        store.close()


def test_control_only_sleep_decision_is_separate_from_personality_learning(database):
    runner = _runner(database, "[[sleep:stay_asleep]]")
    result = runner.run("s", "hello")
    assert result.outcome.status == "control_only"
    host = SimpleNamespace(_apply_sleep_decision=MagicMock(), _affect_updater=MagicMock())
    PostTurnMixin._post_turn_inner_life(
        host, user_text="hello", reaction="neutral", raw_assistant_text=result.raw_text,
        turn_outcome=result.outcome,
    )
    host._apply_sleep_decision.assert_called_once()
    host._affect_updater.apply_turn.assert_not_called()


def test_delayed_reflection_carries_source_and_rechecks_deletion(database):
    result = _runner(database).run("s", "hello")
    store = MemoryStore(database._db_path)
    client = MagicMock()
    client.chat.return_value = '{"observation":"A synthetic reflection."}'
    embedder = MagicMock()
    embedder.embed.return_value = np.array([1, 0], dtype=np.float32)
    worker = ReflectionWorker(
        ollama=client, memory_store=store, embedder=embedder, model="worker",
        min_seconds_between=0, emotional_delta_threshold=0,
    )
    try:
        kwargs = dict(session_key="s", user_text="hello", assistant_text=result.text,
                      reaction="neutral", turn_outcome=result.outcome)
        first = worker.maybe_run(**kwargs)
        assert len(first.persisted_memory_ids) == 1
        database.execute_commit("DELETE FROM messages WHERE id=?", (result.assistant_message_id,))
        client.chat.return_value = '{"observation":"A different delayed reflection."}'
        assert worker.maybe_run(**kwargs).persisted_memory_ids == []
        result.outcome.status = "cancelled"
        assert worker.maybe_run(**kwargs) is None
        assert client.chat.call_count == 2
    finally:
        store.close()


def test_reopening_database_preserves_history_and_outcomes(database):
    _runner(database).run("s", "hello")
    reopened = ChatDatabase(database._db_path)
    try:
        assert [row.content for row in reopened.get_messages("s")] == ["hello", "Hello."]
        assert reopened.get_turn_outcomes("s")[0]["status"] == "completed"
    finally:
        reopened._get_conn().close()


def test_recorded_attempt_cannot_change_provider_identity(database):
    result = _runner(database).run("s", "hello")
    result.outcome.provider_id = "changed"
    with pytest.raises(ValueError, match="source identity"):
        database.record_turn_outcome("s", result.outcome)
    assert database.get_turn_outcomes("s")[0]["provider_id"] == "synthetic"

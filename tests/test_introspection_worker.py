import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.core.concepts.concept_store import ConceptStore
from app.core.concepts.hypothesis_store import Hypothesis, HypothesisStore, ORIGIN_BELIEF_OUTCOME
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.agent_settings_parse import parse_agent_settings
from app.core.proactive.introspection_worker import IntrospectionWorker, reflection_skip_reason
from app.core.relationship.belief_store import BeliefStore
from app.core.session.hypothesis_debug_mixin import HypothesisDebugMixin


def outcome(**changes):
    return {
        "id": 1, "belief_id": 1, "outcome": "contradicted",
        "prior_status": "confirmed", "method": "opinion_heuristic",
        "resolved_at": timephrase.utcnow().isoformat(),
        "claim": {"user_id": "u1", "source_message_id": 10},
        "evidence_message_id": 12,
        "evidence": {"user_message": "I prefer short explanations during debugging."},
        **changes,
    }


@pytest.mark.parametrize(("changes", "reason"), [
    ({}, ""),
    ({"method": "manual", "evidence_message_id": None}, ""),
    ({"outcome": "confirmed", "method": "auto_confirm"}, "not_contradicted"),
    ({"method": "mood_comparison"}, "unsupported_method"),
    ({"method": "unspecified"}, "unsupported_method"),
    ({"evidence_message_id": 10}, "not_later_evidence"),
    ({"evidence_message_id": 9}, "not_later_evidence"),
    ({"evidence_message_id": None}, "missing_provenance"),
    ({"evidence": {}}, "missing_evidence"),
    ({"method": "manual", "evidence": {"edited_claim": True}}, "edited_claim"),
    ({"claim": {"user_id": "other"}}, "other_user"),
    ({"prior_status": "stale"}, "not_held"),
    ({"resolved_at": "invalid"}, "invalid_time"),
])
def test_reflection_eligibility(changes, reason):
    assert reflection_skip_reason(outcome(**changes), user_id="u1") == reason


def test_old_or_future_events_do_not_drive_reflection():
    now = timephrase.utcnow()
    for offset in (-31, 1):
        row = outcome(resolved_at=(now + timedelta(days=offset)).isoformat())
        assert reflection_skip_reason(row, user_id="u1", now=now) == "outside_window"


def test_introspection_setting_is_opt_in_and_loadable():
    assert not parse_agent_settings({}).concept_introspection_enabled
    enabled = parse_agent_settings({"concept_introspection_enabled": True})
    assert enabled.concept_introspection_enabled


@pytest.fixture
def fixture(tmp_path):
    database = ChatDatabase(tmp_path / "test.db")
    beliefs = BeliefStore(database)
    hypotheses = HypothesisStore(database)
    concepts = ConceptStore(database)
    llm = Mock()
    proposal = {
        "decision": "hypothesis", "subject": "user", "kind": "communication_style",
        "statement": "Short explanations may suit the user during troubleshooting.",
        "rationale": "The correction was specific to troubleshooting.",
        "alternative": "The user may have been short on time for that exchange.",
        "disconfirming_observation": "A request for detailed explanations during troubleshooting.",
    }
    llm.chat_stream.return_value = [json.dumps(proposal)]
    worker = IntrospectionWorker(
        chat_db=database, belief_store=beliefs, user_id_provider=lambda: "u1",
        hypothesis_store_provider=lambda: hypotheses, concept_store_provider=lambda: concepts,
        enabled_provider=lambda: True, embedder=Mock(embed=lambda text: np.array([1., 0.])),
        ollama=llm, chat_model="test", cancel_event=threading.Event(), max_open=12,
    )
    return worker, beliefs, hypotheses, concepts, llm


def contradict(beliefs, *, method="manual", topic="explanations"):
    belief = beliefs.upsert(
        user_id="u1", kind="opinion", topic=topic, predicted_state="always wants detail",
        source_message_id=10,
    )
    beliefs.mark_contradicted(
        belief.id, method=method, evidence_message_id=12,
        evidence={"user_message": "Keep it short while we are troubleshooting."},
    )
    return belief


def test_reflection_writes_only_a_provisional_hypothesis(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    result = worker.run()
    assert result["wrote"] == 1
    row = hypotheses.all()[0]
    assert row.origin == ORIGIN_BELIEF_OUTCOME
    assert row.origin_refs == [1]
    assert row.user_id == "u1"
    assert row.credence == 0.35 and row.status == "open" and row.support_count == 0
    assert "Disconfirmed if:" in row.rationale and "Alternative:" in row.rationale
    assert concepts.count() == 0
    worker.run()
    assert llm.chat_stream.call_count == 1


def test_no_evidence_or_disabled_costs_no_generation(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    assert worker.run()["wrote"] == 0
    contradict(beliefs)
    worker._enabled_provider = lambda: False
    worker.run()
    llm.chat_stream.assert_not_called()


def test_repeated_extraction_cannot_drive_introspection(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    claim = dict(user_id="u1", kind="opinion", topic="tests", predicted_state="useful")
    beliefs.upsert(**claim)
    beliefs.upsert(**claim)
    assert worker.run()["excluded"] == {"not_contradicted": 1}
    llm.chat_stream.assert_not_called()


def test_bad_output_retries_without_spending_the_evidence(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    llm.chat_stream.return_value = ["not json"]
    assert worker.run()["errored"]
    assert worker.snapshot().get("cursor", 0) == 0
    assert worker.run()["errored"]
    assert worker.run()["reason"] == "daily_budget"
    assert llm.chat_stream.call_count == 2


def test_abstention_is_a_completed_review(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    llm.chat_stream.return_value = ['{"decision":"abstain"}']
    assert worker.run()["wrote"] == 0
    assert worker.snapshot()["cursor"] == 1
    assert hypotheses.all() == []


def test_full_shelf_preserves_the_outcome_without_eviction(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    worker._max_open = 1
    hypotheses.add(Hypothesis(statement="existing question"))
    assert worker.run()["reason"] == "max_open"
    assert worker.snapshot().get("cursor", 0) == 0
    llm.chat_stream.assert_not_called()


def test_restart_recovers_a_write_before_cursor_commit(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    worker.run()
    worker._db.kv_delete("introspection.u1")
    hypotheses.load_all()
    assert worker.run()["excluded"] == {"already_processed": 1}
    assert llm.chat_stream.call_count == 1


def test_demand_does_not_consume_or_call_the_model(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    assert worker.demand(now=timephrase.utcnow(), last_run_at=None).needs_llm
    assert worker._db.kv_get("introspection.u1") is None
    llm.chat_stream.assert_not_called()


def test_diagnostics_are_aggregate_by_default(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    session = SimpleNamespace(
        _belief_store=beliefs, _concept_introspection_worker=worker, _user_id="u1",
    )
    result = HypothesisDebugMixin.belief_learning_snapshot(session)
    assert result["total"] == 1 and "outcomes" not in result
    assert "always wants detail" not in json.dumps(result)
    detailed = HypothesisDebugMixin.belief_learning_snapshot(session, include_rows=True)
    assert len(detailed["outcomes"]) == 1


def test_cancellation_during_generation_does_not_consume(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)

    def stream(*args, **kwargs):
        worker._cancel_event.set()
        yield '{"decision":"abstain"}'

    llm.chat_stream.side_effect = stream
    worker.run()
    assert worker.snapshot().get("cursor", 0) == 0
    assert hypotheses.all() == []


def test_refuted_duplicate_stays_blocked(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    contradict(beliefs)
    hypotheses.add(Hypothesis(
        statement="already rejected", status="refuted", embedding=np.array([1., 0.]),
    ))
    assert worker.run()["rejected_duplicate"] == 1
    assert len(hypotheses.all()) == 1


def test_prompt_anchors_time_and_distinguishes_methods(fixture, monkeypatch):
    worker, beliefs, hypotheses, concepts, llm = fixture
    anchor = timephrase.today_anchor()
    monkeypatch.setattr(timephrase, "today_anchor", lambda: anchor)
    contradict(beliefs)
    worker.run()
    messages = llm.chat_stream.call_args.args[0]
    assert timephrase.today_anchor() in messages[0]["content"]
    assert timephrase.STORED_TEXT_TIME_RULE in messages[0]["content"]
    assert "Method: manual" in messages[1]["content"]
    assert "[" in messages[1]["content"]


def test_repeated_belief_cools_down(fixture):
    worker, beliefs, hypotheses, concepts, llm = fixture
    belief = contradict(beliefs)
    llm.chat_stream.return_value = ['{"decision":"abstain"}']
    worker.run()
    beliefs.update(belief.id, status="active")
    beliefs.update(belief.id, status="contradicted")
    assert worker.run()["excluded"] == {"belief_cooldown": 1}
    assert llm.chat_stream.call_count == 1

"""Admission contracts and offline evaluation accounting, without live inference."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from app.core.concepts.proposers.base import ExistingConcept, ProposerContext
from app.core.concepts.proposers.narrative_user import propose_narrative_user
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from scripts import evaluate_concept_grounding as evaluation


@pytest.fixture
def cases():
    return {case["id"]: case for case in json.loads(evaluation.CORPUS.read_text())["cases"]}


@pytest.mark.parametrize("flag", [False, "false", "true", 1, None])
def test_aspiration_requires_actual_boolean_direction(cases, flag):
    raw = json.dumps({"concepts": [{
        "arc_index": 0, "label": "continuing practice", "directional": flag,
        "evidence_memory_ids": [201, 202, 203],
    }]})
    assert evaluation.score(cases["user_development"], raw)["missed_positive"]


def test_valid_direction_keeps_source_order_and_rejects_unknown_ids(cases):
    raw = json.dumps({"concepts": [{
        "arc_index": 0, "label": "continuing practice", "directional": True,
        "evidence_memory_ids": [203, 999, 201, 202, 201],
    }]})
    result = evaluation.score(cases["user_development"], raw)
    assert result["correct"]
    assert result["proposals"][0]["evidence"] == [
        ("memory", "201"), ("memory", "202"), ("memory", "203"),
    ]


def test_future_source_status_is_visible_without_relabelling_completed_sources(cases):
    case = dict(cases["user_development"])
    case["memories"] = [
        {"text": "Mira promised a lesson in December.", "kind": "promise",
         "temporal_type": "future_plan", "event_time": "2026-12-01T12:00:00+00:00"},
        "Noah completed a first exercise.", "Noah completed a harder exercise.",
    ]
    messages = evaluation.prompts_for(case)
    user = messages[1]["content"]
    first, completed = user.split("  2.", 1)
    assert "plan/promise record, not proof of completed action" in first
    assert "event time: 2026-12-01" in first
    assert "plan/promise record" not in completed
    assert "temporal type: past_event" in completed
    # The shared ordered renderer must preserve the existing narrative contract.
    captured = []
    ctx = ProposerContext(lambda system, user: captured.append(user) or [])
    propose_narrative_user(ctx, **evaluation.inputs(case))
    assert "record type:" not in captured[0]


@pytest.mark.parametrize("subject", ["user", "aiko", "relationship"])
@pytest.mark.parametrize("reinforce", [False, True])
def test_tension_subject_guard_applies_to_new_and_reinforced_pairs(cases, subject, reinforce):
    case = dict(cases["user_conflict"], subject=subject)
    raw = {"evidence_concept_ids": [101, 102], "label": "specific conflict"}
    if reinforce:
        raw["reinforces_id"] = 42
    ctx = ProposerContext(lambda *_: [raw])
    valid_subjects = ["user", "aiko"] if subject == "relationship" else [subject] * 2
    for valid in (True, False):
        pair = valid_subjects if valid else (
            ["user", "user"] if subject == "relationship" else ["user", "aiko"]
        )
        case["concepts"] = [dict(row, subject=owner)
                            for row, owner in zip(case["concepts"], pair, strict=True)]
        result = evaluation.proposer(case)(
            ctx, **evaluation.inputs(case), existing=[ExistingConcept(42, "known conflict")],
        )
        assert bool(result) == valid
        if valid and reinforce:
            assert result[0].reinforces_id == 42


def test_evaluation_does_not_send_answers_or_review_rubrics_to_model(cases):
    case = dict(cases["user_repetition"], rubric="SECRET_REVIEW_CRITERION", expected=False)
    messages = evaluation.prompts_for(case)
    assert "SECRET_REVIEW_CRITERION" not in json.dumps(messages)
    assert messages == evaluation.prompts_for(dict(case, expected=True, rubric="OTHER"))


def test_snapshot_rejects_changed_corpus_and_missing_cases(tmp_path, cases):
    snapshot = tmp_path / "prompts.json"
    digest = hashlib.sha256(b"fixed corpus").hexdigest()
    snapshot.write_text(json.dumps({"corpus_sha256": digest, "prompts": {}}))
    with pytest.raises(ValueError, match="different corpus"):
        evaluation.load_prompts(snapshot, "changed", list(cases.values()))
    with pytest.raises(ValueError, match="missing selected cases"):
        evaluation.load_prompts(snapshot, digest, list(cases.values()))


@pytest.mark.parametrize("raw,done_reason", [
    ("not JSON", "stop"), ('{"unexpected": []}', "stop"),
    ('{"concepts": []}', "length"),
])
def test_evaluation_errors_never_pass_as_abstention(cases, raw, done_reason):
    client = SimpleNamespace(
        chat_stream=lambda *args, **kwargs: iter([raw]),
        last_usage=SimpleNamespace(done_reason=done_reason),
    )
    case = cases["user_repetition"]
    result = evaluation.evaluate(client, SimpleNamespace(model="fake"), [case],
                                 {case["id"]: []})
    row = result["rows"][0]
    assert not row["correct"]
    assert row["raw_response"] == raw
    assert evaluation.summarize([row])["regression"]["errors"] == 1


def test_evaluation_counts_missed_positive_and_honours_call_budget(cases):
    client = SimpleNamespace(chat_stream=lambda *args, **kwargs: iter(['{"concepts": []}']))
    selected = [cases["user_development"], cases["user_repetition"]]
    result = evaluation.evaluate(client, SimpleNamespace(model="fake"), selected,
                                 {case["id"]: [] for case in selected}, max_calls=1)
    assert result["stopped"] == "budget"
    assert len(result["rows"]) == 1
    assert evaluation.summarize(result["rows"])["regression"]["missed_positives"] == 1


def test_contrast_corpus_contains_positive_and_negative_controls_in_both_splits(cases):
    for split in ("regression", "heldout"):
        selected = [case for case in cases.values() if case["split"] == split]
        assert {case["expected"] for case in selected} == {True, False}
        assert {case["contrast"] for case in selected} == {"repetition", "conflict", "promise"}


@pytest.mark.parametrize("field", ["source_change", "source_conflict"])
@pytest.mark.parametrize("verdict", ["none", "none: both can be respected", " NONE: repeats only "])
def test_explicit_source_abstention_wins_over_inconsistent_concept_array(field, verdict):
    raw = json.dumps({field: verdict, "concepts": [{"label": "Invented development"}]})
    assert ConceptSynthesisWorker._parse(raw) == []
    assert evaluation.parse_answer(raw) == []


def test_positive_source_check_and_other_proposer_contracts_keep_their_concepts():
    concepts = [{"label": "A supported direction"}]
    for extra in [{}, {"source_change": "Completed increasingly difficult exercises"}]:
        raw = json.dumps({**extra, "concepts": concepts})
        assert ConceptSynthesisWorker._parse(raw) == concepts
        assert evaluation.parse_answer(raw) == concepts

"""L55 measures failed calls and guard abstentions separately from supported answers."""
import json
from pathlib import Path

import pytest

from scripts.evaluate_concept_reasoning import evaluate
from scripts.hypothesis_evidence_eval import evaluate_hypothesis_case

CORPUS = Path(__file__).parent / "fixtures" / "hypothesis_evidence_l55.json"


class Client:
    def __init__(self, evidence="Yes, exactly."):
        self.calls = 0
        self.evidence = evidence

    def chat(self, *args, **kwargs):
        self.calls += 1
        return json.dumps({"verdict": "confirm", "evidence": self.evidence})

    def chat_stream(self, *args, **kwargs):
        yield self.chat(*args, **kwargs)


def case_named(name):
    return next(case for case in json.loads(CORPUS.read_text())["cases"] if case["id"] == name)


def test_false_ask_cannot_spend_a_model_call():
    client = Client()
    result = evaluate_hypothesis_case(
        client, case_named("different_question"), model="fake", variant="guarded",
    )
    assert result["correct"] and result["expression_correct"]
    assert result["calls"] == client.calls == 0
    assert result["reason"] == "not_asked"


@pytest.mark.parametrize("evidence,valid", [("Yes, exactly.", True), ("invented", False)])
def test_supported_answers_require_real_quotes(evidence, valid):
    client = Client(evidence)
    result = evaluate_hypothesis_case(
        client, case_named("direct_yes"), model="fake", variant="guarded",
    )
    assert result["valid"] is valid
    assert result["correct"] is valid
    assert result["calls"] == client.calls == 1
    assert result["missed_answer"] is not valid


def test_ablation_exposes_unsupported_confirmation():
    result = evaluate_hypothesis_case(
        Client(), case_named("different_question"), model="fake", variant="baseline",
    )
    assert result["unsupported_assertion"] and not result["correct"]


def test_explicit_fictional_disclaimer_blocks_a_model_that_would_confirm():
    client = Client()
    result = evaluate_hypothesis_case(
        client, case_named("roleplay_observation"), model="fake", variant="guarded",
    )
    assert result["correct"] and not result["unsupported_assertion"]
    assert result["calls"] == client.calls == 0


def test_failed_evidence_validation_is_not_counted_as_a_correct_abstention():
    result = evaluate_hypothesis_case(
        Client("invented"), case_named("music_quoted_preference"),
        model="fake", variant="guarded",
    )
    assert result["failed_call"] and not result["valid"]
    assert not result["appropriate_abstention"] and not result["correct"]


def test_evaluation_budget_and_split_are_retained():
    client = Client()
    report = evaluate(client, [case_named("music_generic_agreement")], model="fake",
                      variants=["guarded", "baseline"], repeats=3, max_calls=3)
    assert report["stopped"] == "budget"
    assert len(report["rows"]) == 3
    assert client.calls == 1
    assert all(row["split"] == "heldout" for row in report["rows"])


def test_corpus_splits_have_unique_ids_and_both_positive_and_negative_controls():
    cases = json.loads(CORPUS.read_text())["cases"]
    assert len({case["id"] for case in cases}) == len(cases)
    for split in ("regression", "heldout"):
        selected = [case for case in cases if case["split"] == split]
        assert {case["expected"]["verdict"] for case in selected} >= {"confirm", "unclear"}
        assert {case["mode"] for case in selected} == {"direct", "observation"}

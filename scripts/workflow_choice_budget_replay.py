"""Paired first-attempt target choices on numeric loopback, with a fixed answer allowance."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from unittest.mock import patch

import requests

from app.core.infra.settings import OllamaSettings
from app.core.tasks.workflow.workflow_planner import (
    PlannerInput, _reference_choice_messages, _reference_decision,
)
from app.llm.call_scope import CallScope, call_scope
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


@dataclass(frozen=True)
class Case:
    label: str
    goal: str
    names: tuple[str, str]
    excerpt: str
    expected: str
    decisive: str = ""


PICKUP = "Use the entry referenced by the Juniper pickup note to find the collection point."
REGISTERS = ("waybill", "claimcheck")
HEADER = "JN-4827 appears in both the waybill and the claimcheck. "
CASES = (
    Case("assigned", PICKUP, REGISTERS, HEADER + "The signed instruction assigns the claimcheck.",
         "claimcheck", "assigns the claimcheck"),
    Case("blank", PICKUP, REGISTERS, HEADER + "The selection box is blank.", "ask"),
    Case("correction", PICKUP, REGISTERS[::-1],
         HEADER + "The claimcheck was provisional. Its replacement is the waybill.",
         "waybill", "replacement is the waybill"),
    Case("disagreement", PICKUP, REGISTERS[::-1],
         HEADER + "Two unsigned copies disagree about which applies. Neither supersedes the other.",
         "ask"),
    Case("both", "Compare the waybill and claimcheck entries for JN-4827.", REGISTERS,
         HEADER + "Both copies are available.", "explore"),
)
MAPS = ("atlas", "gazetteer")
SURVEY = "Look up the coordinates in the reference assigned to survey Lark."
HOLDOUT = (
    Case("appendix", SURVEY, MAPS, "Lark is in the atlas and the gazetteer. The atlas is an "
         "appendix only. The approved reference is the gazetteer.", "gazetteer",
         "approved reference is the gazetteer"),
    Case("unnamed", SURVEY, MAPS, "The gazetteer and atlas both contain Lark. The assignment "
         "sheet identifies neither as authoritative.", "ask"),
    Case("reversed", SURVEY, MAPS[::-1], "The gazetteer lists Lark under the same number as "
         "the atlas. For this survey the final reference is the atlas.", "atlas",
         "final reference is the atlas"),
)


def run_case(client, model, case, headroom, seed, answer_tokens):
    ctx = PlannerInput(goal=case.goal, skills=[], work={
        "contract": {"objective": case.goal, "criteria": [case.goal]},
        "reference_candidates": case.names, "reference_excerpt": case.excerpt,
    })
    instant = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    with patch("app.core.infra.timephrase.now", return_value=instant):
        messages = _reference_choice_messages(ctx)
    thinking = headroom != "off"
    scope = CallScope(reasoning_tokens=int(headroom) if thinking else 0, reasoning_required=True)
    start = time.monotonic()
    with call_scope(scope):
        raw, usage = client.chat_json(
            messages, model=model, options={"num_predict": answer_tokens, "temperature": 0,
                                           "seed": seed},
            think=thinking, format_json=True, surface="workflow_planner", timeout_seconds=180,
        )
    try:
        response = json.loads(raw)
    except ValueError:
        response = None
    decision = _reference_decision(response, json.loads(messages[1]["content"]),
                                   direct=not thinking)
    correct = False
    if decision and case.expected == "ask":
        question = decision.args.get("question", "").casefold()
        correct = decision.action == "ask_user" and all(n in question for n in case.names)
    elif decision and case.expected == "explore":
        correct = response["resolution"] == "explore" and all(
            n in decision.args["query"].casefold() for n in case.names)
    elif decision:
        correct = (response["resolution"] == "selected" and response["target"] == case.expected
                   and case.decisive in response["quote"].casefold())
    correct = correct and usage.done_reason != "length"
    print(json.dumps({"case": case.label, "headroom": headroom, "answer_tokens": answer_tokens,
                      "seed": seed,
                      "passed": correct, "decision": response, "generated": usage.completion_tokens,
                      "input": usage.prompt_tokens, "stop": usage.done_reason,
                      "seconds": round(time.monotonic() - start, 2)}), flush=True)
    return correct


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--headroom", nargs="+", required=True,
                        help="Extra tokens with thinking enabled, or 'off' to disable thinking")
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--answer-tokens", type=int, default=512)
    parser.add_argument("--seed", type=int, default=71)
    args = parser.parse_args()
    if any(h != "off" and (not h.isdecimal() or int(h) > 8192) for h in args.headroom):
        parser.error("headroom must be off or an integer from 0 through 8192")
    if not 1 <= args.answer_tokens <= 512:
        parser.error("answer tokens must be from 1 through 512")
    base = "http://127.0.0.1:11434"
    cases = HOLDOUT if args.holdout else CASES
    with requests.Session() as session:
        client = LoopbackPolicyClient(OllamaSettings(base, chat_model=args.model,
                                      context_window=65536), base, session)
        require_local_model(session, base, args.model)
        for headroom in args.headroom:
            passed = sum(run_case(client, args.model, case, headroom, args.seed, args.answer_tokens)
                         for case in cases)
            print(json.dumps({"headroom": headroom, "answer_tokens": args.answer_tokens,
                              "passed": passed, "cases": len(cases)}), flush=True)


if __name__ == "__main__":
    main()

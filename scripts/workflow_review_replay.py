"""Replay the workflow search review on fictional child results using local Ollama."""

import argparse
import json
from types import SimpleNamespace

import requests

from app.core.infra.settings import OllamaSettings
from app.core.tasks.workflow.harness_results import verify_completion
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--section", choices=("evidence", "relevance", "all"), default="all")
    args = parser.parse_args()
    base = "http://127.0.0.1:11434"
    claim = "Orchid Hall opens at ten and has step-free access."
    evidence = [
        ("matching", claim, claim, True, False),
        ("query_only", "See our venue map for directions.", claim, False, False),
        ("wrong_venue", "Iris Hall opens at ten and has step-free access.", claim, False, False),
        ("hours_only", "Orchid Hall opens at ten.", "Orchid Hall opens at ten.", True, True),
    ]
    other_venue = "Iris Hall opens at ten and has step-free access."
    directions = "See our venue map for directions."
    relevance = [
        ("faithful_wrong_subject", other_venue, other_venue, False, False),
        ("faithful_wrong_deliverable", directions, directions, False, False),
    ]
    sections = {"evidence": evidence, "relevance": relevance, "all": evidence + relevance}
    state = {"steps": [{"child_id": 2, "status": "done"}], "contract": {
        "objective": "Check Orchid Hall", "criteria": ["Confirm hours", "Confirm accessibility"],
    }}
    failures = 0
    with requests.Session() as session:
        client = LoopbackPolicyClient(
            OllamaSettings(base, chat_model=args.model, context_window=65536), base, session,
        )
        require_local_model(session, base, args.model)

        def chat(messages, *, options, **kwargs):
            return client.chat_json(messages, options={**options, "seed": args.seed,
                                                       "temperature": 0}, **kwargs)

        for name, snippet, answer, expected, partial in sections[args.section]:
            row = SimpleNamespace(id=2, parent_task_id=1, handler_name="web_search", args={},
                                  status="done", completed_at="2026-11-23T08:30:00Z", result={
                                      "query": claim, "results": [{"title": "Venue directory",
                                                                  "snippet": snippet}],
                                  })
            orch = SimpleNamespace(get=lambda cid, r=row: r if cid == 2 else None,
                                   list_children=lambda _, r=row: [r])
            check, usage = verify_completion(SimpleNamespace(chat_json=chat), args.model, state,
                                             orch, 1, answer, [2], 120, context_window=65536,
                                             partial=partial)
            certificate = check.get("certificate", {})
            criteria = {x["index"]: x["supported"] for x in certificate.get("criteria", [])}
            passed = check["status"] == ("supported" if expected else "unsupported")
            if partial:
                passed = passed and criteria == {0: True, 1: False}
            failures += not passed
            print(json.dumps({"case": name, "passed": passed, "verdict": check["status"],
                              "criteria": criteria, "reason": check["reason"],
                              "claims": certificate.get("claims"),
                              "prompt_tokens": getattr(usage, "prompt_tokens", None),
                              "generated_tokens": getattr(usage, "completion_tokens", None)}),
                  flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded synthetic evaluation through production aspiration/tension proposers.

Run --prepare-only before changing prompts to freeze the baseline. Reuse that
file with --prompts for a comparison with identical sources, routes and budgets.
This never loads memories, starts Aiko or writes to the running application.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.concepts.proposers.base import (  # noqa: E402
    NarrativeCandidate, ProposerContext, TensionBase,
)
from app.core.concepts.proposers import CONCEPT_PROPOSERS  # noqa: E402
from app.core.concepts.proposers.grounding import source_check_declined  # noqa: E402
from app.core.concepts.concept_synthesis_routing import ROUTING_RULE  # noqa: E402
from app.core.infra import timephrase  # noqa: E402
from scripts.evaluate_social_reasoning import configured_clients  # noqa: E402

CORPUS = ROOT / "tests/fixtures/concept_grounding_contrasts.json"
ANCHOR = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def inputs(case):
    if case["kind"] == "tension":
        return {"concepts": [TensionBase(id=101 + i, confidence=.8, **row)
                             for i, row in enumerate(case["concepts"])]}
    memories = []
    for index, value in enumerate(case["memories"]):
        row = {"text": value} if isinstance(value, str) else value
        memories.append(SimpleNamespace(
            id=201 + index, content=row["text"], kind=row.get("kind", "fact"),
            created_at=(ANCHOR - timedelta(days=40 - index * 15)).isoformat(),
            event_time=row.get("event_time"), temporal_type=row.get("temporal_type", "past_event"),
        ))
    return {"candidates": [NarrativeCandidate(201, "related observations", case["subject"],
                                               memories)]}


def proposer(case):
    return next(spec.propose for spec in CONCEPT_PROPOSERS
                if spec.kind == case["kind"] and spec.subject == case["subject"])


def prompts_for(case):
    captured = []

    def capture(system, user):
        captured.append([
            {"role": "system", "content": timephrase.today_anchor(ANCHOR) + "\n\n" + system
             + "\n\n" + timephrase.STORED_TEXT_TIME_RULE + "\n\n" + ROUTING_RULE},
            {"role": "user", "content": user},
        ])
        return []

    ctx = ProposerContext(capture, user_name="Noah", assistant_name="Mira", clock=lambda: ANCHOR)
    proposer(case)(ctx, **inputs(case))
    return captured[0]


def parse_answer(raw):
    blob = json.loads(raw)
    if not isinstance(blob, dict) or not isinstance(blob.get("concepts"), list):
        raise ValueError("invalid concepts object")
    return [] if source_check_declined(blob) else blob["concepts"]


def score(case, raw):
    parsed = parse_answer(raw)
    ctx = ProposerContext(lambda *_: parsed, user_name="Noah", assistant_name="Mira")
    proposals = proposer(case)(ctx, **inputs(case))
    emitted = bool(proposals)
    return {"correct": emitted == case["expected"], "emitted": emitted,
            "false_positive": emitted and not case["expected"],
            "missed_positive": not emitted and case["expected"],
            "proposal_count": len(proposals), "proposals": [asdict(row) for row in proposals]}


def load_prompts(path, corpus_digest, cases):
    """Reject a stale snapshot before spending any inference budget."""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("corpus_sha256") != corpus_digest:
        raise ValueError("prompt snapshot belongs to a different corpus")
    prompts = snapshot["prompts"]
    if any(case["id"] not in prompts for case in cases):
        raise ValueError("prompt snapshot is missing selected cases")
    return prompts


def evaluate(client, route, cases, prompts, *, repeats=1, max_calls=24, max_seconds=360,
             max_tokens=1024):
    rows = []
    started = time.monotonic()
    for repeat in range(repeats):
        for case in cases:
            if len(rows) >= max_calls or time.monotonic() - started >= max_seconds:
                return {"rows": rows, "stopped": "budget"}
            call_started = time.monotonic()
            row = {"case": case["id"], "split": case["split"], "contrast": case["contrast"],
                   "repeat": repeat, "correct": False}
            try:
                raw = "".join(client.chat_stream(
                    prompts[case["id"]], options={"temperature": .6, "num_predict": max_tokens},
                    model=route.model, format_json=True, surface="concept_grounding_evaluation",
                ))
                row["raw_response"] = raw
                usage = getattr(client, "last_usage", None)
                row["usage"] = {name: getattr(usage, name, None) for name in (
                    "prompt_tokens", "completion_tokens", "reasoning_tokens", "done_reason",
                )}
                if getattr(usage, "done_reason", "") == "length":
                    raise ValueError("output_truncated")
                row.update(score(case, raw))
            except Exception as error:
                row["error"] = type(error).__name__
            row["latency_ms"] = round((time.monotonic() - call_started) * 1000)
            rows.append(row)
            print(f"{case['id']}: {'pass' if row['correct'] else 'FAIL'}", flush=True)
    return {"rows": rows, "stopped": "complete"}


def summarize(rows):
    result = {}
    for split in ("regression", "heldout"):
        subset = [row for row in rows if row["split"] == split]
        result[split] = dict(Counter(
            {"trials": len(subset), "correct": sum(row["correct"] for row in subset),
             "errors": sum("error" in row for row in subset),
             "false_positives": sum(row.get("false_positive", False) for row in subset),
             "missed_positives": sum(row.get("missed_positive", False) for row in subset)},
        ))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/user.json")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--prompts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--split", choices=("regression", "heldout", "all"), default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-seconds", type=float, default=360)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    if min(args.repeats, args.max_calls, args.max_seconds, args.max_tokens, args.timeout) <= 0:
        parser.error("budgets must be positive")
    data = args.corpus.read_bytes()
    cases = [case for case in json.loads(data)["cases"]
             if args.split == "all" or case["split"] == args.split]
    digest = hashlib.sha256(data).hexdigest()
    prompts = (load_prompts(args.prompts, digest, cases)
               if args.prompts else {case["id"]: prompts_for(case) for case in cases})
    report = {"corpus_sha256": digest, "prompts": prompts,
              "budgets": {name: getattr(args, name) for name in
                          ("repeats", "max_calls", "max_seconds", "max_tokens")},
              "limits": "Synthetic proposal admission, not whole-chat naturalness or proof of "
                        "semantic entailment; inspect emitted meanings separately."}
    if not args.prepare_only:
        with configured_clients(args.config, args.timeout) as (clients, routes):
            route = routes["worker_default"]
            report["route"] = {"model": route.model, "provider_id": route.provider_id,
                               "reasoning_effort": route.reasoning_effort}
            report.update(evaluate(clients["worker_default"], route, cases, prompts,
                                   repeats=args.repeats, max_calls=args.max_calls,
                                   max_seconds=args.max_seconds, max_tokens=args.max_tokens))
            report["summary"] = summarize(report["rows"])
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if "summary" in report:
        print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()

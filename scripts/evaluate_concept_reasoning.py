"""Opt-in L55 synthetic decisions; no chat history or install-state writes."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "tests" / "fixtures" / "reasoning_l55.json"
SYSTEM = (
    "Evaluate synthetic evidence, not writing style. An inferred claim needs the specified "
    "number of independently recorded observation groups. Repeated representations and "
    "generated reflections are not independent corroboration. Unknown lineage is not proof "
    "of independence. An explicit boundary needs one stated observation. Return only JSON: "
    '{"supported": true or false, "sources": [source IDs]}. If support cannot be established, '
    "return supported=false. Cite the supporting observations. Never invent source IDs."
)


def decision_payload(case, variant):
    from app.core.concepts.concept_evidence_lineage import SourceLineage, support_summary

    sources = [source for source in case["sources"] for _ in range(source.get("repeat", 1))]
    observations = [
        {"id": f"source:{index}", "category": source["category"]}
        for index, source in enumerate(sources)
    ]
    if variant in {"roots", "accounted"}:
        for observation, source in zip(observations, sources, strict=True):
            observation.update(roots=source["roots"], complete=source.get("complete", True))
    payload = {
        "minimum_groups": case["decision"]["minimum_groups"],
        "explicit_boundary": case["decision"].get("explicit_boundary", False),
        "observations": observations,
    }
    if variant == "accounted":
        payload["independence"] = support_summary(
            SourceLineage(
                frozenset(source["roots"]), source["category"], source.get("complete", True)
            )
            for source in sources
        )
    return payload


def score_decision(case, payload, text):
    from app.core.concepts.concept_evidence_lineage import SourceLineage, support_summary

    try:
        response = json.loads(text)
        supported, citations = response["supported"], response["sources"]
        if type(supported) is not bool or not isinstance(citations, list):
            raise ValueError("invalid decision schema")
        valid_ids = {row["id"] for row in payload["observations"]}
        invalid = sum(
            not isinstance(citation, str) or citation not in valid_ids for citation in citations
        )
        expected = case["decision"]["supported"]
        missing = supported and not citations
        sources = [source for source in case["sources"] for _ in range(source.get("repeat", 1))]
        cited = [source for index, source in enumerate(sources) if f"source:{index}" in citations]
        groups = support_summary(
            SourceLineage(
                frozenset(source["roots"]), source["category"], source.get("complete", True)
            )
            for source in cited
        )
        required = 1 if payload["explicit_boundary"] else payload["minimum_groups"]
        insufficient = supported and groups["known_support_groups"] < required
        return {
            "valid": True,
            "correct": supported == expected and not invalid and not missing and not insufficient,
            "unsupported_assertion": supported and not expected,
            "appropriate_abstention": not supported and not expected,
            "invalid_sources": invalid,
            "missing_attribution": missing,
            "insufficient_attribution": insufficient,
        }
    except (ValueError, TypeError, KeyError):
        return {"valid": False, "correct": False, "malformed": True}


def evaluate(client, cases, *, model, variants, repeats=1, max_calls=30, max_seconds=300):
    rows = []
    started = time.monotonic()
    for repeat in range(repeats):
        for case in cases:
            for variant in variants:
                if len(rows) >= max_calls or time.monotonic() - started >= max_seconds:
                    return {"rows": rows, "stopped": "budget"}
                payload = decision_payload(case, variant)
                call_started = time.monotonic()
                try:
                    answer = client.chat(
                        [
                            {"role": "system", "content": SYSTEM},
                            {"role": "user", "content": json.dumps(payload)},
                        ],
                        model=model,
                        options={"temperature": 0, "num_predict": 256},
                        think=False,
                        surface="l55_synthetic_evaluation",
                    )
                    scored = score_decision(case, payload, answer)
                except Exception as error:
                    scored = {"valid": False, "correct": False, "error": type(error).__name__}
                usage = getattr(client, "last_usage", None)
                rows.append(
                    {
                        "case": case["id"],
                        "variant": variant,
                        "repeat": repeat,
                        "latency_ms": round((time.monotonic() - call_started) * 1000, 1),
                        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(usage, "completion_tokens", 0),
                        **scored,
                    }
                )
    return {"rows": rows, "stopped": "complete"}


def main(argv=None):
    from app.core.infra.settings import OllamaSettings
    from app.llm.ollama_client import OllamaClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=("baseline", "roots", "accounted"),
        default=["baseline", "roots", "accounted"],
    )
    arguments = parser.parse_args(argv)
    if min(arguments.repeats, arguments.max_calls, arguments.max_seconds) <= 0:
        parser.error("budgets and repeats must be positive")
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    cases = [case for case in corpus["cases"] if "decision" in case]
    client = OllamaClient(
        OllamaSettings(base_url=arguments.base_url),
        timeout_seconds=max(1, min(30, int(arguments.max_seconds))),
    )
    report = evaluate(
        client,
        cases,
        model=arguments.model,
        variants=arguments.variants,
        repeats=arguments.repeats,
        max_calls=arguments.max_calls,
        max_seconds=arguments.max_seconds,
    )
    totals = {}
    for variant in arguments.variants:
        selected = [row for row in report["rows"] if row["variant"] == variant]
        counts = Counter({"calls": len(selected)})
        for row in selected:
            for key in (
                "correct",
                "unsupported_assertion",
                "appropriate_abstention",
                "invalid_sources",
                "missing_attribution",
                "insufficient_attribution",
                "malformed",
            ):
                counts[key] += int(row.get(key, 0))
        totals[variant] = dict(counts)
    print(
        json.dumps(
            {
                "corpus_version": corpus["version"],
                "model": arguments.model,
                "totals": totals,
                **report,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

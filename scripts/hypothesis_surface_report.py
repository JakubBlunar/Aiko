"""Read-only H7/P43 pilot: join final claims, verified asks and answer stages.

Counts describe retained receipts, not semantic truth or an engagement score.
No model, embeddings, application startup or private text output is required.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.concepts.hypothesis_expression import question_span  # noqa: E402
from app.core.memory.surfacing_trace_store import PREFIX, PREFIX_END  # noqa: E402


def collect(conn):
    counts = Counter({"retained_turn_traces": 0, "unreadable_turn_traces": 0})
    claims = defaultdict(list)
    try:
        traces = conn.execute("SELECT value FROM kv_meta WHERE key >= ? AND key < ?",
                              (PREFIX, PREFIX_END))
        for (raw,) in traces:
            try:
                trace = json.loads(raw)
                if trace.get("version") != 1 or not trace.get("attempts"):
                    raise ValueError("unknown trace")
                message_id = int(trace["assistant_message_id"])
                attempts = trace["attempts"]
                pending = []
                omitted = 0
                for index, attempt in enumerate(attempts):
                    omitted += int(attempt.get("claims_omitted", 0))
                    focused = (attempt.get("optional_choice") or {}).get("focused_block")
                    for claim in attempt.get("claims", []):
                        if claim.get("cue_type") == "concept_hypothesis":
                            pending.append((int(claim["cue_id"]), {
                                "message_id": message_id, "state": claim["state"],
                                "final_attempt": index == len(attempts) - 1,
                                "focused": bool(focused and focused in claim.get("blocks", [])),
                            }))
                counts["attempts_omitted"] += int(trace.get("attempts_omitted", 0))
                counts["claims_omitted"] += omitted
                counts["retained_turn_traces"] += 1
                for cue_id, claim in pending:
                    claims[cue_id].append(claim)
            except (ValueError, TypeError, KeyError, AttributeError):
                counts["unreadable_turn_traces"] += 1
    except sqlite3.Error:
        counts["trace_table_unavailable"] += 1

    families = defaultdict(Counter)
    rows = []
    try:
        cues = conn.execute(
            "SELECT id, subject, payload FROM cue_pool WHERE cue_type='concept_hypothesis'",
        ).fetchall()
    except sqlite3.Error as error:
        return {"error": str(error), "coverage": dict(counts), "families": {}, "cues": []}
    for cue_id, subject, raw in cues:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("invalid cue")
        except (TypeError, ValueError):
            counts["unreadable_cue_payloads"] += 1
            continue
        family = payload.get("target_type")
        family = family if family in ("hypothesis", "concept") else "unknown"
        metrics = families[family]
        metrics["retained_cues"] += 1
        asked = payload.get("asked_message_id")
        asked = asked if type(asked) is int and asked > 0 else None
        asked_text = payload.get("asked_text")
        asked_text = asked_text if isinstance(asked_text, str) else ""
        span = question_span(str(payload.get("label") or subject), asked_text)
        verified = bool(asked and payload.get("ask_verified") is True and span)
        metrics["stored_ask_receipts"] += bool(asked)
        metrics["verified_ask_receipts"] += verified
        metrics["unverified_ask_receipts"] += bool(asked and not verified)
        joined = [claim for claim in claims.get(cue_id, []) if claim["final_attempt"]]
        committed = [claim for claim in joined if claim["state"] == "committed"]
        metrics["cues_with_final_trace"] += bool(joined)
        metrics["cues_with_final_commit"] += bool(committed)
        metrics["cues_with_focused_commit"] += any(claim["focused"] for claim in committed)
        metrics["verified_asks_joined_to_final_commit"] += bool(
            verified and any(claim["message_id"] == asked for claim in committed)
        )
        metrics["cues_without_final_trace"] += not bool(joined)
        feedback = payload.get("feedback") or []
        if not isinstance(feedback, list):
            counts["unreadable_feedback"] += 1
            feedback = []
        feedback = [entry for entry in feedback if isinstance(entry, dict)]
        stages = {entry["state"] for entry in feedback if isinstance(entry.get("state"), str)}
        for stage in ("not_asked", "unrelated_answer", "adjudicator_failure", "accepted_answer",
                      "update_applied", "update_not_applied", "unverified_ask"):
            metrics["cues_with_" + stage] += stage in stages
        metrics["accepted_answers_with_unverified_ask"] += bool(
            "accepted_answer" in stages and not verified
        )
        delivery_reported = any(isinstance(entry.get("delivery"), dict)
                                and entry["delivery"].get("status") == "reported"
                                for entry in feedback)
        metrics["cues_with_reported_delivery"] += delivery_reported
        metrics["cues_without_reported_delivery"] += not delivery_reported
        counts["feedback_entries_retained"] += len(feedback)
        rows.append({"cue_id": cue_id, "family": family,
                     "asked_message_id": asked, "verified_ask": verified,
                     "question_span_detected": bool(span), "final_claims": joined,
                     "feedback_stages": sorted(str(stage) for stage in stages)})
    return {"coverage": dict(counts), "families": dict(families), "cues": rows,
            "limits": [
                "Assembly receipts retain at most 128 turns and two attempts per turn.",
                "Cue feedback retains at most eight events; missing stages are unknown.",
                "Verified ask is a conservative expression check, not semantic entailment.",
                "Delivery and human-rated usefulness are not inferred from engagement or a commit.",
                "Family counts describe retained cues, not a common-window conversion rate.",
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "chat_sessions.db")
    args = parser.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        print(json.dumps(collect(conn), indent=2))


if __name__ == "__main__":
    main()

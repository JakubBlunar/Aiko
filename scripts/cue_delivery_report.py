"""Read-only, content-free cue delivery baseline over a declared time window."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3

from app.core.memory.surfacing_trace_store import PREFIX, PREFIX_END


def collect(conn, *, since):
    turns = {row[0] for row in conn.execute(
        "SELECT DISTINCT assistant_message_id FROM turn_prompt_blocks "
        "WHERE julianday(created_at)>=julianday(?)", (since,),
    )}
    coverage = Counter({"measured_turns": len(turns), "retained_final_traces": 0,
                        "invalid_traces": 0})
    families = defaultdict(Counter)
    routes = Counter()
    traced = set()
    for (raw,) in conn.execute("SELECT value FROM kv_meta WHERE key>=? AND key<?",
                               (PREFIX, PREFIX_END)):
        try:
            trace = json.loads(raw)
            mid = int(trace["assistant_message_id"])
            if mid not in turns:
                continue
            attempts = trace["attempts"]
            if trace.get("version") != 1 or not attempts:
                raise ValueError("unknown trace")
            final = attempts[-1]
            if not isinstance(final, dict):
                raise ValueError("invalid attempt")
            traced.add(mid)
            coverage["retained_final_traces"] += 1
            coverage["discarded_or_retry_attempts"] += len(attempts) - 1
            for claim in final.get("claims", []):
                family = str(claim["cue_type"])
                state = str(claim["state"])
                families[family]["final_claim:" + state] += 1
                if state == "committed":
                    for block in claim.get("blocks", []):
                        routes[f"{family}:{block}"] += 1
            coverage["claims_omitted_by_retention"] += int(final.get("claims_omitted", 0))
        except (ValueError, TypeError, KeyError, AttributeError):
            coverage["invalid_traces"] += 1
    coverage["turns_without_retained_trace"] = len(turns - traced)
    for cue, outcome, reason, count in conn.execute(
        "SELECT cue,outcome,reason,count(*) FROM cue_decisions "
        "WHERE julianday(created_at)>=julianday(?) GROUP BY cue,outcome,reason", (since,),
    ):
        families[cue]["decision:" + outcome] += count
        if outcome == "declined":
            families[cue]["decline:" + reason] += count
    # Feedback is bounded per cue and not a historical event ledger. Count only
    # observed references within the chosen turn cohort; absence stays unknown.
    for cue, raw in conn.execute("SELECT cue_type,payload FROM cue_pool"):
        try:
            payload = json.loads(raw or "{}")
            if payload.get("asked_message_id") in turns:
                families[cue]["stored_ask_receipts"] += 1
                families[cue]["verified_ask_receipts"] += payload.get("ask_verified") is True
            for feedback in payload.get("feedback", []):
                if feedback.get("assistant_message_id") in turns:
                    families[cue]["feedback:" + str(feedback.get("state", "unknown"))] += 1
        except (ValueError, TypeError, AttributeError):
            coverage["invalid_cue_payloads"] += 1
    return {
        "since": since, "coverage": dict(coverage), "families": dict(families),
        "committed_routes": dict(routes),
        "limits": ["Assembly claims, stated questions, answer matching and usefulness differ.",
                   "Missing retained traces or feedback are unknown, not failed delivery.",
                   "Counters are stages with different units; do not divide them into a funnel.",
                   "No dialogue text, cue subjects, or human usefulness labels are exported."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/chat_sessions.db"))
    parser.add_argument("--since", required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        print(json.dumps(collect(conn, since=args.since), indent=2))


if __name__ == "__main__":
    main()

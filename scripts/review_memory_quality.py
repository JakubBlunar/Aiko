"""Prepare a read-only, source-linked historical review; never apply repairs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra import timephrase  # noqa: E402
from app.core.memory.claim_fidelity import refusal  # noqa: E402
from app.core.memory.memory_origin import claim_source_refusal  # noqa: E402
from app.core.memory.promise_evidence import same_source_actions  # noqa: E402
from app.core.memory.promise_worker import PromiseExtractionWorker  # noqa: E402
from app.core.memory.promise_extractor import Promise  # noqa: E402
from app.core.memory.promise_lifecycle import promise_what  # noqa: E402


def _meta(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _evidence(conn, memory):
    meta = memory.metadata
    ids = [memory.source_message_id] if memory.source_message_id else []
    ids.extend((meta.get("admission") or {}).get("source_message_ids", []))
    result = []
    for mid in list(dict.fromkeys(ids))[:4]:
        row = conn.execute("SELECT id,role,content,created_at FROM messages WHERE id=?", (mid,))
        row = row.fetchone()
        if row is None:
            result.append({"id": mid, "missing": True})
        else:
            value = dict(row)
            value["truncated"] = len(value["content"]) > 4000
            value["content"] = value["content"][:4000]
            result.append(value)
    return result


def review(conn, *, limit=24, concept_ids=()):
    """Flags nominate review candidates, not errors or a semantic quality score."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        memories = {}
        for row in conn.execute("SELECT id,content,kind,metadata,source_message_id,created_at,"
                                "event_time,temporal_type,provenance FROM memories ORDER BY id"):
            value = dict(row)
            value["metadata"] = _meta(value["metadata"])
            memories[value["id"]] = SimpleNamespace(**value)
        flags = defaultdict(list)
        seen = defaultdict(list)
        for memory in memories.values():
            meta = memory.metadata
            if memory.kind == "promise":
                who = meta.get("promise_who")
                promise = Promise(who=who, text=promise_what(memory),
                                  source_turn_id=memory.source_message_id)
                key = (who, memory.source_message_id)
                if same_source_actions(promise, seen[key], PromiseExtractionWorker._is_duplicate):
                    flags["repeated_commitment_source"].append(memory.id)
                seen[key].append(memory)
                if meta.get("promise_deadline") and not meta.get("deadline_quote"):
                    flags["deadline_needs_source_review"].append(memory.id)
            if (memory.temporal_type == "past_event" and re.search(
                r"\b(?:plans? to|intends? to|will|is going to|promised)\b", memory.content, re.I,
            )):
                flags["historical_plan_modality"].append(memory.id)
            citations = (meta.get("admission") or {}).get("evidence") or []
            quotes = [item["quote"] for item in citations
                      if isinstance(item, dict) and isinstance(item.get("quote"), str)]
            if quotes:
                reason = refusal(memory.content, quotes, "reported")
                if reason:
                    flags[reason].append(memory.id)
        samples = {}
        for reason, ids in sorted(flags.items()):
            samples[reason] = [{"id": mid, "content": memories[mid].content,
                "temporal_type": memories[mid].temporal_type,
                "event_time": memories[mid].event_time,
                "created_at": memories[mid].created_at,
                "deadline": memories[mid].metadata.get("promise_deadline"),
                "source_messages": _evidence(conn, memories[mid])}
                for mid in list(reversed(ids))[:limit]]
        concepts = []
        for row in conn.execute("SELECT id,label,status,kind,subject FROM concepts "
                                "WHERE status='active' ORDER BY id DESC"):
            bad = []
            for edge in conn.execute("SELECT DISTINCT src_type,src_id FROM concept_edges "
                "WHERE dst_type='concept' AND dst_id=? AND polarity>0 "
                "AND src_type IN ('memory','moment','cluster')", (str(row["id"]),)):
                memory = memories.get(int(edge["src_id"]))
                reason = (claim_source_refusal(memory, subject=row["subject"], kind=row["kind"])
                          if memory else "missing_source")
                if reason:
                    bad.append({"type": edge["src_type"], "id": edge["src_id"], "reason": reason,
                        "content": memory.content if memory else None,
                        "source_messages": _evidence(conn, memory) if memory else []})
            if bad:
                concepts.append({**dict(row), "incompatible_sources": bad,
                    "meaning": "Some sources are incompatible; other support may remain. "
                               "Cluster references inspect the representative, "
                               "not historical members."})
        selected = ([row for row in concepts if row["id"] in set(concept_ids)]
                    if concept_ids else concepts[:limit])
        return {"version": 1, "scope": "read_only_review_candidates",
            "captured_at": timephrase.utcnow().isoformat(), "memory_count": len(memories),
            "flag_counts": dict(Counter({key: len(ids) for key, ids in flags.items()})),
            "memory_samples": samples, "concept_candidate_count": len(concepts),
            "concept_samples": selected, "sample_limit_per_reason": limit,
            "limits": "Flags are not confirmed errors. Missing source remains unknown. "
                      "No status, confidence, deadline, memory or source was changed."}
    finally:
        conn.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/chat_sessions.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--concept-ids", type=int, nargs="*", default=[])
    args = parser.parse_args()
    if args.output.resolve() == args.database.resolve():
        parser.error("output must differ from the source database")
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        report = review(conn, limit=max(1, min(100, args.limit)), concept_ids=args.concept_ids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"memory_count": report["memory_count"], "flag_counts": report["flag_counts"],
                      "concept_candidate_count": report["concept_candidate_count"]}))


if __name__ == "__main__":
    main()

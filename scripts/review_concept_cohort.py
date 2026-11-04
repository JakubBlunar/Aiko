"""Export a reproducible, source-linked concept review without changing live state.

Private output belongs outside tracked fixtures. Missing or model-authored evidence
remains explicit; the export does not certify support, independence or quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from app.core.infra import timephrase

FIELDS = ("supported", "distinct", "scope_correct", "natural_register")


def select_sample(rows, *, limit=24, seed="ordinary-use-v1"):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["subject"], row["kind"], row["status"])].append(dict(row))
    for group in groups.values():
        group.sort(key=lambda row: hashlib.sha256(f"{seed}:{row['id']}".encode()).hexdigest())
    result = []
    while groups and len(result) < limit:
        for key in sorted(list(groups)):
            result.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
            if len(result) >= limit:
                break
    return result


def _source(conn, kind, ident):
    if kind == "memory":
        row = conn.execute(
            "SELECT id,content,kind,created_at,source_message_id,metadata,provenance,"
            "temporal_type,event_time FROM memories WHERE id=?", (ident,),
        ).fetchone()
        if row is None:
            return {"type": kind, "id": ident, "missing": True}
        record = dict(row)
        meta = json.loads(record.pop("metadata") or "{}")
        ids = [record["source_message_id"]] if record["source_message_id"] else []
        ids += list(meta.get("admission", {}).get("source_message_ids", []))
        messages = []
        for mid in list(dict.fromkeys(ids))[:4]:
            message = conn.execute(
                "SELECT id,role,content,created_at FROM messages WHERE id=?", (mid,),
            ).fetchone()
            messages.append(dict(message) if message else {"id": mid, "missing": True})
        record["messages"] = messages
        record["source_authority"] = meta.get("source_authority", "unknown")
        return {"type": kind, **record}
    if kind == "concept":
        row = conn.execute(
            "SELECT id,label,rationale,status,subject,kind FROM concepts WHERE id=?", (ident,),
        ).fetchone()
    elif kind == "cluster":
        # Evidence edges use representative MEMORY ids, not database cluster ids.
        # Cached labels are descriptive only; a current cluster cannot recreate
        # the historical membership that supported an earlier claim.
        cached = conn.execute("SELECT value FROM kv_meta WHERE key=?",
                              (f"aiko.topic_label.{ident}",)).fetchone()
        label = json.loads(cached[0]) if cached else {}
        return {"type": kind, "id": ident, "label": label.get("label", ""),
                "representative": _source(conn, "memory", ident),
                "historical_membership": "not_reconstructed"}
    else:
        row = None
    return {"type": kind, **dict(row)} if row else {"type": kind, "id": ident, "missing": True}


def score_annotations(sample, annotations):
    sampled = {str(row["id"]) for row in sample}
    report = {}
    for field in FIELDS:
        values = [row.get(field) for key, row in annotations.items() if key in sampled]
        if any(value is not None and type(value) is not bool for value in values):
            raise ValueError("annotation values must be true, false or null")
        judged = [value for value in values if value is not None]
        report[field] = {"judged": len(judged), "passed": sum(judged),
                         "rate": sum(judged) / len(judged) if judged else None}
    return report


def collect(conn, *, since, until=None, limit=24, seed="ordinary-use-v1", annotations=None):
    until = until or timephrase.utcnow().isoformat()
    rows = conn.execute(
        "SELECT id,label,rationale,subject,kind,status,created_at,promoted_at,"
        "confidence,distinct_source_count FROM concepts "
        "WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?) "
        "AND status IN ('active','candidate')",
        (since, until),
    ).fetchall()
    sample = select_sample(rows, limit=limit, seed=seed)
    for row in sample:
        edges = conn.execute(
            "SELECT src_type,src_id FROM concept_edges WHERE dst_type='concept' "
            "AND dst_id=? AND relation='evidence' ORDER BY src_type,src_id LIMIT 9",
            (str(row["id"]),),
        ).fetchall()
        row["evidence_truncated"] = len(edges) > 8
        row["evidence"] = [_source(conn, edge["src_type"], edge["src_id"])
                           for edge in edges[:8]]
        row["review_flags"] = ["relative_time"] if timephrase.has_relative_deictic(
            row["label"],
        ) else []
        row["annotation"] = (annotations or {}).get(str(row["id"]),
                                                   {field: None for field in FIELDS})
    return {
        "since": since, "until": until, "seed": seed,
        "eligible": len(rows), "sampled": len(sample),
        "strata": dict(Counter(f"{r['subject']}/{r['kind']}/{r['status']}" for r in sample)),
        "selection": "deterministic round-robin by subject/kind/status; not prevalence-weighted",
        "scoreboard": score_annotations(sample, annotations or {}),
        "limits": "Direct evidence only; excerpts and cluster labels are not proof of entailment "
                  "or independent support. No unjudged item is scored as correct.",
        "sample": sample,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/chat_sessions.db"))
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--seed", default="ordinary-use-v1")
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    annotations = (
        json.loads(args.annotations.read_text(encoding="utf-8")) if args.annotations else {}
    )
    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        report = collect(conn, since=args.since, until=args.until,
                         limit=max(1, min(args.limit, 100)),
                         seed=args.seed, annotations=annotations)
    finally:
        conn.close()
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "sample"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

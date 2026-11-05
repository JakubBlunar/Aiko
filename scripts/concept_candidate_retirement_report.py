"""Read-only candidate-retirement eligibility, using the production predicate.

This is a snapshot, not a full lifecycle replay: subsequent accrual, source
validation and incoming support can change the decision before evaluation.
No labels, dialogue or memory text are included in the report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.concepts.concept_candidate_retirement import (  # noqa: E402
    candidate_retirement_reason,
)
from app.core.infra import timephrase  # noqa: E402
from app.core.infra.memory_settings import parse_memory_settings  # noqa: E402


def collect(connection, settings, *, now):
    cursor = connection.execute(
        "SELECT id,kind,confidence,distinct_source_count,created_at,first_evidence_at,"
        "first_evidence_engagement,last_reinforced_at FROM concepts WHERE status='candidate' "
        "ORDER BY id",
    )
    columns = [field[0] for field in cursor.description]
    rows = cursor.fetchall()
    total = connection.execute(
        "SELECT value FROM kv_meta WHERE key='engagement.total_units'",
    ).fetchone()
    units = float(total[0]) if total is not None and settings.engagement_clock_enabled else None
    eligible = []
    unknown_age = 0
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        first = timephrase.parse_iso(record["first_evidence_at"] or record["created_at"])
        anchor = record["first_evidence_engagement"]
        if units is not None and anchor is not None:
            age = max(0, units - float(anchor)) / settings.engagement_seconds_per_day
        elif first is not None:
            age = max(0, (now - first).total_seconds()) / 86400
        else:
            unknown_age += 1
            continue
        last = (timephrase.parse_iso(record["last_reinforced_at"])
                or timephrase.parse_iso(record["created_at"]))
        quiet = max(0, (now - last).total_seconds()) / 86400 if last is not None else None
        reason = candidate_retirement_reason(
            age_days=age, ttl_days=settings.concept_candidate_ttl_days,
            sources=record["distinct_source_count"],
            min_sources=settings.concept_promote_min_sources,
            confidence=record["confidence"], retire_floor=settings.concept_retire_confidence_floor,
            quiet_wall_days=quiet,
        )
        if reason:
            eligible.append({"id": record["id"], "kind": record["kind"], "reason": reason,
                             "confidence": record["confidence"], "age_days": round(age, 2),
                             "quiet_wall_days": round(quiet, 2) if quiet is not None else None})
    return {"generated_at": now.isoformat(), "candidates": len(rows), "unknown_age": unknown_age,
            "eligible": len(eligible), "by_reason": dict(Counter(r["reason"] for r in eligible)),
            "by_kind": dict(Counter(r["kind"] for r in eligible)), "records": eligible,
            "settings": {name: getattr(settings, name) for name in (
                "concept_candidate_ttl_days", "concept_retire_confidence_floor",
                "concept_promote_min_sources", "engagement_seconds_per_day",
            )}, "limits": __doc__.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/chat_sessions.db")
    parser.add_argument("--config", type=Path, default=ROOT / "config/user.json")
    args = parser.parse_args()
    defaults = json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))
    overrides = json.loads(args.config.read_text(encoding="utf-8"))
    settings = parse_memory_settings({**defaults.get("memory", {}), **overrides.get("memory", {})})
    # Resolve before opening, and require an existing database: no bootstrap or migration.
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        print(json.dumps(collect(connection, settings, now=timephrase.utcnow()), indent=2))


if __name__ == "__main__":
    main()

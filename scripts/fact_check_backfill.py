"""F1 fact-check backfill -- queue the impersonal claims already sitting in memory.

The enqueue hook (``MemoryFacadeMixin._maybe_enqueue_claims``) reads its payload
off a ``Memory`` *or* off ``mem.to_dict()``; for a long time it only handled the
object, so every worker that passed the dict form -- knowledge, topic digest,
pre-thought, K1 goal -- silently queued nothing. The hook is fixed, but it only
fires on *new* writes, and impersonal knowledge arrives at a handful of
claim-bearing rows a month. This script walks what is already stored so the
checker has something to chew on now rather than next quarter.

Read-only by default::

    python scripts/fact_check_backfill.py            # what would be queued
    python scripts/fact_check_backfill.py --apply    # actually queue it

Nothing here bypasses a gate. Each row goes through the same
:func:`classify_memory_for_fact_check` privacy classifier and the same
:func:`web_safe_probe` per-span gate the live path uses, so a memory
that would be refused on write is refused here too. Enqueueing is free -- the
queue is a JSON list in ``kv_meta`` -- and ``IdleFactChecker`` drains it one
claim per tick under its own rate limiter, so a large backfill costs nothing up
front and cannot outrun the search budget.

Rows already carrying ``metadata.last_verified_at`` / ``last_checked_at`` are
skipped: those have been adjudicated once already.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra.chat_database import ChatDatabase  # noqa: E402
from app.core.infra.settings import load_settings  # noqa: E402
from app.core.memory.claim_extractor import find_claims  # noqa: E402
from app.core.memory.fact_check_privacy import (  # noqa: E402
    classify_memory_for_fact_check,
    web_safe_probe,
)
from app.core.memory.fact_check_queue import FactCheckQueue  # noqa: E402

DEFAULT_DB = Path("data/chat_sessions.db")

#: Kinds worth walking. The privacy classifier refuses the personal kinds
#: outright, so listing them here would only produce noise in the skip
#: histogram; these are the ones that can plausibly clear it.
DEFAULT_KINDS = ("knowledge", "curiosity_finding", "topic_digest")


def _identity(settings: Any) -> tuple[list[str], str | None]:
    """User names + assistant name for the privacy gate, from live settings."""
    assistant = getattr(settings, "assistant", None)
    names: list[str] = []
    user_name = (getattr(assistant, "user_display_name", "") or "").strip()
    if user_name:
        names.append(user_name)
    aiko = (getattr(assistant, "name", "") or "").strip() or None
    return names, aiko


def collect(
    conn: sqlite3.Connection,
    *,
    kinds: tuple[str, ...],
    user_names: list[str],
    assistant_name: str | None,
    limit: int,
) -> dict[str, Any]:
    """Replay the live enqueue gates over stored rows. Writes nothing."""
    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"SELECT id, kind, content, metadata FROM memories "  # noqa: S608
        f"WHERE kind IN ({placeholders}) ORDER BY id DESC",
        kinds,
    ).fetchall()

    skips: Counter[str] = Counter()
    planned: list[dict[str, Any]] = []
    for row in rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
        except Exception:
            meta = {}
        if isinstance(meta, dict) and (
            meta.get("last_verified_at") or meta.get("last_checked_at")
        ):
            skips["already_adjudicated"] += 1
            continue

        content = (row["content"] or "").strip()
        if not content:
            skips["empty"] += 1
            continue

        decision = classify_memory_for_fact_check(
            kind=row["kind"],
            content=content,
            user_names=user_names,
            assistant_name=assistant_name,
        )
        if decision.personal:
            skips[f"privacy:{decision.reason}"] += 1
            continue

        claims = find_claims(content)
        if not claims:
            skips["no_extractable_claim"] += 1
            continue

        kept = []
        for claim in claims:
            # Gate only — the raw span is what gets queued, so this needs
            # the yes/no answer rather than a scrubbed rewrite.
            if not web_safe_probe(
                claim.text,
                user_names=user_names,
                assistant_name=assistant_name,
            ):
                skips["scrub_refused_span"] += 1
                continue
            kept.append(
                {
                    "text": claim.text,
                    "kind": claim.kind,
                    "sentence": claim.sentence,
                }
            )
        if not kept:
            skips["all_spans_scrubbed"] += 1
            continue

        planned.append(
            {
                "memory_id": int(row["id"]),
                "kind": row["kind"],
                "preview": content[:90],
                "claims": kept,
            }
        )
        if len(planned) >= limit:
            break

    return {
        "scanned": len(rows),
        "kinds": list(kinds),
        "planned_memories": len(planned),
        "planned_claims": sum(len(p["claims"]) for p in planned),
        "skipped": dict(skips.most_common()),
        "plan": planned,
    }


def render(data: dict[str, Any]) -> str:
    out = [
        f"scanned {data['scanned']} rows across {', '.join(data['kinds'])}",
        f"would queue {data['planned_claims']} claims "
        f"from {data['planned_memories']} memories",
        "",
        "skipped",
    ]
    for reason, n in (data["skipped"] or {}).items():
        out.append(f"  {reason:<34} {n}")
    if data["plan"]:
        out.append("")
        out.append("plan (newest first)")
        for item in data["plan"]:
            spans = ", ".join(
                f"{c['kind']}:{c['text']}" for c in item["claims"]
            )
            out.append(f"  #{item['memory_id']:<6} {item['preview']}")
            out.append(f"          -> {spans}")
    return "\n".join(out)


def apply(db: Path, plan: list[dict[str, Any]]) -> int:
    """Enqueue the planned claims. Returns how many were appended."""
    queue = FactCheckQueue(ChatDatabase(db))
    already = {
        (item.memory_id, item.claim_text) for item in queue.peek_all()
    }
    appended = 0
    for entry in plan:
        for claim in entry["claims"]:
            key = (int(entry["memory_id"]), claim["text"])
            if key in already:
                continue
            queue.enqueue(
                memory_id=int(entry["memory_id"]),
                claim_text=claim["text"],
                claim_kind=claim["kind"],
                claim_sentence=claim.get("sentence", ""),
            )
            already.add(key)
            appended += 1
    return appended


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually enqueue (default is a dry run that writes nothing)",
    )
    parser.add_argument(
        "--kinds",
        default=",".join(DEFAULT_KINDS),
        help=f"comma-separated memory kinds (default: {','.join(DEFAULT_KINDS)})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="cap on memories queued in one pass (default: 100)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"no database at {args.db}")

    kinds = tuple(k.strip() for k in str(args.kinds).split(",") if k.strip())
    if not kinds:
        sys.exit("no kinds given")

    user_names, assistant_name = _identity(load_settings())

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = collect(
            conn,
            kinds=kinds,
            user_names=user_names,
            assistant_name=assistant_name,
            limit=max(1, int(args.limit)),
        )
    finally:
        conn.close()

    if args.apply:
        appended = apply(args.db, data["plan"])
        data["enqueued"] = appended

    if args.json:
        # Pure JSON on stdout so the report stays pipeable.
        print(json.dumps(data, indent=2, default=str))
        return 0

    print(render(data))
    if args.apply:
        print()
        print(
            f"enqueued {data['enqueued']} claims. IdleFactChecker drains one "
            f"per tick under its rate limiter."
        )
    elif data["planned_claims"]:
        print()
        print("dry run -- nothing written. Re-run with --apply to queue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

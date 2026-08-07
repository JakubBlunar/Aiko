#!/usr/bin/env python3
"""One-off backfill: re-embed ``shared_moment`` rows from the bare summary.

``SharedMomentsStore`` used to embed the *rendered* content --
``"Shared moment (<vibe>): <summary>"`` -- so the identical prefix on every
row dominated the vector. The topic graph then clustered moments by **vibe
word** instead of by what happened: measured on a 145-moment corpus, one
cluster held 77 moments of which 76 were ``tender``, another held 38 with
27 ``playful``, and a third was 4/4 ``repair``. Every topical consumer
starved on that -- L7 rituals had minted exactly one concept from 145
moments, and L29 shared arcs could not be sourced at all.

The write path now embeds the summary alone (vibe stays a structured field,
which is how every consumer already reads it). This script brings existing
rows onto the new basis so the topic graph can re-cluster them by topic.

**Dry-run by default.** It reports what it would change and touches
nothing. Pass ``--apply`` to write.

    python scripts/reembed_shared_moments.py            # look
    python scripts/reembed_shared_moments.py --apply    # do it

Writes go through :class:`MemoryStore.update`, which re-upserts the LanceDB
mirror alongside the SQLite row, so retrieval stays in sync.

Stop the app before running with ``--apply`` -- this reaches around the
live writer, which is fine against a quiet database and a race against a
running one.

Afterwards the topic graph still holds the old clustering. Force a rebuild
(the ``force_topic_graph_rebuild`` MCP tool) before expecting arcs, then
``force_concept_synthesis``. Re-clustering shifts cluster ids, so the L8
user/aiko narrative passes will see their signatures go dirty and re-propose
once; that is self-healing via the existing watermarks.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.infra.settings import load_settings  # noqa: E402
from app.core.memory.memory_store import MemoryStore  # noqa: E402
from app.core.relationship.shared_moments import (  # noqa: E402
    _strip_summary_prefix,
)
from app.llm.embedder import build_embedder  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"
LANCE_ROOT = REPO_ROOT / "data" / "lancedb"

_SAMPLE = 8


def _summary_of(mem) -> str:
    """The text that should have been embedded: the bare summary."""
    meta = mem.metadata or {}
    what = str(meta.get("what") or "").strip()
    if what:
        return what
    return _strip_summary_prefix(mem.content).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the new embeddings (default is a dry run)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"path to chat_sessions.db (default {DEFAULT_DB})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if not args.db.exists():
        sys.exit(f"no database at {args.db}")

    settings = load_settings()
    embedder = build_embedder(settings.llm)
    store = MemoryStore(args.db)

    rag = None
    if args.apply:
        try:
            from app.core.rag.rag_store import auto_open as rag_auto_open

            rag = rag_auto_open(
                LANCE_ROOT,
                embedder_model=embedder.model,
                embedder_probe=embedder,
            )
        except Exception as exc:
            print(f"warning: LanceDB unavailable ({exc}); SQLite only")
            rag = None
        if rag is not None:
            store.attach_rag_store(rag)

    rows = store.iter_by_kind("shared_moment")
    if not rows:
        print("no shared_moment rows; nothing to do")
        return 0

    vibes: Counter[str] = Counter()
    plan: list[tuple[int, str]] = []
    skipped = 0
    for mem in rows:
        summary = _summary_of(mem)
        if len(summary) < 4:
            skipped += 1
            continue
        vibes[str((mem.metadata or {}).get("vibe") or "?")] += 1
        plan.append((int(mem.id), summary))

    print(f"{len(rows)} shared_moment rows, {len(plan)} to re-embed, {skipped} skipped")
    print("vibes: " + ", ".join(f"{v}x{n}" for v, n in vibes.most_common()))
    print("\nsample of the text that will be embedded:")
    for mid, summary in plan[:_SAMPLE]:
        trimmed = summary if len(summary) <= 70 else summary[:67] + "..."
        print(f"  #{mid}: {trimmed}")

    if not args.apply:
        print("\ndry run -- pass --apply to write")
        return 0

    done = 0
    failed = 0
    for mid, summary in plan:
        try:
            vector = embedder.embed(summary)
        except Exception as exc:
            print(f"  #{mid}: embed failed ({exc})")
            failed += 1
            continue
        if store.update(mid, embedding=vector) is None:
            print(f"  #{mid}: update returned None")
            failed += 1
            continue
        done += 1
        if done % 25 == 0:
            print(f"  ... {done}/{len(plan)}")

    print(f"\nre-embedded {done}, failed {failed}")
    print(
        "next: force_topic_graph_rebuild, then force_concept_synthesis"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

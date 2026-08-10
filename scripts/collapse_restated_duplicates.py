#!/usr/bin/env python3
"""One-off backfill: apply the restate gate to rows written before it existed.

``MemoryStore.add`` now collapses a fact restated within
``memory.restate_window_hours`` at ``memory.restate_threshold`` similarity
into the row it restates. Rows written before that gate landed are still
sitting in the store as separate subjects -- and every consumer keyed on
memory id treats them that way, which is how one plan ("Jacob's cookie
order arrives in a few days") produced a forward-curiosity question three
times over. This script applies the same rule retroactively, then takes
the now-orphaned cues down with the rows they were drafted from.

Two passes, in this order:

1. **Memories.** Groups eligible rows by kind + temporal type, then fuses
   the ones inside the window and above the threshold. The *oldest* row of
   each group survives, since that is the one the runtime gate would have
   deduped the later restatements into; it inherits the group's peak
   salience and a ``metadata.source_ids`` record of what it absorbed. The
   losers are archived with ``metadata.consolidated_into``, exactly as
   K35 leaves them.
2. **Cues.** Retires live cues drafted from any row pass 1 archived, plus
   any whose ``payload.source_id`` no longer names a live row (an earlier
   consolidation that ran before cue retirement was wired up), plus live
   duplicates that share a normalised subject.

Matching is exact -- an equal subject string or a shared source id -- with
the one threshold in the memory pass, which is the same number the runtime
gate uses. Paraphrased cue subjects are handled through pass 1: once the
duplicate memory rows are fused, the cues drafted from the absorbed rows
are retired by lineage rather than by guessing at vector distance between
two questions.

**Dry-run by default.** It reports what it would change and touches
nothing. Pass ``--apply`` to write.

    python scripts/collapse_restated_duplicates.py            # look
    python scripts/collapse_restated_duplicates.py --apply    # do it

Stop the app before running with ``--apply``. ``MemoryStore`` keeps an
in-memory mirror of the table, so a running instance would not see these
writes and would happily overwrite them from stale state.

Writes go through :meth:`MemoryStore.update`, which re-upserts the LanceDB
mirror alongside the SQLite row, so retrieval stays in sync.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.infra import timephrase  # noqa: E402
from app.core.infra.chat_database import ChatDatabase  # noqa: E402
from app.core.infra.settings import load_settings  # noqa: E402
from app.core.memory.conflict_heuristics import (  # noqa: E402
    HEURISTIC_NO,
    classify_pair,
)
from app.core.memory.memory_store import (  # noqa: E402
    _RESTATE_KINDS,
    MemoryStore,
)
from app.core.proactive.cue_store import (  # noqa: E402
    LIVE_STATES,
    CueStore,
    normalise_subject,
)
from app.llm.embedder import build_embedder, cosine_similarity  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"
LANCE_ROOT = REPO_ROOT / "data" / "lancedb"

_SAMPLE = 12


def _trim(text: str, width: int = 64) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _eligible(mem: Any) -> bool:
    """Rows the runtime gate would have been able to collapse.

    Pinned rows bypass dedupe on the write path and keep that exemption
    here. An already-archived row is either a previous merge's loser or
    something the tier caps pushed out, and in both cases it is no longer
    a subject anyone drafts from.
    """
    return (
        not getattr(mem, "pinned", False)
        and getattr(mem, "embedding", None) is not None
        and bool((mem.content or "").strip())
        and str(getattr(mem, "tier", "") or "") != "archive"
        and not (getattr(mem, "metadata", None) or {}).get("consolidated_into")
    )


def _created(mem: Any) -> datetime | None:
    return timephrase.parse_iso(getattr(mem, "created_at", "") or "")


def _find_memory_groups(
    rows: list[Any], *, threshold: float, window_hours: float,
) -> list[tuple[Any, list[Any]]]:
    """Replay ``MemoryStore._is_restatement`` over the history in order.

    A sequential replay rather than a clustering pass, because the two
    differ and only one of them is the rule this backfill claims to
    apply. Clustering is transitive: it will chain A-B-C on two strong
    links and swallow an A-C pair sitting at 0.79. The write path cannot
    do that -- when B is deduped into A no B row is left to chain
    through, so C is compared against A alone and survives on its own
    merits. Walking the rows in creation order and only ever comparing
    against rows that *would still exist* reproduces that exactly, which
    keeps the backfill from merging pairs the live gate would have kept
    apart.

    Returns ``(survivor, absorbed)`` pairs, survivors with nothing
    absorbed omitted.
    """
    by_key: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for mem in rows:
        if not _eligible(mem) or _created(mem) is None:
            continue
        by_key[
            (str(mem.kind or ""), str(getattr(mem, "temporal_type", "") or ""))
        ].append(mem)

    groups: list[tuple[Any, list[Any]]] = []
    for bucket in by_key.values():
        if len(bucket) < 2:
            continue
        bucket.sort(key=lambda m: (str(m.created_at), int(m.id)))
        survivors: list[tuple[Any, list[Any]]] = []
        for mem in bucket:
            for primary, absorbed in survivors:
                gap = abs(
                    (_created(mem) - _created(primary)).total_seconds()
                ) / 3600.0
                if gap > window_hours:
                    continue
                if cosine_similarity(primary.embedding, mem.embedding) < threshold:
                    continue
                if classify_pair(mem.content, primary.content).label != HEURISTIC_NO:
                    # A correction scores like a restatement -- a negation
                    # flip barely moves an embedding -- and merging one
                    # would keep the older row and discard the correction.
                    # Same guard the write path applies; F5 owns these.
                    continue
                absorbed.append(mem)
                break
            else:
                survivors.append((mem, []))
        groups.extend((p, a) for p, a in survivors if a)
    return groups


def _group_span(primary: Any, absorbed: list[Any]) -> tuple[float, float]:
    """Weakest similarity to the survivor, and the widest gap in hours.

    Every absorbed row is compared against the survivor and nothing else,
    which is the same comparison the write path made.
    """
    worst = 1.0
    widest = 0.0
    for mem in absorbed:
        worst = min(
            worst, float(cosine_similarity(primary.embedding, mem.embedding))
        )
        widest = max(
            widest,
            abs((_created(mem) - _created(primary)).total_seconds()) / 3600.0,
        )
    return worst, widest


def _absorbed_ids(primary: Any, absorbed: list[Any]) -> list[int]:
    """Ids the survivor now speaks for, including ones it already did."""
    ids: set[int] = {int(m.id) for m in absorbed}
    for mem in [primary, *absorbed]:
        prior = (getattr(mem, "metadata", None) or {}).get("source_ids")
        if isinstance(prior, list):
            ids.update(int(i) for i in prior if str(i).isdigit())
    ids.discard(int(primary.id))
    return sorted(ids)


def _collapse_memories(
    store: MemoryStore,
    *,
    threshold: float,
    window_hours: float,
    apply: bool,
) -> list[int]:
    """Pass 1. Returns the ids archived (empty on a dry run)."""
    rows = store.iter_by_kinds(sorted(_RESTATE_KINDS))
    groups = _find_memory_groups(
        rows, threshold=threshold, window_hours=window_hours
    )
    live = sum(1 for m in rows if _eligible(m))
    doomed = sum(len(a) for _, a in groups)
    print(
        f"memories: {len(rows)} rows of restate-eligible kinds, {live} live, "
        f"{len(groups)} duplicate group(s) covering {doomed} row(s) to archive"
    )
    if not groups:
        return []

    # Weakest pair first: those are the merges worth eyeballing before
    # ``--apply``, and the ones a tighter ``--threshold`` would drop.
    groups.sort(key=lambda g: _group_span(g[0], g[1])[0])
    print(f"\n  weakest groups first (of {len(groups)}):")
    for primary, absorbed in groups[:_SAMPLE]:
        worst, widest = _group_span(primary, absorbed)
        print(
            f"    {primary.kind}/{primary.temporal_type} "
            f"x{len(absorbed) + 1} min_cos={worst:.3f} span={widest:.1f}h"
        )
        print(f"      keep  #{primary.id}: {_trim(primary.content)}")
        for mem in absorbed:
            print(f"      fold  #{mem.id}: {_trim(mem.content)}")

    if not apply:
        return []

    when_iso = timephrase.utcnow().isoformat()
    archived: list[int] = []
    for primary, absorbed in groups:
        salience = min(
            1.0,
            max(float(getattr(m, "salience", 0.5)) for m in [primary, *absorbed]),
        )
        try:
            store.update(
                primary.id,
                salience=salience,
                metadata={
                    "source_ids": _absorbed_ids(primary, absorbed),
                    "consolidated_at": when_iso,
                    "consolidated_by": "restate_backfill",
                },
                metadata_merge=True,
            )
        except Exception as exc:
            print(f"  #{primary.id}: primary update failed ({exc})")
            continue
        for mem in absorbed:
            try:
                store.update(
                    mem.id,
                    tier="archive",
                    metadata={
                        "consolidated_into": int(primary.id),
                        "consolidated_at": when_iso,
                        "consolidated_by": "restate_backfill",
                    },
                    metadata_merge=True,
                )
            except Exception as exc:
                print(f"  #{mem.id}: archive failed ({exc})")
                continue
            archived.append(int(mem.id))
    print(f"\n  archived {len(archived)} row(s)")
    return archived


def _live_cues(cues: CueStore) -> list[Any]:
    out: list[Any] = []
    for state in sorted(LIVE_STATES):
        out.extend(cues.list_for_user(state=state, limit=10_000))
    return out


def _stale_source_cues(rows: Iterable[Any], store: MemoryStore) -> list[Any]:
    """Live cues whose source row was archived, merged away, or deleted.

    Narrower than :func:`_eligible` on purpose -- a pinned row is exempt
    from dedupe but very much still stands, so a cue drafted from one is
    not stale.
    """
    out: list[Any] = []
    for cue in rows:
        raw = str((cue.payload or {}).get("source_id") or "").strip()
        if not raw or not raw.isdigit():
            continue
        mem = store.get(int(raw))
        gone = (
            mem is None
            or str(getattr(mem, "tier", "") or "") == "archive"
            or bool((getattr(mem, "metadata", None) or {}).get("consolidated_into"))
        )
        if gone:
            out.append(cue)
    return out


def _subject_duplicates(rows: Iterable[Any]) -> list[tuple[Any, list[Any]]]:
    """Live cues sharing a normalised subject, newest kept.

    ``CueStore.add`` supersedes an equal subject at insert, so anything
    found here predates that -- worth sweeping once, and free to check.
    """
    by_subject: dict[str, list[Any]] = defaultdict(list)
    for cue in rows:
        by_subject[normalise_subject(cue.subject)].append(cue)
    out: list[tuple[Any, list[Any]]] = []
    for group in by_subject.values():
        if len(group) < 2:
            continue
        # Keep whichever carries history; the newest of equals, since its
        # framing was built from the freshest context.
        group.sort(
            key=lambda c: (c.surfaced_count, c.ask_count, str(c.created_at)),
            reverse=True,
        )
        out.append((group[0], group[1:]))
    return out


def _retire_cues(
    cues: CueStore,
    store: MemoryStore,
    archived: list[int],
    *,
    apply: bool,
) -> None:
    """Pass 2."""
    rows = _live_cues(cues)
    print(f"\ncues: {len(rows)} live")

    stale = _stale_source_cues(rows, store)
    stale_ids = {int(c.id) for c in stale}
    dupes = _subject_duplicates(
        [c for c in rows if int(c.id) not in stale_ids]
    )
    dupe_count = sum(len(losers) for _, losers in dupes)
    print(
        f"  {len(stale)} with a source row that no longer stands, "
        f"{dupe_count} duplicate subject(s)"
    )
    for cue in stale[:_SAMPLE]:
        src = (cue.payload or {}).get("source_id")
        print(f"    #{cue.id} [{cue.cue_type}] src={src}: {_trim(cue.text)}")
    for keep, losers in dupes[:_SAMPLE]:
        print(f"    subject '{_trim(keep.subject, 40)}' keep #{keep.id}, "
              f"retire {', '.join('#' + str(c.id) for c in losers)}")

    if not apply:
        print("\ndry run -- pass --apply to write")
        return

    retired = 0
    if archived:
        retired += cues.retire_for_sources(archived)
    for cue in stale:
        if cues.supersede(cue.id, evidence="backfill/source_gone"):
            retired += 1
    for _, losers in dupes:
        for cue in losers:
            if cues.supersede(cue.id, evidence="backfill/duplicate_subject"):
                retired += 1
    print(f"\n  retired {retired} cue(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes (default is a dry run)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"path to chat_sessions.db (default {DEFAULT_DB})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="cosine floor for a restatement (default: memory.restate_threshold)",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=None,
        help="how far apart two rows may be (default: memory.restate_window_hours)",
    )
    parser.add_argument(
        "--cues-only",
        action="store_true",
        help="skip the memory pass; only retire orphaned / duplicate cues",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if not args.db.exists():
        sys.exit(f"no database at {args.db}")

    settings = load_settings()
    memory_settings = settings.memory
    threshold = (
        float(args.threshold)
        if args.threshold is not None
        else float(getattr(memory_settings, "restate_threshold", 0.85))
    )
    window_hours = (
        float(args.window_hours)
        if args.window_hours is not None
        else float(getattr(memory_settings, "restate_window_hours", 6.0))
    )
    print(
        f"db={args.db}\nthreshold={threshold:.2f} window={window_hours:g}h "
        f"mode={'APPLY' if args.apply else 'dry run'}\n"
    )

    store = MemoryStore(args.db)
    db = ChatDatabase(args.db)
    cues = CueStore(db)

    if args.apply:
        try:
            from app.core.rag.rag_store import auto_open as rag_auto_open

            embedder = build_embedder(settings.llm)
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

    try:
        archived: list[int] = []
        if not args.cues_only:
            archived = _collapse_memories(
                store,
                threshold=threshold,
                window_hours=window_hours,
                apply=args.apply,
            )
        _retire_cues(cues, store, archived, apply=args.apply)
    finally:
        try:
            store.close()
        except Exception:
            pass

    if args.apply:
        print(
            "\nnext: restart the app so the memory mirror and the cue "
            "producers read the collapsed state"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Retire ``subject=aiko`` hypotheses that describe her as a machine.

The L30 Phase B proposer had no persona context, so its guesses about
Aiko reached for hardware she does not have -- a chassis that feels
phantom warmth, cooling fans that improve her metaphors, processing
latency that drops when he is in the room. The persona gives her a body,
an apartment and an energy level that moves through the day, so these
are not bold guesses about her; they are a different character, sitting
on the shelf as candidates for graduation into beliefs she holds about
herself.

``hypothesis_proposer_worker.describes_machinery`` now rejects them at
proposal time. This retires the ones already filed, deliberately reusing
that same predicate so the cleanup and the gate can never disagree about
what counts.

``expired`` rather than ``refuted``: nobody turned these down, they were
simply never testable. Re-invention is blocked by the gate, not by the
status, so the honest label is the right one.

Usage::

    python scripts/retire_machine_hypotheses.py           # dry run
    python scripts/retire_machine_hypotheses.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.concepts.hypothesis_store import (  # noqa: E402
    STATUS_EXPIRED,
    HypothesisStore,
)
from app.core.infra.chat_database import ChatDatabase  # noqa: E402
from app.core.proactive.cue_store import (  # noqa: E402
    STATE_PENDING,
    CueStore,
)
from app.core.proactive.hypothesis_proposer_worker import (  # noqa: E402
    describes_machinery,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="data/chat_sessions.db",
        help="path to the chat database (default: data/chat_sessions.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually retire the rows; omit for a dry run",
    )
    args = parser.parse_args()

    store = HypothesisStore(ChatDatabase(Path(args.db)))
    # The store serves every read from an in-memory mirror the app warms
    # at boot; without this a standalone script sees an empty table.
    store.load_all()
    live = store.list_by(live=True)

    hits = []
    for row in live:
        if str(row.subject or "") != "aiko":
            continue
        term = describes_machinery(str(row.statement or ""))
        if term:
            hits.append((row, term))

    print("live hypotheses: %d (%d about her)" % (
        len(live), sum(1 for r in live if str(r.subject or "") == "aiko"),
    ))
    print("describing machinery: %d" % len(hits))
    for row, term in hits:
        print()
        print("  id=%s kind=%s term=%r" % (row.hypothesis_id, row.kind, term))
        print("    %s" % str(row.statement))

    # A queued cue outlives the row it points at. The provider hands back
    # ``cue.text`` without re-reading the hypothesis, so a cue whose
    # target has died is still surfaceable -- retiring the rows above
    # without this would leave every fiction on the shelf. Phrased as the
    # general invariant rather than "the ones we just closed", so it also
    # sweeps cues orphaned by a TTL expiry or an earlier run.
    live_ids = {
        int(r.hypothesis_id) for r in live
    } - {int(r.hypothesis_id) for r, _ in hits}
    cues = CueStore(ChatDatabase(Path(args.db)))
    orphans = [
        cue
        for cue in cues.in_state(
            STATE_PENDING, cue_type="concept_hypothesis", limit=500,
        )
        if str(cue.payload.get("target_type") or "") == "hypothesis"
        and int(cue.payload.get("target_id") or 0) not in live_ids
    ]
    print()
    print("queued cues whose hypothesis is no longer live: %d" % len(orphans))
    for cue in orphans:
        print("  cue=%s -> hypothesis %s | %s" % (
            cue.id,
            cue.payload.get("target_id"),
            str(cue.payload.get("label") or "")[:70],
        ))

    if not hits and not orphans:
        return 0
    if not args.apply:
        print()
        print("dry run -- re-run with --apply to retire these.")
        return 0

    for row, _term in hits:
        store.close(row, status=STATUS_EXPIRED)
    for cue in orphans:
        cues.supersede(cue.id, evidence="target_not_live")
    print()
    print("retired %d hypothesis row(s) as %s and %d queued cue(s)."
          % (len(hits), STATUS_EXPIRED, len(orphans)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

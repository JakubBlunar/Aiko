"""Keep forward-curiosity wants attached to the cue that owns their lifetime."""
from __future__ import annotations

from dataclasses import replace

def forward_text(row):
    from app.core.proactive.forward_curiosity_worker import source_context

    context = source_context({**row.payload, "topic": row.payload.get("topic") or row.subject})
    return row.text + ("\n" + context if context and context not in row.text else "")


def refresh_forward_wants(state, store):
    """Project availability, including old fc: references, without changing the pool.

    The journal is historical output, not an unspent shelf. Awaiting, resolved,
    expired and delayed cues must not keep growing a second desire to ask.
    """
    if store is None:
        return state
    try:
        rows = store.pending("forward_curiosity", limit=64)
        by_id = {f"cue:{row.id}": row for row in rows}
        by_source = {f"fc:{row.payload.get('source_id')}": row for row in rows}
    except Exception:
        rows, by_id, by_source = [], {}, {}
    wants = []
    for want in state.wants:
        if want.source != "forward_curiosity":
            wants.append(want)
            continue
        row = by_id.get(want.source_ref) or by_source.get(want.source_ref)
        if row is not None:
            wants.append(replace(want, source_ref=f"cue:{row.id}", text=forward_text(row)))
    return replace(state, wants=tuple(wants))


def claim_forward_wants(session, wants, *, block):
    """Use the ordinary staged claim, bound to the block carrying this offer."""
    result = []
    for want in wants:
        if want.source != "forward_curiosity" or not want.source_ref.startswith("cue:"):
            result.append(want)
            continue
        row = session.take_pool_cue(
            "forward_curiosity", cue_id=int(want.source_ref.split(":", 1)[1]),
            claim_block=block,
        )
        if row is not None:
            result.append(want)
    return tuple(result)

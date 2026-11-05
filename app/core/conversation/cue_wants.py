"""Keep cue-derived wants attached to their source and its quality contract."""
from __future__ import annotations

from dataclasses import replace

def forward_text(row):
    from app.core.proactive.forward_curiosity_worker import source_context

    context = source_context({**row.payload, "topic": row.payload.get("topic") or row.subject})
    return row.text + ("\n" + context if context and context not in row.text else "")


def seed_text(row):
    text = row.payload.get("prompt_text") or row.text or row.subject
    why = row.payload.get("why")
    value = (row.payload.get("quality") or {}).get("useful_because")
    return (str(text) + (f"\nConnection: {why}" if isinstance(why, str) and why else "")
            + (f"\nWhat it could add: {value}" if isinstance(value, str) and value else ""))


def refresh_forward_wants(state, store):
    """Project availability, including old fc: references, without changing the pool.

    Forwarded questions share the cue's lifetime. Seed wants may outlive pool
    expiry, but must retain a usable source and its full direction.
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
        if want.source == "curiosity_seed" and want.source_ref.startswith("cue:"):
            from app.core.proactive.cue_quality import seed_usable

            try:
                seed = store.get(int(want.source_ref.split(":", 1)[1]))
            except Exception:
                wants.append(want)
                continue
            if (seed is not None and seed.state not in {"used", "superseded"}
                    and seed_usable(seed.payload)):
                wants.append(replace(want, text=seed_text(seed)))
            continue
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
        if want.source == "curiosity_seed" and want.source_ref.startswith("cue:"):
            from app.core.proactive.cue_quality import seed_usable

            store = session._cue_pool_store()
            try:
                seed = store.get(int(want.source_ref.split(":", 1)[1])) if store else None
            except Exception:
                seed = None
            if (seed is None or not seed_usable(seed.payload)
                    or seed.state in {"used", "superseded"}):
                continue
            if seed.state != "expired" and session.take_pool_cue(
                "curiosity_seed", cue_id=seed.id, claim_block=block,
            ) is None:
                continue
            result.append(replace(want, text=seed_text(seed)))
            continue
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

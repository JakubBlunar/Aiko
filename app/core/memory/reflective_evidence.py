"""Bounded generation context for inner life; derivation is not observation."""
from copy import deepcopy
from types import SimpleNamespace

from app.core.infra import timephrase


def eligible(row):
    meta = getattr(row, "metadata", None) or {}
    return (not (meta.get("superseded_by") or meta.get("superseded_reason")
                 or meta.get("consolidated_into"))
            and meta.get("belief_revision") not in {"inaccurate", "superseded"})


def revision(row):
    from app.core.memory.memory_relations import reference_revision
    from app.core.concepts.concept_support import revision_hash

    return revision_hash([reference_revision(row),
                          (getattr(row, "metadata", None) or {}).get("reflective_derivation")])


def capture(rows, *, max_chars=5000, limit=8):
    """Pack whole qualified records. Never retain an ID for an omitted row."""
    from app.core.memory.memory_origin import origin_annotation

    lines, inputs, snapshots = [], [], {}
    remaining = max_chars
    for row in rows:
        mid = getattr(row, "id", None)
        if (type(mid) is not int or mid in snapshots or len(snapshots) >= limit
                or not eligible(row)):
            continue
        snap = SimpleNamespace(**{key: deepcopy(getattr(row, key, default)) for key, default in (
            ("id", mid), ("content", ""), ("kind", ""), ("metadata", {}), ("created_at", ""),
            ("event_time", None), ("temporal_type", "durable"), ("provenance", "inferred"),
            ("relevance_until", None), ("source_message_id", None),
        )})
        line = (f"ID {mid} " + timephrase.format_memory_line(snap)
                + " [" + origin_annotation(snap) + "]")
        if not snap.content or len(line) > remaining:
            continue
        lines.append(line)
        remaining -= len(line) + 1
        inputs.append({"memory_id": mid, "revision": revision(row)})
        snapshots[mid] = snap
    return "\n".join(lines), {"version": 1, "scope": "generation_context",
                              "memory_inputs": inputs}, snapshots


def current(receipt, store):
    from app.core.memory.observation_receipts import longitudinal_sources_current

    return all((row := store.get(item["memory_id"])) is not None
               and eligible(row) and revision(row) == item["revision"]
               and longitudinal_sources_current(row, store)
               for item in receipt.get("memory_inputs", []))

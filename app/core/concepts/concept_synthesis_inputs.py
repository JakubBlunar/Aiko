"""Delta-first individual evidence selection for concept synthesis."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(slots=True)
class MemoryBatch:
    rows: list[Any]
    fingerprints: dict[str, str]
    pending: int
    remaining: int
    origin_max_id: int


def select_memory_batch(
    population: Sequence[Any],
    previous: dict[str, Any],
    *,
    excluded_ids: set[int],
    limit: int,
    force: bool = False,
    metadata_keys: tuple[str, ...] = (),
) -> MemoryBatch:
    """Drain unseen/changed rows without checkpointing past unoffered evidence."""
    rows = [row for row in population if int(row.id) not in excluded_ids]
    current = {
        str(row.id): hashlib.sha256(
            json.dumps(
                [
                    row.content,
                    getattr(row, "kind", ""),
                    getattr(row, "event_time", None),
                    getattr(row, "created_at", ""),
                    {key: (getattr(row, "metadata", None) or {}).get(key) for key in metadata_keys},
                ],
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:24]
        for row in rows
    }
    old = previous.get("mem_fingerprints", {})
    if not isinstance(old, dict):
        old = {}
    committed = {key: value for key, value in old.items() if key in current}
    origin = int(
        previous.get(
            "mem_origin_max_id",
            previous.get("mem_max_id", previous.get("max_id", 0)),
        )
    )
    pending = sorted(
        (row for row in rows if committed.get(str(row.id)) != current[str(row.id)]),
        key=lambda row: (int(row.id) <= origin, int(row.id)),
    )
    anchors = sorted(
        (row for row in rows if committed.get(str(row.id)) == current[str(row.id)]),
        key=lambda row: (-float(getattr(row, "salience", 0.0)), int(row.id)),
    )
    cap = max(1, int(limit))
    reserve = min(len(anchors), max(1, cap // 4)) if pending and cap > 1 else 0
    selected = pending[: cap - reserve]
    selected.extend(anchors[: cap - len(selected)])
    if force and not pending:
        selected = anchors[:cap]
    for row in selected:
        committed[str(row.id)] = current[str(row.id)]
    pending_ids = {int(row.id) for row in pending}
    processed = sum(1 for row in selected if int(row.id) in pending_ids)
    return MemoryBatch(selected, committed, len(pending), len(pending) - processed, origin)

"""L17f: append-only store for the evolution diary.

The diary is the human-legible face of ``concept_learning_events``: where
L17e lets you drill into one belief's history, an entry here says "over
this stretch, here is how I changed" in Aiko's own voice, grounded only in
the ``because`` clauses of the events it cites.

Three properties the worker above it relies on:

- **Append-only, never pruned.** Same reasoning as the learning events it
  narrates -- an old era going mute is a loss, not a saving.
- **Watermarked.** ``event_watermark`` is the highest learning-event id an
  entry accounts for, so composition resumes exactly where it stopped.
  Nothing is narrated twice and nothing is skipped.
- **Gaps are meaningful.** There is no placeholder entry. A period with no
  above-noise change simply has no row, which is what makes a period that
  *does* have one worth reading.

Never raises: every method logs and degrades, because losing a diary write
must not be able to break a worker tick.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.evolution_diary")

_COLS = (
    "id, entry, period_start, period_end, event_watermark, "
    "learning_event_ids, concept_ids, shape_counts, salience_max, created_at"
)


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _load(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


@dataclass(slots=True)
class DiaryEntry:
    """One period's account of how Aiko's understanding moved."""

    entry: str = ""
    period_start: str = ""
    period_end: str = ""
    event_watermark: int = 0
    learning_event_ids: tuple[int, ...] = ()
    concept_ids: tuple[int, ...] = ()
    shape_counts: dict[str, int] = field(default_factory=dict)
    salience_max: float = 0.0
    created_at: str = ""
    entry_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the REST / MCP surfaces."""
        return {
            "id": self.entry_id,
            "entry": self.entry,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "event_watermark": self.event_watermark,
            # The provenance that makes each line inspectable: the UI
            # resolves these through the L17e drill-down.
            "learning_event_ids": list(self.learning_event_ids),
            "concept_ids": list(self.concept_ids),
            "shape_counts": dict(self.shape_counts),
            "salience_max": round(float(self.salience_max), 4),
            "created_at": self.created_at,
        }


class EvolutionDiaryStore:
    """Append-only CRUD over ``evolution_diary``."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes (append-only) ──────────────────────────────────────────

    def add(self, entry: DiaryEntry) -> int:
        """Append one entry. Returns its id, or ``0`` on refusal/failure.

        An empty body is refused rather than stored: a blank entry would
        show up in the UI as a period Aiko had nothing to say about, which
        is exactly the filler the skip rule exists to prevent.
        """
        body = str(entry.entry or "").strip()
        if not body:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        entry.created_at = entry.created_at or _now_iso()
        try:
            cursor = conn.execute(
                "INSERT INTO evolution_diary "
                "(entry, period_start, period_end, event_watermark, "
                " learning_event_ids, concept_ids, shape_counts, "
                " salience_max, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    body,
                    str(entry.period_start or ""),
                    str(entry.period_end or ""),
                    int(entry.event_watermark),
                    _dump([int(i) for i in entry.learning_event_ids]),
                    _dump([int(i) for i in entry.concept_ids]),
                    _dump(
                        {str(k): int(v) for k, v in entry.shape_counts.items()}
                    ),
                    float(entry.salience_max),
                    entry.created_at,
                ),
            )
            conn.commit()
        except Exception:
            log.warning("diary entry insert failed", exc_info=True)
            return 0
        entry.entry_id = int(cursor.lastrowid or 0)
        return entry.entry_id

    # ── reads ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_entry(r: tuple) -> DiaryEntry:
        counts = _load(r[7], {})
        return DiaryEntry(
            entry_id=int(r[0]),
            entry=str(r[1] or ""),
            period_start=str(r[2] or ""),
            period_end=str(r[3] or ""),
            event_watermark=int(r[4] or 0),
            learning_event_ids=tuple(
                int(i) for i in _load(r[5], []) if isinstance(i, int)
            ),
            concept_ids=tuple(
                int(i) for i in _load(r[6], []) if isinstance(i, int)
            ),
            shape_counts=(
                {str(k): int(v) for k, v in counts.items()}
                if isinstance(counts, dict)
                else {}
            ),
            salience_max=float(r[8] or 0.0),
            created_at=str(r[9] or ""),
        )

    def list(
        self, *, limit: int = 50, before_id: int | None = None
    ) -> list[DiaryEntry]:
        """Entries newest-first; ``before_id`` pages backwards."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        clause = "WHERE id < ?" if before_id is not None else ""
        params: list[object] = []
        if before_id is not None:
            params.append(int(before_id))
        params.append(max(1, int(limit)))
        try:
            rows = conn.execute(
                f"SELECT {_COLS} FROM evolution_diary "
                f"{clause} ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("diary list failed", exc_info=True)
            return []
        return [self._row_to_entry(r) for r in rows]

    def latest(self) -> DiaryEntry | None:
        entries = self.list(limit=1)
        return entries[0] if entries else None

    def latest_watermark(self) -> int:
        """Highest learning-event id any entry has already accounted for.

        The resume point for the next composition. ``MAX`` rather than the
        newest row's value so a hand-inserted or out-of-order entry cannot
        rewind the diary into re-narrating the same changes.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT MAX(event_watermark) FROM evolution_diary"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0] or 0) if row else 0

    def count(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM evolution_diary"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def cited_concept_ids(self, *, limit: int = 50) -> list[int]:
        """Concept ids cited across the most recent entries, deduplicated."""
        seen: list[int] = []
        for entry in self.list(limit=limit):
            for cid in entry.concept_ids:
                if cid not in seen:
                    seen.append(int(cid))
        return seen

    def entries_since(self, watermark: int) -> Sequence[DiaryEntry]:
        """Entries composed after a given learning-event watermark."""
        return [
            entry
            for entry in self.list(limit=200)
            if entry.event_watermark > int(watermark)
        ]


__all__ = ["DiaryEntry", "EvolutionDiaryStore"]

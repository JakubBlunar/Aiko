"""Append-only discovery timeline for the higher-order concept layer.

A **concept event** records a moment in a concept's life -- for v1, the
moment Aiko first *discovers* (synthesises) it. The point is a
scrollable, years-long timeline of her "aha!" moments: watching her
understanding of herself and the user form and evolve.

Two deliberate design choices, both mirrored in the ``concept_events``
DDL (see :mod:`app.core.infra.chat_database`):

- **Append-only.** Events are only ever inserted, never mutated. The
  store exposes ``add`` + read helpers, no ``update`` / ``delete``.
- **Decoupled from the concept lifecycle.** ``concept_id`` is a soft
  reference that is *not* cascade-deleted, and ``label`` snapshots the
  concept text at event time, so deleting or relabelling a concept
  still leaves its discovery standing in the timeline.

Unlike :class:`app.core.concepts.concept_store.ConceptStore` there is no
in-process cosine mirror: events are never searched by similarity, only
read back in reverse-chronological order for the debug timeline, so this
talks to SQLite directly and orders by ``created_at``.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.concept_event_store")

_EVENT_COLS = (
    "id, concept_id, event_type, kind, subject, label, confidence, "
    "novelty, evidence_count, distinct_source_count, source_kinds, "
    "reason, created_at"
)


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


@dataclass(slots=True)
class ConceptEvent:
    """One row in the concept discovery timeline.

    ``event_type`` is an open enum. Emitted today: ``discovered`` (L2
    synthesis), ``promoted`` / ``demoted`` / ``dormant`` / ``retired`` /
    ``revived`` / ``contradicted`` / ``plasticity_shift`` /
    ``reinforced`` / ``confidence_sample`` (L3 lifecycle), and ``merged``
    (L2 consolidation). ``confidence_sample`` is the L17a trail marker:
    a concept that slowly decays without crossing a status threshold
    emits nothing else, so its downward path would be invisible to
    :meth:`ConceptEventStore.trajectory`. ``demoted`` is the
    structural counterpart to ``dormant``: the belief did not fade, its
    supporting evidence was reconciled away and it no longer rests on
    anything. ``novelty`` is ``1 - cosine`` to the
    nearest existing concept of the same subject/kind at synthesis time
    (``1.0`` for a first-of-its-kind, and ``0.0`` for historical rows
    backfilled from pre-timeline concepts).
    """

    event_type: str = "discovered"
    kind: str = "identity"
    subject: str = "user"
    label: str = ""
    confidence: float = 0.0
    novelty: float = 0.0
    evidence_count: int = 0
    distinct_source_count: int = 0
    source_kinds: str = ""
    reason: str = ""
    concept_id: int | None = None
    created_at: str = ""
    event_id: int = 0


class ConceptEventStore:
    """Append-only CRUD for the ``concept_events`` timeline table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes (append-only) ──────────────────────────────────────────

    def add(self, event: ConceptEvent) -> int:
        """Insert one timeline event, populate its ``event_id``, return it."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        event.created_at = event.created_at or _now_iso()
        try:
            cursor = conn.execute(
                "INSERT INTO concept_events "
                "(concept_id, event_type, kind, subject, label, confidence, "
                " novelty, evidence_count, distinct_source_count, "
                " source_kinds, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (int(event.concept_id)
                     if event.concept_id is not None else None),
                    str(event.event_type),
                    str(event.kind),
                    str(event.subject),
                    str(event.label or ""),
                    float(event.confidence),
                    float(event.novelty),
                    int(event.evidence_count),
                    int(event.distinct_source_count),
                    str(event.source_kinds or ""),
                    str(event.reason or ""),
                    event.created_at,
                ),
            )
            conn.commit()
        except Exception:
            log.warning("concept event insert failed", exc_info=True)
            return 0
        event.event_id = int(cursor.lastrowid or 0)
        return event.event_id

    # ── reads ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(r: tuple) -> ConceptEvent:
        return ConceptEvent(
            event_id=int(r[0]),
            concept_id=(int(r[1]) if r[1] is not None else None),
            event_type=str(r[2] or "discovered"),
            kind=str(r[3] or "identity"),
            subject=str(r[4] or "user"),
            label=str(r[5] or ""),
            confidence=float(r[6] or 0.0),
            novelty=float(r[7] or 0.0),
            evidence_count=int(r[8] or 0),
            distinct_source_count=int(r[9] or 0),
            source_kinds=str(r[10] or ""),
            reason=str(r[11] or ""),
            created_at=str(r[12] or ""),
        )

    def list(
        self,
        *,
        limit: int = 200,
        subject: str | None = None,
        event_type: str | None = None,
        before_id: int | None = None,
        concept_id: int | None = None,
    ) -> list[ConceptEvent]:
        """Return timeline events newest-first.

        ``before_id`` pages backwards through history (pass the smallest
        ``event_id`` from the previous page to fetch the next older
        batch), so the UI can "scroll through the years" without loading
        everything at once. ``subject`` / ``event_type`` / ``concept_id``
        narrow the feed.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        where: list[str] = []
        params: list[object] = []
        if subject:
            where.append("subject = ?")
            params.append(str(subject))
        if event_type:
            where.append("event_type = ?")
            params.append(str(event_type))
        if concept_id is not None:
            where.append("concept_id = ?")
            params.append(int(concept_id))
        if before_id is not None:
            where.append("id < ?")
            params.append(int(before_id))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, int(limit)))
        try:
            rows = conn.execute(
                f"SELECT {_EVENT_COLS} FROM concept_events "
                f"{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("concept event list failed", exc_info=True)
            return []
        return [self._row_to_event(r) for r in rows]

    def trajectory(
        self, concept_id: int, *, limit: int = 500
    ) -> list[ConceptEvent]:
        """Return one concept's events **oldest-first** -- how it moved.

        The L17 self-drift work reads a concept as a path rather than a
        current value: confidence and label at each recorded moment, in
        the order they happened. Ordering is the inverse of :meth:`list`
        (which is a reverse-chronological feed) because a trajectory is
        read forwards, and ``limit`` keeps the *oldest* rows so the start
        of the story survives on a long-lived concept.

        Cheap: ``idx_concept_events_concept`` covers the filter.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                f"SELECT {_EVENT_COLS} FROM concept_events "
                "WHERE concept_id = ? ORDER BY created_at ASC, id ASC "
                "LIMIT ?",
                (int(concept_id), max(1, int(limit))),
            ).fetchall()
        except Exception:
            log.warning("concept trajectory read failed", exc_info=True)
            return []
        return [self._row_to_event(r) for r in rows]

    def latest_confidence(
        self, concept_ids: Sequence[int]
    ) -> dict[int, float]:
        """Map ``concept_id -> confidence`` at each concept's newest event.

        This is the watermark the L3 sampler compares against to decide
        whether a concept has drifted far enough since it was last on the
        timeline to be worth recording. Done as **one** grouped query for
        the whole lifecycle batch rather than a read per concept, since
        the sweep already touches up to `concept_lifecycle_batch_size`
        rows a tick.
        """
        ids = [int(c) for c in concept_ids if c is not None]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT e.concept_id, e.confidence FROM concept_events e "
                "JOIN (SELECT concept_id, MAX(id) AS mid FROM concept_events "
                f"      WHERE concept_id IN ({placeholders}) "
                "       GROUP BY concept_id) m ON e.id = m.mid",
                tuple(ids),
            ).fetchall()
        except Exception:
            log.warning("concept latest-confidence read failed", exc_info=True)
            return {}
        return {int(r[0]): float(r[1] or 0.0) for r in rows}

    def count(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM concept_events"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def counts_by_type(self) -> dict[str, int]:
        """Whole-timeline tally of ``event_type -> count``.

        The L22 flow metrics (promotion rate, demotions, reinforcement
        volume) are ratios over the full history, so they need a grouped
        count rather than a page of rows. Aggregated in SQL because the
        timeline is unbounded by design -- paging it into Python just to
        count would grow linearly forever.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT event_type, COUNT(*) FROM concept_events "
                "GROUP BY event_type"
            ).fetchall()
        except Exception:
            log.warning("concept event counts failed", exc_info=True)
            return {}
        return {str(r[0] or ""): int(r[1] or 0) for r in rows}


__all__ = ["ConceptEvent", "ConceptEventStore"]

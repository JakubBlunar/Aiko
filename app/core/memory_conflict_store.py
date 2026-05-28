"""Conflicting-memory pair store (F5 personality backlog).

The ``memory_conflicts`` table (schema v11) records pairs of memories
the F5 :class:`MemoryConflictWorker` has flagged as contradictory.
Each row pins exactly one ordered pair (``memory_a_id`` <
``memory_b_id``); the worker enforces the sort on insert so the
``UNIQUE(memory_a_id, memory_b_id)`` constraint dedupes naturally.

Lifecycle::

    detected -> status='open'
       |
       +- worker auto-resolved (delta >= auto_resolve_delta)
       |   -> status='auto_resolved', winner_id/loser_id/resolution_action set
       |
       +- user resolved via Conflicts sub-tab
       |   -> status='user_resolved', winner_id/loser_id/resolution_action set
       |
       +- user dismissed ("not actually a conflict")
           -> status='dismissed'

The ``flagged_by`` column is ``'auto'`` for worker-found pairs and
``'aiko'`` when Aiko emitted a ``[[conflict:reason]]`` self-tag (the
v1 implementation force-runs the worker on her flag rather than
trying to attribute the reason to specific memory ids -- the column
is reserved for the future where we can do better).

This module talks to SQLite directly (mirroring
:class:`app.core.affect_state.AffectStore`) rather than going through
``MemoryStore`` because the rows are pair-pointers, not memories.
``delete_for_memory`` is the cascade hook ``MemoryStore.delete``
should call so dropping one half of a pair doesn't leave a dangling
row pointing at a missing id.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase


log = logging.getLogger("app.memory_conflict_store")


# Public status values. Worker only writes ``open`` and ``auto_resolved``;
# the REST layer flips the row to ``user_resolved`` / ``dismissed``.
STATUS_OPEN = "open"
STATUS_AUTO_RESOLVED = "auto_resolved"
STATUS_USER_RESOLVED = "user_resolved"
STATUS_DISMISSED = "dismissed"

VALID_STATUSES: frozenset[str] = frozenset(
    (STATUS_OPEN, STATUS_AUTO_RESOLVED, STATUS_USER_RESOLVED, STATUS_DISMISSED)
)

# Resolution actions the REST layer / worker can record.
ACTION_DEMOTE = "demote"
ACTION_DELETE = "delete"
ACTION_DISMISS = "dismiss"

VALID_ACTIONS: frozenset[str] = frozenset((ACTION_DEMOTE, ACTION_DELETE, ACTION_DISMISS))

# Heuristic labels emitted by ``app.core.conflict_heuristics.classify_pair``.
# Re-exported here so callers that only import the store still see them.
from app.core.conflict_heuristics import (  # noqa: E402
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    HEURISTIC_NO,
)

# Origin of the conflict flag.
FLAGGED_BY_AUTO = "auto"
FLAGGED_BY_AIKO = "aiko"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_signals(signals: list[str] | None) -> str | None:
    if not signals:
        return None
    try:
        return json.dumps(list(signals), ensure_ascii=False)
    except Exception:
        return None


def _decode_signals(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


@dataclass(slots=True)
class ConflictPair:
    """One row of the ``memory_conflicts`` table.

    The two memory ids are *not* normalised back into ``a``/``b``
    semantic roles -- they're just the ids that won the
    ``min`` / ``max`` lottery on insert. ``winner_id`` / ``loser_id``
    carry the real "who won the resolution" meaning when a row is
    resolved.
    """

    id: int
    memory_a_id: int
    memory_b_id: int
    similarity: float
    confidence_delta: float
    heuristic_label: str
    heuristic_signals: list[str]
    llm_verdict: str | None
    llm_reason: str | None
    status: str
    winner_id: int | None
    loser_id: int | None
    resolution_action: str | None
    flagged_by: str
    detected_at: str
    resolved_at: str | None


def _row_to_pair(row: tuple[Any, ...]) -> ConflictPair:
    return ConflictPair(
        id=int(row[0]),
        memory_a_id=int(row[1]),
        memory_b_id=int(row[2]),
        similarity=float(row[3]),
        confidence_delta=float(row[4]),
        heuristic_label=str(row[5]),
        heuristic_signals=_decode_signals(row[6] if isinstance(row[6], str) else None),
        llm_verdict=(str(row[7]) if row[7] is not None else None),
        llm_reason=(str(row[8]) if row[8] is not None else None),
        status=str(row[9]),
        winner_id=(int(row[10]) if row[10] is not None else None),
        loser_id=(int(row[11]) if row[11] is not None else None),
        resolution_action=(str(row[12]) if row[12] is not None else None),
        flagged_by=str(row[13]),
        detected_at=str(row[14]),
        resolved_at=(str(row[15]) if row[15] is not None else None),
    )


_SELECT_COLS = (
    "id, memory_a_id, memory_b_id, similarity, confidence_delta, "
    "heuristic_label, heuristic_signals, llm_verdict, llm_reason, "
    "status, winner_id, loser_id, resolution_action, flagged_by, "
    "detected_at, resolved_at"
)


class MemoryConflictStore:
    """SQLite-backed store for the ``memory_conflicts`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        memory_a_id: int,
        memory_b_id: int,
        similarity: float,
        confidence_delta: float,
        heuristic_label: str,
        heuristic_signals: list[str] | None = None,
        llm_verdict: str | None = None,
        llm_reason: str | None = None,
        status: str = STATUS_OPEN,
        winner_id: int | None = None,
        loser_id: int | None = None,
        resolution_action: str | None = None,
        flagged_by: str = FLAGGED_BY_AUTO,
        detected_at: str | None = None,
    ) -> int | None:
        """Insert a conflict pair, normalising id order. Returns the row id.

        If the same pair is already recorded (any status), the call is
        an idempotent no-op and returns the existing id. The worker
        relies on this to skip pairs it has already classified.
        """
        a, b = sorted((int(memory_a_id), int(memory_b_id)))
        if a == b:
            log.debug("memory_conflicts.record skip self-pair id=%s", a)
            return None
        status_normalized = str(status or STATUS_OPEN).strip().lower()
        if status_normalized not in VALID_STATUSES:
            raise ValueError(
                f"invalid status {status!r} (valid: {sorted(VALID_STATUSES)})"
            )
        flagged = str(flagged_by or FLAGGED_BY_AUTO).strip().lower()
        if flagged not in (FLAGGED_BY_AUTO, FLAGGED_BY_AIKO):
            flagged = FLAGGED_BY_AUTO
        when = detected_at or _now_iso()
        signals_text = _encode_signals(heuristic_signals)

        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "INSERT INTO memory_conflicts ("
            "  memory_a_id, memory_b_id, similarity, confidence_delta,"
            "  heuristic_label, heuristic_signals, llm_verdict, llm_reason,"
            "  status, winner_id, loser_id, resolution_action,"
            "  flagged_by, detected_at, resolved_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(memory_a_id, memory_b_id) DO NOTHING",
            (
                a,
                b,
                float(similarity),
                float(confidence_delta),
                str(heuristic_label),
                signals_text,
                llm_verdict,
                llm_reason,
                status_normalized,
                int(winner_id) if winner_id is not None else None,
                int(loser_id) if loser_id is not None else None,
                resolution_action,
                flagged,
                when,
            ),
        )
        conn.commit()
        if cursor.rowcount and cursor.lastrowid:
            return int(cursor.lastrowid)
        # Already exists -- look up the id we collided with.
        row = conn.execute(
            "SELECT id FROM memory_conflicts WHERE memory_a_id = ? AND memory_b_id = ?",
            (a, b),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def mark_user_resolved(
        self,
        pair_id: int,
        *,
        winner_id: int,
        loser_id: int,
        action: str,
    ) -> bool:
        """Flip a pair to ``user_resolved`` after the UI applied a fix."""
        if str(action).strip().lower() not in (ACTION_DEMOTE, ACTION_DELETE):
            raise ValueError(
                f"invalid resolution action {action!r} "
                "(valid: 'demote' | 'delete')"
            )
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE memory_conflicts SET "
            "  status = ?, winner_id = ?, loser_id = ?, "
            "  resolution_action = ?, resolved_at = ? "
            "WHERE id = ?",
            (
                STATUS_USER_RESOLVED,
                int(winner_id),
                int(loser_id),
                str(action).strip().lower(),
                _now_iso(),
                int(pair_id),
            ),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def mark_auto_resolved(
        self,
        pair_id: int,
        *,
        winner_id: int,
        loser_id: int,
    ) -> bool:
        """Flip a pair to ``auto_resolved`` (worker-driven). Always demote."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE memory_conflicts SET "
            "  status = ?, winner_id = ?, loser_id = ?, "
            "  resolution_action = ?, resolved_at = ? "
            "WHERE id = ?",
            (
                STATUS_AUTO_RESOLVED,
                int(winner_id),
                int(loser_id),
                ACTION_DEMOTE,
                _now_iso(),
                int(pair_id),
            ),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def dismiss(self, pair_id: int) -> bool:
        """Mark a pair as ``dismissed`` (user said it's not actually a conflict)."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE memory_conflicts SET "
            "  status = ?, resolution_action = ?, resolved_at = ? "
            "WHERE id = ?",
            (STATUS_DISMISSED, ACTION_DISMISS, _now_iso(), int(pair_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def delete_for_memory(self, memory_id: int) -> int:
        """Drop every pair referencing ``memory_id`` (cascade-on-delete hook)."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "DELETE FROM memory_conflicts "
            "WHERE memory_a_id = ? OR memory_b_id = ?",
            (int(memory_id), int(memory_id)),
        )
        conn.commit()
        return int(cursor.rowcount or 0)

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, pair_id: int) -> ConflictPair | None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM memory_conflicts WHERE id = ?",
            (int(pair_id),),
        ).fetchone()
        return _row_to_pair(row) if row is not None else None

    def has_pair(self, memory_a_id: int, memory_b_id: int) -> bool:
        """Cheap check used by the worker to skip pairs it already saw."""
        a, b = sorted((int(memory_a_id), int(memory_b_id)))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT 1 FROM memory_conflicts "
            "WHERE memory_a_id = ? AND memory_b_id = ? LIMIT 1",
            (a, b),
        ).fetchone()
        return row is not None

    def list_open(self, *, limit: int = 50, offset: int = 0) -> list[ConflictPair]:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM memory_conflicts "
            "WHERE status = ? "
            "ORDER BY detected_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (STATUS_OPEN, int(limit), int(offset)),
        ).fetchall()
        return [_row_to_pair(r) for r in rows]

    def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> list[ConflictPair]:
        """List pairs newest-first. Optional status filter."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if status is None:
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM memory_conflicts "
                "ORDER BY detected_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
        else:
            status_normalized = str(status).strip().lower()
            if status_normalized not in VALID_STATUSES:
                raise ValueError(
                    f"invalid status filter {status!r} "
                    f"(valid: {sorted(VALID_STATUSES)})"
                )
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM memory_conflicts "
                "WHERE status = ? "
                "ORDER BY detected_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (status_normalized, int(limit), int(offset)),
            ).fetchall()
        return [_row_to_pair(r) for r in rows]

    def list_recently_auto_resolved(self, *, limit: int = 10) -> list[ConflictPair]:
        return self.list_recent(limit=limit, status=STATUS_AUTO_RESOLVED)

    def count_open(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_conflicts WHERE status = ?",
            (STATUS_OPEN,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def count_by_status(self) -> dict[str, int]:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM memory_conflicts GROUP BY status"
        ).fetchall()
        out = {s: 0 for s in VALID_STATUSES}
        for status, count in rows:
            out[str(status)] = int(count)
        return out

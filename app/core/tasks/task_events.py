"""Append-only per-task event log (schema v17).

Sibling of :class:`TaskStore`. The hot ``tasks.state`` blob carries the
handler's *current* decision context (a few hundred bytes — current
phase, objective, minimal scratchpad). Long-form audit ("I tried
URL X then URL Y, found N results, am now on step 3") lives here so
the hot blob stays small and the trail stays paginable + replayable.

The orchestrator appends to this log on every emit (one row per
``TaskProgress`` / ``TaskInputNeeded`` / ``TaskCompleted`` /
``TaskFailed`` / cancel / interrupt / heartbeat-stalled moment).
Handlers can also append custom rows via the ``TaskEvent`` outcome
for handler-specific audit (e.g. ``event("visited_url",
{"url": "https://..."})`` from a future browser handler).

Schema:

* ``id INTEGER PK`` — autoincrement.
* ``task_id INTEGER`` — FK-by-convention to ``tasks(id)``. No SQL FK
  so the cleanup worker can prune in any order without surprising
  cascades; the orchestrator's :meth:`delete_for_task` is the one
  authoritative cleanup path.
* ``type TEXT`` — free-text label. Stable constants for orchestrator
  events live in this module; handler-emitted events should use
  :data:`EVENT_CUSTOM` (the data dict can carry a sub-type).
* ``data TEXT`` — optional jsonable blob (NULL when the type carries
  no payload, e.g. ``EVENT_HEARTBEAT_STALLED``).
* ``created_at TEXT`` — ISO timestamp, UTC.

Threading: same shape as :class:`TaskStore` — borrowed per-thread
connection from :class:`ChatDatabase`, every method opens/writes/
commits in one fast pass, no long-lived transaction.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.task_events")


# ── stable event types ───────────────────────────────────────────────
#
# Orchestrator-owned event types. Handlers should use
# :data:`EVENT_CUSTOM` (the ``data`` dict carries a free-text
# ``"subtype"`` key) so an unknown type at read time always maps to
# "handler-defined" without a schema migration.

EVENT_STARTED = "started"
EVENT_PHASE_CHANGE = "phase_change"
EVENT_PROGRESS = "progress"
EVENT_INPUT_QUESTION = "input_question"
EVENT_INPUT_ANSWER = "input_answer"
EVENT_HEARTBEAT_STALLED = "heartbeat_stalled"
EVENT_CHILD_SPAWNED = "child_spawned"
EVENT_COMPLETED = "completed"
EVENT_FAILED = "failed"
EVENT_CANCELLED = "cancelled"
EVENT_INTERRUPTED = "interrupted"
EVENT_CUSTOM = "custom"

# Every type the orchestrator + protocol may produce. New types MUST
# be added here so :func:`is_known_event_type` returns the right
# answer for log-line audits.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    (
        EVENT_STARTED,
        EVENT_PHASE_CHANGE,
        EVENT_PROGRESS,
        EVENT_INPUT_QUESTION,
        EVENT_INPUT_ANSWER,
        EVENT_HEARTBEAT_STALLED,
        EVENT_CHILD_SPAWNED,
        EVENT_COMPLETED,
        EVENT_FAILED,
        EVENT_CANCELLED,
        EVENT_INTERRUPTED,
        EVENT_CUSTOM,
    )
)


def is_known_event_type(value: str) -> bool:
    """Return True iff ``value`` is a built-in event type.

    Handler-defined types pass through to ``EVENT_CUSTOM`` so this
    predicate is rarely False in practice — only relevant for the
    MCP debug listing.
    """
    return str(value) in KNOWN_EVENT_TYPES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_data(payload: Any) -> str | None:
    """Encode the ``data`` JSON column. None / empty -> NULL."""
    if payload is None:
        return None
    if isinstance(payload, (dict, list)) and not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        log.warning(
            "task_events encode failed: type=%s", type(payload).__name__
        )
        return None


def _decode_data(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except Exception:
        return None
    if isinstance(decoded, dict):
        return decoded
    return {"value": decoded}


# ── public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """One row of the ``task_events`` table.

    Frozen for cheap defensive equality + safe sharing across threads.
    ``data`` is the decoded JSON dict (or None when the column is
    NULL); producers / consumers agree on the shape per ``type``.
    """

    id: int
    task_id: int
    type: str
    data: dict[str, Any] | None
    created_at: str


def _row_to_event(row: tuple[Any, ...]) -> TaskEvent:
    return TaskEvent(
        id=int(row[0]),
        task_id=int(row[1]),
        type=str(row[2]),
        data=_decode_data(row[3]),
        created_at=str(row[4]),
    )


# ── store ────────────────────────────────────────────────────────────


class TaskEventStore:
    """SQLite facade for the ``task_events`` table.

    Cheap to instantiate (one ``_db`` reference). Mirrors the
    threading model of :class:`TaskStore` — borrowed per-thread
    connection via ``ChatDatabase._get_conn``.
    """

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def append(
        self,
        task_id: int,
        *,
        type: str,
        data: Any | None = None,
    ) -> int:
        """Append one event row. Returns the new ``id``.

        ``type`` should usually be one of the module-level constants;
        unknown values are accepted (handlers may roll their own
        sub-types via :data:`EVENT_CUSTOM` payload, or future code
        may add types ahead of this audit list). ``data`` is jsonable
        or ``None``; encoding failures are logged + collapsed to NULL
        rather than raised — append is best-effort by contract.
        """
        if int(task_id) <= 0:
            return 0
        type_norm = str(type or "").strip()
        if not type_norm:
            return 0
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "INSERT INTO task_events (task_id, type, data, created_at) "
            "VALUES (?, ?, ?, ?)",
            (int(task_id), type_norm, _encode_data(data), when),
        )
        conn.commit()
        event_id = int(cursor.lastrowid or 0)
        if event_id and not is_known_event_type(type_norm):
            log.debug(
                "task_event custom type appended: task=%d type=%s id=%d",
                int(task_id),
                type_norm,
                event_id,
            )
        return event_id

    def delete_for_task(self, task_id: int) -> int:
        """Remove every event for ``task_id``. Returns deleted count.

        Used by the cleanup worker when pruning terminal rows. The
        orchestrator never deletes individual events.
        """
        if int(task_id) <= 0:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "DELETE FROM task_events WHERE task_id = ?",
            (int(task_id),),
        )
        conn.commit()
        return int(cursor.rowcount or 0)

    # ── reads ─────────────────────────────────────────────────────────

    def list_for_task(
        self,
        task_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
        ascending: bool = True,
    ) -> list[TaskEvent]:
        """Return events for ``task_id``, paginated.

        ``ascending=True`` (default) returns chronological order so
        replay / audit reads naturally; the REST surface uses this
        directly. Pass ``False`` for the most-recent-first newest
        view used by the MCP "show me the last N" path.
        Cap ``limit`` at 1000 defensively.
        """
        if int(task_id) <= 0:
            return []
        capped_limit = max(1, min(1000, int(limit)))
        capped_offset = max(0, int(offset))
        order = "ASC" if ascending else "DESC"
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT id, task_id, type, data, created_at "
            "FROM task_events WHERE task_id = ? "
            f"ORDER BY id {order} LIMIT ? OFFSET ?",
            (int(task_id), capped_limit, capped_offset),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def count_for_task(self, task_id: int) -> int:
        """Total event count for one task. Cheap; uses the
        ``(task_id, id)`` index."""
        if int(task_id) <= 0:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (int(task_id),),
        ).fetchone()
        return int(row[0]) if row else 0

    def latest_for_task(
        self, task_id: int, *, type: str | None = None
    ) -> TaskEvent | None:
        """Most recent event, optionally filtered by ``type``.

        Used by the MCP debug surface and the test harness. Returns
        ``None`` when no rows match.
        """
        if int(task_id) <= 0:
            return None
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if type is not None:
            row = conn.execute(
                "SELECT id, task_id, type, data, created_at "
                "FROM task_events WHERE task_id = ? AND type = ? "
                "ORDER BY id DESC LIMIT 1",
                (int(task_id), str(type)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, task_id, type, data, created_at "
                "FROM task_events WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (int(task_id),),
            ).fetchone()
        return _row_to_event(row) if row is not None else None


__all__ = [
    "TaskEvent",
    "TaskEventStore",
    "EVENT_STARTED",
    "EVENT_PHASE_CHANGE",
    "EVENT_PROGRESS",
    "EVENT_INPUT_QUESTION",
    "EVENT_INPUT_ANSWER",
    "EVENT_HEARTBEAT_STALLED",
    "EVENT_CHILD_SPAWNED",
    "EVENT_COMPLETED",
    "EVENT_FAILED",
    "EVENT_CANCELLED",
    "EVENT_INTERRUPTED",
    "EVENT_CUSTOM",
    "KNOWN_EVENT_TYPES",
    "is_known_event_type",
]

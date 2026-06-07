"""Per-task input/answer history (schema v17).

The legacy ``tasks.input_request TEXT`` column is a single denormalised
slot for "the question Aiko is currently asking the user about this
task". That works for one-shot disambiguation (`file_read`'s "which
root?") but breaks down for real agent workflows where the handler
needs to:

* Ask multiple clarifications across the lifetime of one task.
* Re-ask after a partial / invalid answer (retry, narrower follow-up).
* Audit the whole question/answer trail for later replay.

This module owns the per-task input history. The orchestrator writes
one row per ``TaskInputNeeded`` emit and updates it on ``answer()``;
older pending rows are ``superseded`` when a fresh question arrives,
and cancelled tasks get their pending row flipped to ``cancelled``
so the audit stays clean.

Schema:

* ``id INTEGER PK`` — autoincrement.
* ``task_id INTEGER`` — FK-by-convention to ``tasks(id)``.
* ``prompt TEXT`` — the human-readable question text.
* ``kind TEXT`` — optional UI hint (``"choice"`` / ``"free_text"`` /
  ``"confirm"``). Nullable.
* ``options TEXT`` — optional jsonable list (used when ``kind="choice"``).
  Nullable.
* ``status TEXT`` — one of :data:`STATUS_PENDING` /
  :data:`STATUS_ANSWERED` / :data:`STATUS_SUPERSEDED` /
  :data:`STATUS_CANCELLED`.
* ``response TEXT`` — the raw user answer (NULL until answered).
* ``created_at TEXT`` — ISO timestamp, UTC.
* ``answered_at TEXT`` — ISO timestamp populated by
  :meth:`TaskInputStore.answer` (also bumped by ``mark_superseded`` /
  ``mark_cancelled`` so the row has a "left the pending state at" stamp).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.task_inputs")


# ── status enum ──────────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_SUPERSEDED = "superseded"
STATUS_CANCELLED = "cancelled"

VALID_INPUT_STATUSES: frozenset[str] = frozenset(
    (STATUS_PENDING, STATUS_ANSWERED, STATUS_SUPERSEDED, STATUS_CANCELLED)
)

# Statuses that count as "the row is no longer awaiting an answer".
# Used by ``supersede_pending_for_task`` + the orchestrator to gate
# answer-replay attempts.
TERMINAL_INPUT_STATUSES: frozenset[str] = frozenset(
    (STATUS_ANSWERED, STATUS_SUPERSEDED, STATUS_CANCELLED)
)


# ── input kind hints ─────────────────────────────────────────────────
#
# Free-text labels for the UI. Not enforced in the DB; handlers pick
# whichever helps the frontend render the right control. The list
# below is the canonical recommended set.

KIND_FREE_TEXT = "free_text"
KIND_CHOICE = "choice"
KIND_CONFIRM = "confirm"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_options(options: Any | None) -> str | None:
    if options is None:
        return None
    if not isinstance(options, (list, tuple)) or not options:
        return None
    try:
        normalised = [str(o) for o in options if str(o).strip()]
        if not normalised:
            return None
        return json.dumps(normalised, ensure_ascii=False)
    except Exception:
        return None


def _decode_options(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    if not isinstance(value, list):
        return None
    return [str(v) for v in value]


# ── public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TaskInput:
    """One row of the ``task_inputs`` table.

    Frozen for the same defensive equality + cross-thread safety
    rationale as :class:`TaskEvent`.
    """

    id: int
    task_id: int
    prompt: str
    kind: str | None
    options: list[str] | None
    status: str
    response: str | None
    created_at: str
    answered_at: str | None


def _row_to_input(row: tuple[Any, ...]) -> TaskInput:
    return TaskInput(
        id=int(row[0]),
        task_id=int(row[1]),
        prompt=str(row[2]),
        kind=(str(row[3]) if row[3] is not None else None),
        options=_decode_options(row[4]),
        status=str(row[5]),
        response=(str(row[6]) if row[6] is not None else None),
        created_at=str(row[7]),
        answered_at=(str(row[8]) if row[8] is not None else None),
    )


# ── store ────────────────────────────────────────────────────────────


class TaskInputStore:
    """SQLite facade for the ``task_inputs`` table.

    Same threading model as :class:`TaskStore` /
    :class:`TaskEventStore`.
    """

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def create(
        self,
        task_id: int,
        *,
        prompt: str,
        kind: str | None = None,
        options: list[str] | None = None,
    ) -> int:
        """Insert one pending input row. Returns the new ``id``.

        ``kind`` and ``options`` are advisory; an empty prompt is
        rejected with a ValueError because a blank question never
        helps the user. The orchestrator is expected to call
        :meth:`supersede_pending_for_task` first so only one row is
        ever in ``pending`` for a given task.
        """
        if int(task_id) <= 0:
            raise ValueError("task_inputs.create: task_id must be positive")
        prompt_norm = str(prompt or "").strip()
        if not prompt_norm:
            raise ValueError("task_inputs.create: prompt must be non-empty")
        kind_norm = str(kind).strip() if kind is not None else None
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "INSERT INTO task_inputs ("
            "  task_id, prompt, kind, options, status, response, "
            "  created_at, answered_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(task_id),
                prompt_norm,
                kind_norm or None,
                _encode_options(options),
                STATUS_PENDING,
                None,
                when,
                None,
            ),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        log.info(
            "task_input created: task=%d input=%d prompt_len=%d kind=%s "
            "options=%d",
            int(task_id),
            new_id,
            len(prompt_norm),
            kind_norm or "-",
            len(options or []),
        )
        return new_id

    def answer(self, input_id: int, *, response: str) -> bool:
        """Resolve the pending input row with the user's answer.

        Only moves rows currently in :data:`STATUS_PENDING`; calling
        ``answer`` on a row that was already superseded / cancelled
        is a no-op (returns False) so the orchestrator can't race a
        late answer with a fresh question. Bumps ``answered_at``.
        """
        if int(input_id) <= 0:
            return False
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE task_inputs SET status = ?, response = ?, "
            "answered_at = ? WHERE id = ? AND status = ?",
            (
                STATUS_ANSWERED,
                str(response or ""),
                when,
                int(input_id),
                STATUS_PENDING,
            ),
        )
        conn.commit()
        rowcount = bool(cursor.rowcount)
        if rowcount:
            log.info(
                "task_input answered: input=%d response_len=%d",
                int(input_id),
                len(str(response or "")),
            )
        return rowcount

    def supersede_pending_for_task(self, task_id: int) -> int:
        """Mark every pending row for ``task_id`` as superseded.

        Called by the orchestrator immediately before creating a
        fresh pending row (when the handler emits a *new*
        :class:`TaskInputNeeded` without the user having answered
        the previous one yet). Returns the count of rows affected.
        Bumps ``answered_at`` so the row has a "left the pending
        state at" stamp.
        """
        if int(task_id) <= 0:
            return 0
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE task_inputs SET status = ?, answered_at = ? "
            "WHERE task_id = ? AND status = ?",
            (STATUS_SUPERSEDED, when, int(task_id), STATUS_PENDING),
        )
        conn.commit()
        count = int(cursor.rowcount or 0)
        if count:
            log.info(
                "task_input superseded: task=%d count=%d",
                int(task_id),
                count,
            )
        return count

    def cancel_pending_for_task(self, task_id: int) -> int:
        """Mark every pending row for ``task_id`` as cancelled.

        Called by the orchestrator on task cancel / interrupt /
        boot-recovery. Distinguishable from ``superseded`` so the
        audit trail makes the reason visible. Bumps ``answered_at``.
        """
        if int(task_id) <= 0:
            return 0
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE task_inputs SET status = ?, answered_at = ? "
            "WHERE task_id = ? AND status = ?",
            (STATUS_CANCELLED, when, int(task_id), STATUS_PENDING),
        )
        conn.commit()
        count = int(cursor.rowcount or 0)
        if count:
            log.info(
                "task_input cancelled: task=%d count=%d",
                int(task_id),
                count,
            )
        return count

    def delete_for_task(self, task_id: int) -> int:
        """Remove every input row for ``task_id``. Returns deleted count.

        Used by the cleanup worker when pruning terminal task rows.
        The orchestrator never deletes individual input rows.
        """
        if int(task_id) <= 0:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "DELETE FROM task_inputs WHERE task_id = ?",
            (int(task_id),),
        )
        conn.commit()
        return int(cursor.rowcount or 0)

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, input_id: int) -> TaskInput | None:
        """Fetch one input row by id. Returns ``None`` when missing."""
        if int(input_id) <= 0:
            return None
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT id, task_id, prompt, kind, options, status, "
            "response, created_at, answered_at "
            "FROM task_inputs WHERE id = ?",
            (int(input_id),),
        ).fetchone()
        return _row_to_input(row) if row is not None else None

    def latest_pending(self, task_id: int) -> TaskInput | None:
        """Most recent pending row for ``task_id``.

        Returns ``None`` when no pending row exists. Used by
        :meth:`TaskOrchestrator.answer` to route the user's answer to
        the right input row.
        """
        if int(task_id) <= 0:
            return None
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT id, task_id, prompt, kind, options, status, "
            "response, created_at, answered_at "
            "FROM task_inputs WHERE task_id = ? AND status = ? "
            "ORDER BY id DESC LIMIT 1",
            (int(task_id), STATUS_PENDING),
        ).fetchone()
        return _row_to_input(row) if row is not None else None

    def list_for_task(
        self,
        task_id: int,
        *,
        ascending: bool = True,
        limit: int = 100,
    ) -> list[TaskInput]:
        """Full input history for one task, chronological by default.

        Cap ``limit`` at 500. The history is intentionally not
        paginated (the volume per task is expected to be small —
        a handful of clarifications, not thousands).
        """
        if int(task_id) <= 0:
            return []
        capped_limit = max(1, min(500, int(limit)))
        order = "ASC" if ascending else "DESC"
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT id, task_id, prompt, kind, options, status, "
            "response, created_at, answered_at "
            "FROM task_inputs WHERE task_id = ? "
            f"ORDER BY id {order} LIMIT ?",
            (int(task_id), capped_limit),
        ).fetchall()
        return [_row_to_input(r) for r in rows]


__all__ = [
    "TaskInput",
    "TaskInputStore",
    "STATUS_PENDING",
    "STATUS_ANSWERED",
    "STATUS_SUPERSEDED",
    "STATUS_CANCELLED",
    "VALID_INPUT_STATUSES",
    "TERMINAL_INPUT_STATUSES",
    "KIND_FREE_TEXT",
    "KIND_CHOICE",
    "KIND_CONFIRM",
]

"""SQLite-backed CRUD facade for the schema-v16 ``tasks`` table.

The store is owned by :class:`TaskOrchestrator`. Every row mutation
goes through one of the typed methods here so the SQL is in exactly
one place and the JSON encoding for ``args`` / ``state`` /
``input_request`` / ``result`` / ``metadata`` is always consistent.

Threading model: mirrors :class:`BeliefStore` and
:class:`MemoryConflictStore`. The store does not maintain its own
connection — it borrows the per-thread connection from
:class:`ChatDatabase` via the existing ``_get_conn`` helper. Each
public method opens, reads/writes, and commits in one
fast pass; there is no long-lived transaction across method calls.

Status validation: every transition method asserts the new status is
in :data:`VALID_STATUSES`. The terminal-status check belongs to the
orchestrator — the store will happily move a ``done`` row back to
``running`` if the orchestrator asks, but the orchestrator won't.

Logging contract — see ``docs/brain-orchestration.md`` *Logging*
section. Lifecycle lines fire at INFO; the structured field shape is
pinned by :mod:`tests.test_brain_log_fields`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from app.core.tasks.task_handler import (
    ACTIVE_STATUSES,
    INITIATED_BY_AIKO,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    VALID_INITIATED_BY,
    VALID_STATUSES,
)

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.task_store")


# Column ordering used by every SELECT * style fetch. Pinned as a
# constant so a typo at one site can't quietly desync TaskRow's field
# layout — tests assert this exact tuple.
_SELECT_COLS = (
    "id, user_id, handler_name, args, state, status, title, progress, "
    "last_message, input_request, result, error, notify_aiko, "
    "visible_to_user, initiated_by, created_at, updated_at, "
    "completed_at, metadata, phase, parent_task_id, heartbeat_at"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_json(payload: Any) -> str:
    """Encode an arbitrary jsonable value to a TEXT column.

    Always returns a non-NULL string so ``args`` / ``state``
    (declared NOT NULL) never raise on insert. Falls back to
    ``"{}"`` for unencodable input — surfaces in the row but doesn't
    crash the write path. The actual handler-side state shape is the
    handler's responsibility.
    """
    if payload is None:
        return "{}"
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        log.warning("task_store encode failed: type=%s", type(payload).__name__)
        return "{}"


def _encode_json_or_null(payload: Any) -> str | None:
    """Encode for *nullable* columns. Empty / None / unencodable -> NULL."""
    if payload is None:
        return None
    if isinstance(payload, (dict, list)) and not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return None


def _decode_json(raw: str | None, default: Any) -> Any:
    """Decode a JSON column. Returns ``default`` on NULL / parse error.

    Used for the args + state + input_request + result + metadata
    columns. The caller passes the expected shape (`{}` or `[]`) so
    the decoded value is always safe to subscript.
    """
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# ── public dataclass ─────────────────────────────────────────────────


@dataclass(slots=True)
class TaskRow:
    """One row of the ``tasks`` table.

    Snapshot semantics — the store builds a fresh :class:`TaskRow`
    for each read. Field defaults match the column DDL so a row built
    in-memory (e.g. by tests) round-trips correctly through
    :meth:`TaskStore.create`.

    The four JSON columns expose decoded Python values:

    * ``args`` — dict[str, Any] (default empty)
    * ``state`` — dict[str, Any] (handler-owned)
    * ``input_request`` — dict[str, Any] | None (None when not blocked)
    * ``result`` — dict[str, Any] | None (None until ``status='done'``)
    * ``metadata`` — dict[str, Any] | None (handler-extensibility blob)

    ``notify_aiko`` and ``visible_to_user`` are decoded to ``bool``
    even though they're stored as INTEGER 0/1. Use the constants in
    :mod:`task_handler` for status / initiated_by comparisons.
    """

    id: int
    user_id: str
    handler_name: str
    args: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_RUNNING
    title: str = ""
    progress: float | None = None
    last_message: str | None = None
    input_request: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    notify_aiko: bool = True
    visible_to_user: bool = True
    initiated_by: str = INITIATED_BY_AIKO
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] | None = None
    # ── schema v17 additions ─────────────────────────────────────────
    # ``phase`` is a free-text per-handler label promoted from
    # ``state["phase"]`` so every WS / prompt / cue site can read it
    # without parsing the state JSON. ``parent_task_id`` records the
    # task that spawned this one (single-parent tree, NOT a DAG).
    # ``heartbeat_at`` is an ISO timestamp bumped by the orchestrator
    # on every emit; the heartbeat sweep flags rows whose timestamp
    # is stale ("handler alive in-process but stuck").
    phase: str | None = None
    parent_task_id: int | None = None
    heartbeat_at: str | None = None


def _row_to_task(row: tuple[Any, ...]) -> TaskRow:
    return TaskRow(
        id=int(row[0]),
        user_id=str(row[1]),
        handler_name=str(row[2]),
        args=_decode_json(row[3], {}),
        state=_decode_json(row[4], {}),
        status=str(row[5]),
        title=str(row[6]),
        progress=(float(row[7]) if row[7] is not None else None),
        last_message=(str(row[8]) if row[8] is not None else None),
        input_request=_decode_json(row[9], None),
        result=_decode_json(row[10], None),
        error=(str(row[11]) if row[11] is not None else None),
        notify_aiko=bool(row[12]),
        visible_to_user=bool(row[13]),
        initiated_by=str(row[14]),
        created_at=str(row[15]),
        updated_at=str(row[16]),
        completed_at=(str(row[17]) if row[17] is not None else None),
        metadata=_decode_json(row[18], None),
        # v17 columns; legacy databases that pre-date the migration
        # may surface NULL for any of these — the decoder treats NULL
        # as "absent" rather than failing the read.
        phase=(str(row[19]) if row[19] is not None else None),
        parent_task_id=(int(row[20]) if row[20] is not None else None),
        heartbeat_at=(str(row[21]) if row[21] is not None else None),
    )


# ── store ────────────────────────────────────────────────────────────


class TaskStore:
    """SQLite facade for the ``tasks`` table.

    One instance per :class:`ChatDatabase`. Borrowed thread-local
    connections (no shared mutable state on the store itself except
    the ``_db`` reference), so the store is safe to share between
    the orchestrator's worker threads.
    """

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def create(
        self,
        *,
        user_id: str,
        handler_name: str,
        title: str,
        args: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        notify_aiko: bool = True,
        visible_to_user: bool = True,
        initiated_by: str = INITIATED_BY_AIKO,
        metadata: dict[str, Any] | None = None,
        parent_task_id: int | None = None,
        phase: str | None = None,
    ) -> int:
        """Insert a new row in ``status='running'`` and return the id.

        Validates ``initiated_by`` against
        :data:`VALID_INITIATED_BY`; an unknown value silently
        downgrades to ``INITIATED_BY_AIKO`` so a buggy caller can't
        crash the write path. ``user_id`` and ``handler_name`` must
        be non-empty — they're declared NOT NULL at the SQL layer
        and we reject empties early for a clean ValueError instead
        of an opaque sqlite IntegrityError.

        ``parent_task_id`` (schema v17) records the task that spawned
        this one. Single-parent tree, not a DAG; NULL = top-level.
        ``phase`` (schema v17) seeds the per-handler phase column;
        most callers leave it None and let the first ``TaskProgress``
        emit set it.

        ``heartbeat_at`` is auto-populated to ``created_at`` so the
        heartbeat sweep has a starting value to compare against.
        """
        user_id_norm = str(user_id or "").strip()
        if not user_id_norm:
            raise ValueError("task_store.create: user_id must be non-empty")
        handler_norm = str(handler_name or "").strip()
        if not handler_norm:
            raise ValueError("task_store.create: handler_name must be non-empty")
        initiated_norm = str(initiated_by or INITIATED_BY_AIKO).strip().lower()
        if initiated_norm not in VALID_INITIATED_BY:
            initiated_norm = INITIATED_BY_AIKO
        parent_norm = (
            int(parent_task_id)
            if parent_task_id is not None and int(parent_task_id) > 0
            else None
        )
        phase_norm = str(phase).strip() if phase is not None else None
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "INSERT INTO tasks ("
            "  user_id, handler_name, args, state, status, title, "
            "  progress, last_message, input_request, result, error, "
            "  notify_aiko, visible_to_user, initiated_by, "
            "  created_at, updated_at, completed_at, metadata, "
            "  phase, parent_task_id, heartbeat_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "         ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id_norm,
                handler_norm,
                _encode_json(args or {}),
                _encode_json(state or {}),
                STATUS_RUNNING,
                str(title or "").strip() or handler_norm,
                None,  # progress
                None,  # last_message
                None,  # input_request
                None,  # result
                None,  # error
                1 if notify_aiko else 0,
                1 if visible_to_user else 0,
                initiated_norm,
                when,
                when,
                None,  # completed_at
                _encode_json_or_null(metadata),
                phase_norm or None,
                parent_norm,
                when,  # heartbeat_at seeded to created_at
            ),
        )
        conn.commit()
        task_id = int(cursor.lastrowid or 0)
        log.info(
            "task created: task=%d user=%s handler=%s initiated_by=%s "
            "notify_aiko=%d visible_to_user=%d parent=%s",
            task_id,
            user_id_norm,
            handler_norm,
            initiated_norm,
            1 if notify_aiko else 0,
            1 if visible_to_user else 0,
            parent_norm if parent_norm is not None else "-",
        )
        return task_id

    def update_state(self, task_id: int, state: dict[str, Any]) -> bool:
        """Replace the handler-owned ``state`` blob.

        Used after every lifecycle call returns. ``updated_at`` is
        bumped on every successful update. Returns False if the row
        doesn't exist.
        """
        if task_id <= 0:
            return False
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
            (_encode_json(state), _now_iso(), int(task_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def update_progress(
        self,
        task_id: int,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> bool:
        """Patch the ``progress`` and/or ``last_message`` columns.

        Either / both may be ``None`` to clear. Used by the
        :class:`TaskProgress` outcome path. Does NOT change
        ``status`` — that's the other transition methods' job.
        """
        if task_id <= 0:
            return False
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        # Build a small dynamic SQL so a caller that only patches one
        # field doesn't accidentally clobber the other.
        sets: list[str] = []
        params: list[Any] = []
        if progress is not None:
            sets.append("progress = ?")
            params.append(float(progress))
        if message is not None:
            sets.append("last_message = ?")
            params.append(str(message))
        if not sets:
            # Caller passed neither — still bump updated_at as a
            # liveness signal (cheap and matches "the handler is
            # alive" semantics).
            sets.append("updated_at = ?")
            params.append(_now_iso())
        else:
            sets.append("updated_at = ?")
            params.append(_now_iso())
        params.append(int(task_id))
        cursor = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def update_phase(self, task_id: int, phase: str) -> bool:
        """Set the per-handler phase label (schema v17).

        ``phase`` is free-text — each handler documents its own
        phases. Empty / whitespace clears the column to NULL.
        Bumps ``updated_at``. Returns False on missing row.
        """
        if task_id <= 0:
            return False
        phase_norm = str(phase or "").strip() or None
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET phase = ?, updated_at = ? WHERE id = ?",
            (phase_norm, _now_iso(), int(task_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def update_heartbeat(self, task_id: int) -> bool:
        """Bump ``heartbeat_at`` + ``updated_at`` to ``now`` (schema v17).

        Called by the orchestrator on every emit so the heartbeat
        sweep can distinguish "alive" from "stalled". Returns False
        on missing row.
        """
        if task_id <= 0:
            return False
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (when, when, int(task_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def mark_awaiting_input(
        self,
        task_id: int,
        *,
        prompt: str,
        options: list[str] | None = None,
    ) -> bool:
        """Persist ``status='awaiting_input'`` + the input_request blob.

        ``options`` is a small list of pre-baked answers for the
        click-to-answer UI fallback path. Free-form text is always
        accepted; options are advisory. Empty / None ``options``
        stays NULL.
        """
        if task_id <= 0:
            return False
        input_request = {"prompt": str(prompt or "").strip()}
        if options:
            input_request["options"] = [str(o) for o in options if o]
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET status = ?, input_request = ?, updated_at = ? "
            "WHERE id = ?",
            (
                STATUS_AWAITING_INPUT,
                _encode_json(input_request),
                _now_iso(),
                int(task_id),
            ),
        )
        conn.commit()
        rowcount = bool(cursor.rowcount)
        if rowcount:
            log.info(
                "task awaiting input: task=%d prompt_len=%d options=%d",
                int(task_id),
                len(input_request["prompt"]),
                len(input_request.get("options") or []),
            )
        return rowcount

    def clear_awaiting_input(self, task_id: int) -> bool:
        """Move ``awaiting_input`` back to ``running`` after the
        user's answer arrives. NULLs the ``input_request`` column."""
        if task_id <= 0:
            return False
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET status = ?, input_request = NULL, updated_at = ? "
            "WHERE id = ?",
            (STATUS_RUNNING, _now_iso(), int(task_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def mark_done(self, task_id: int, *, result: dict[str, Any]) -> bool:
        """Persist a successful terminal transition.

        Sets ``status='done'`` + ``result`` JSON + ``completed_at``.
        ``progress`` is clamped to ``1.0`` only if the handler
        forgot to set it — we don't want a UI strip stuck at 90%
        after success.
        """
        if task_id <= 0:
            return False
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET status = ?, result = ?, error = NULL, "
            "progress = COALESCE(progress, 1.0), updated_at = ?, "
            "completed_at = ? WHERE id = ?",
            (STATUS_DONE, _encode_json(result), when, when, int(task_id)),
        )
        conn.commit()
        rowcount = bool(cursor.rowcount)
        if rowcount:
            log.info(
                "task done: task=%d result_size=%d",
                int(task_id),
                len(_encode_json(result)),
            )
        return rowcount

    def mark_failed(self, task_id: int, *, error: str) -> bool:
        """Persist a failure terminal transition.

        Sets ``status='failed'`` + ``error`` string +
        ``completed_at``. ``result`` is NULLed if it was set by a
        prior progress patch.
        """
        if task_id <= 0:
            return False
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "UPDATE tasks SET status = ?, error = ?, result = NULL, "
            "updated_at = ?, completed_at = ? WHERE id = ?",
            (
                STATUS_FAILED,
                str(error or "").strip() or "unspecified error",
                when,
                when,
                int(task_id),
            ),
        )
        conn.commit()
        rowcount = bool(cursor.rowcount)
        if rowcount:
            log.info("task failed: task=%d error=%s", int(task_id), error)
        return rowcount

    def mark_cancelled(self, task_id: int) -> bool:
        """Persist a user-initiated cancel.

        Only moves rows currently in :data:`ACTIVE_STATUSES`; calling
        cancel on an already-terminal row is a no-op (returns False)
        so the orchestrator doesn't accidentally race a completion.
        """
        if task_id <= 0:
            return False
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        active_list = tuple(ACTIVE_STATUSES)
        cursor = conn.execute(
            f"UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (STATUS_CANCELLED, when, when, int(task_id), *active_list),
        )
        conn.commit()
        rowcount = bool(cursor.rowcount)
        if rowcount:
            log.info("task cancelled: task=%d", int(task_id))
        return rowcount

    def mark_interrupted(self, task_id: int) -> bool:
        """Boot-recovery only: demote a ``running`` row to ``interrupted``.

        Same gate as :meth:`mark_cancelled` — only moves active
        rows. Called by
        :func:`app.core.tasks.recovery.recover_interrupted_tasks` on
        startup. The orchestrator parks a "the X task stopped, want
        me to retry?" cue for Aiko's next turn.
        """
        if task_id <= 0:
            return False
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        active_list = tuple(ACTIVE_STATUSES)
        cursor = conn.execute(
            f"UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (STATUS_INTERRUPTED, when, when, int(task_id), *active_list),
        )
        conn.commit()
        return bool(cursor.rowcount)

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, task_id: int) -> TaskRow | None:
        """Fetch one row by id, or ``None`` if missing."""
        if task_id <= 0:
            return None
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def list_running(
        self,
        user_id: str | None = None,
        *,
        statuses: Iterable[str] = ACTIVE_STATUSES,
    ) -> list[TaskRow]:
        """Return active tasks, newest first.

        Defaults to :data:`ACTIVE_STATUSES` (running + awaiting_input
        + paused) so the running-tasks inner-life provider sees every
        live row. ``user_id`` filter is optional; ``None`` returns
        active tasks for *every* user (used by MCP debug + boot
        recovery).
        """
        status_list = [s for s in statuses if s in VALID_STATUSES]
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        params: list[Any] = list(status_list)
        where = f"status IN ({placeholders})"
        if user_id is not None and str(user_id).strip():
            where = f"user_id = ? AND " + where
            params.insert(0, str(user_id).strip())
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks WHERE {where} "
            f"ORDER BY id DESC",
            tuple(params),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_for_user(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        visible_only: bool = True,
        roots_only: bool = False,
    ) -> list[TaskRow]:
        """Paginated history for the REST + UI surface.

        ``status`` may be a single status string or ``None`` for all
        statuses. Newest-first. Cap ``limit`` at 200 defensively so a
        misbehaving client can't pull the whole table in one shot.
        ``visible_only=True`` (default, chunk 13) filters out
        ``visible_to_user=0`` rows so the REST endpoint never leaks
        system-internal tasks to the frontend. ``roots_only=True``
        restricts to top-level tasks (``parent_task_id IS NULL``) so
        the Tasks tab can render parents only and fetch children
        on-demand via :meth:`list_children`.
        """
        user_norm = str(user_id or "").strip()
        if not user_norm:
            return []
        capped_limit = max(1, min(200, int(limit)))
        capped_offset = max(0, int(offset))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        clauses = ["user_id = ?"]
        params: list[Any] = [user_norm]
        if status is not None and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip())
        if visible_only:
            clauses.append("visible_to_user = 1")
        if roots_only:
            clauses.append("parent_task_id IS NULL")
        where = " AND ".join(clauses)
        params.extend([capped_limit, capped_offset])
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks WHERE {where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def count_for_user(
        self,
        user_id: str,
        *,
        status: str | None = None,
        visible_only: bool = True,
        roots_only: bool = False,
    ) -> int:
        """Total task count for a user, mirroring :meth:`list_for_user`'s filters.

        Used by the chunk-13 REST surface so paginated responses can
        carry a ``total`` field next to the page of items. ``status``
        accepts a single status string or ``None`` for all statuses.
        ``visible_only=True`` (default) filters out
        ``visible_to_user=0`` rows so the REST + UI never expose
        system-internal task bookkeeping. ``roots_only=True`` mirrors
        :meth:`list_for_user` and counts only top-level tasks
        (``parent_task_id IS NULL``) so the Tasks tab pager stays
        consistent with a parents-only list.
        """
        user_norm = str(user_id or "").strip()
        if not user_norm:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        clauses = ["user_id = ?"]
        params: list[Any] = [user_norm]
        if status is not None and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip())
        if visible_only:
            clauses.append("visible_to_user = 1")
        if roots_only:
            clauses.append("parent_task_id IS NULL")
        where = " AND ".join(clauses)
        row = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE {where}",
            tuple(params),
        ).fetchone()
        return int(row[0]) if row else 0

    def count_active_for_user(self, user_id: str) -> int:
        """Active task count for the per-user cap check in
        :class:`TaskOrchestrator`. Always cheap — index-only."""
        user_norm = str(user_id or "").strip()
        if not user_norm:
            return 0
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE user_id = ? "
            f"AND status IN ({placeholders})",
            (user_norm, *ACTIVE_STATUSES),
        ).fetchone()
        return int(row[0]) if row else 0

    def list_non_terminal(self) -> list[TaskRow]:
        """All rows currently in :data:`ACTIVE_STATUSES`, across users.

        Boot-recovery helper. Used by
        :func:`app.core.tasks.recovery.recover_interrupted_tasks` to
        demote stranded rows to ``interrupted``.
        """
        return self.list_running(user_id=None)

    def list_stalled(
        self, stalled_seconds: int, *, now_iso: str | None = None
    ) -> list[TaskRow]:
        """Rows whose ``heartbeat_at`` is older than the threshold.

        Filters to ``status='running'`` only — awaiting_input and
        paused rows are *expected* to have stale heartbeats (the
        handler isn't currently doing work). Returns the rows in
        oldest-heartbeat-first order so the sweep handles the worst
        cases first. ``now_iso`` is overridable for the test harness;
        production passes None to read the wall clock.

        Cheap because ``idx_tasks_heartbeat`` covers
        ``(status, heartbeat_at)``.
        """
        seconds_norm = max(1, int(stalled_seconds))
        if now_iso is None:
            cutoff = (
                datetime.now(timezone.utc).timestamp() - float(seconds_norm)
            )
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        else:
            # Parse the provided wall-clock anchor (test-only path).
            try:
                anchor = datetime.fromisoformat(str(now_iso))
            except Exception:
                anchor = datetime.now(timezone.utc)
            cutoff = anchor.timestamp() - float(seconds_norm)
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks "
            "WHERE status = ? AND heartbeat_at IS NOT NULL "
            "AND heartbeat_at < ? "
            "ORDER BY heartbeat_at ASC",
            (STATUS_RUNNING, cutoff_iso),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_children(
        self,
        parent_task_id: int,
        *,
        statuses: Iterable[str] | None = None,
    ) -> list[TaskRow]:
        """Child tasks of ``parent_task_id``.

        ``statuses`` filters the result set; when ``None`` returns
        every child regardless of status (used by the audit /
        introspection paths). When set, only children whose status
        is in the iterable are returned (used by cascade-cancel,
        which passes :data:`ACTIVE_STATUSES`).
        """
        if int(parent_task_id) <= 0:
            return []
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if statuses is None:
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM tasks "
                "WHERE parent_task_id = ? ORDER BY id ASC",
                (int(parent_task_id),),
            ).fetchall()
            return [_row_to_task(r) for r in rows]
        status_list = [s for s in statuses if s in VALID_STATUSES]
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks "
            f"WHERE parent_task_id = ? AND status IN ({placeholders}) "
            "ORDER BY id ASC",
            (int(parent_task_id), *status_list),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_terminal_older_than(
        self,
        cutoff_iso: str,
        *,
        limit: int = 500,
    ) -> list[TaskRow]:
        """Terminal rows whose ``completed_at`` is older than ``cutoff_iso``.

        Used by :class:`TaskCleanupWorker` to find prunable rows.
        Cap ``limit`` at 5000 defensively so a long-deferred cleanup
        doesn't pull a giant set in one transaction. Newest-first so
        the worker can process in any order.
        """
        cutoff = str(cutoff_iso or "").strip()
        if not cutoff:
            return []
        capped_limit = max(1, min(5000, int(limit)))
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        terminal_list = tuple(TERMINAL_STATUSES)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks "
            f"WHERE status IN ({placeholders}) "
            "AND completed_at IS NOT NULL "
            "AND completed_at < ? "
            "ORDER BY id ASC LIMIT ?",
            (*terminal_list, cutoff, capped_limit),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def delete(self, task_id: int) -> bool:
        """Hard delete a row. Only used by tests + MCP cleanup.

        The orchestrator never deletes — terminal rows are kept for
        history. Returns False on missing id.
        """
        if task_id <= 0:
            return False
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
        conn.commit()
        return bool(cursor.rowcount)


__all__ = ["TaskRow", "TaskStore"]

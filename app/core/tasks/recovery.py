"""Boot-time task recovery for the brain-orchestration layer.

A clean shutdown sets every task to a terminal status before the
process exits. A *crash* (SIGKILL, power loss, unhandled exception
in the supervisor) leaves whatever was running stranded —
``tasks.status='running'`` with a connection that's now dead. This
module scans non-terminal rows on next boot and:

* ``running`` → demoted to ``interrupted``; the orchestrator emits a
  :class:`TaskResultEvent` cue so Aiko sees "the file search I
  started earlier stopped when we last talked — want me to retry?"
  on her next turn.
* ``awaiting_input`` → kept as-is; the pending question is still
  valid (the user's answer didn't get lost, just hasn't been given
  yet).
* ``paused`` → kept as-is; reserved for phase-2 explicit resume.

The recovery pass does **not** auto-resume — that's a sharper
footgun than asking once. Explicit user intent ("yeah, retry it")
spawns a fresh task with the recovered row's ``args``; the old row
stays in ``interrupted`` for audit.

Logging contract — one INFO line per recovered row plus one summary
INFO line. The line shape is pinned by
:mod:`tests.test_brain_log_fields` so MCP grep targets stay stable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.tasks.task_handler import (
    STATUS_AWAITING_INPUT,
    STATUS_PAUSED,
    STATUS_RUNNING,
)

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.tasks.task_orchestrator import TaskOrchestrator
    from app.core.tasks.task_store import TaskRow, TaskStore


log = logging.getLogger("app.task_orchestrator")


@dataclass(slots=True)
class RecoveryReport:
    """Summary returned by :func:`recover_interrupted_tasks`.

    ``interrupted`` — rows that were ``running`` and got demoted.
    ``preserved`` — rows in ``awaiting_input`` / ``paused`` that
                    survived as-is.
    ``failed`` — rows the store refused to update (DB error, race).

    All three are lists of task ids. Used by MCP debug + the boot
    log line so a developer can see at a glance whether the
    recovery hook did anything.
    """

    interrupted: list[int]
    preserved: list[int]
    failed: list[int]

    @property
    def total_scanned(self) -> int:
        return len(self.interrupted) + len(self.preserved) + len(self.failed)


def recover_interrupted_tasks(
    store: "TaskStore",
    *,
    orchestrator: "TaskOrchestrator | None" = None,
    resume_on_boot: bool = True,
) -> RecoveryReport:
    """Walk every non-terminal row and apply the recovery policy.

    ``store`` is required; ``orchestrator`` is optional — when wired
    the orchestrator emits a :class:`TaskResultEvent` cue per
    demoted row so Aiko surfaces a retry prompt on her next turn.
    ``resume_on_boot`` (the ``agent.tasks_resume_on_boot`` setting)
    gates the cue emission only — the SQL demotion always runs so
    the DB never has stranded ``running`` rows after a successful
    boot.

    Returns a :class:`RecoveryReport` with the per-bucket task ids.
    Safe to call multiple times: a row already in a terminal status
    (or in ``awaiting_input`` / ``paused``) is left alone.
    """
    interrupted: list[int] = []
    preserved: list[int] = []
    failed: list[int] = []

    rows = store.list_non_terminal()
    if not rows:
        log.info("task recovery: scanned=0 interrupted=0 preserved=0")
        return RecoveryReport(interrupted, preserved, failed)

    for row in rows:
        try:
            if row.status == STATUS_RUNNING:
                ok = store.mark_interrupted(row.id)
                if not ok:
                    failed.append(row.id)
                    continue
                interrupted.append(row.id)
                log.info(
                    "task recovered on boot: task=%d was_status=%s "
                    "now_status=interrupted",
                    row.id,
                    STATUS_RUNNING,
                )
                if orchestrator is not None and resume_on_boot:
                    try:
                        orchestrator.register_recovered(
                            task_id=row.id,
                            user_id=row.user_id,
                            title=row.title,
                            notify_aiko=row.notify_aiko,
                            visible_to_user=row.visible_to_user,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        log.exception(
                            "task recovery: orchestrator hook failed "
                            "task=%d exc=%r",
                            row.id,
                            exc,
                        )
            elif row.status in (STATUS_AWAITING_INPUT, STATUS_PAUSED):
                preserved.append(row.id)
                log.debug(
                    "task recovery preserved: task=%d status=%s",
                    row.id,
                    row.status,
                )
            else:
                # Defensive: shouldn't happen because
                # ``list_non_terminal`` filters to active statuses,
                # but log anything weird that slips through.
                log.warning(
                    "task recovery: unexpected status task=%d status=%s",
                    row.id,
                    row.status,
                )
                preserved.append(row.id)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception(
                "task recovery row failed: task=%d exc=%r", row.id, exc
            )
            failed.append(row.id)

    log.info(
        "task recovery: scanned=%d interrupted=%d preserved=%d failed=%d "
        "resume_on_boot=%d",
        len(rows),
        len(interrupted),
        len(preserved),
        len(failed),
        1 if resume_on_boot else 0,
    )
    return RecoveryReport(interrupted, preserved, failed)


__all__ = ["RecoveryReport", "recover_interrupted_tasks"]

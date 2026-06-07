"""Correlation IDs for log records: turn id and task id.

Two :class:`contextvars.ContextVar` instances carry per-thread
correlation through every log call:

* **turn id** — set by :class:`app.core.session.turn_runner.TurnRunner`
  for the duration of one conversation turn. Greppable as
  ``turn=abc12345``.
* **task id** — set by
  :class:`app.core.tasks.task_orchestrator.TaskOrchestrator` for the
  duration of a single handler invocation (start / resume / on_input /
  cancel). Greppable as ``task=def67890``. A line can carry both ids
  simultaneously — the typical case is a mid-turn ``start_*`` tool call
  that spawns a task whose first emit lands while ``turn`` is still
  active.

The :class:`_TurnIdFilter` in :mod:`app.core.infra.crash_logging`
reads both variables and stamps them onto every record so a single
grep yields the full trace of one turn or one task across modules and
threads.

Usage::

    from app.core.infra.log_context import (
        set_turn_id, reset_turn_id,
        set_task_id, reset_task_id,
    )

    turn_token = set_turn_id("abc12345")
    try:
        # ... run a turn ...
        task_token = set_task_id("def67890")
        try:
            # ... run a task handler ...
        finally:
            reset_task_id(task_token)
    finally:
        reset_turn_id(turn_token)

Background workers that fan out from the turn (LLM streaming, TTS
playback, speaking-window scheduled jobs, task handler emits) inherit
both ids automatically because ``contextvars`` are copied into newly-
spawned tasks/threads via ``contextvars.copy_context``. Plain
``threading.Thread`` does **not** copy the context, so workers that
explicitly want correlation should either propagate the id or capture
it via :func:`get_turn_id` / :func:`get_task_id` and pass it along.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = [
    "set_turn_id",
    "reset_turn_id",
    "get_turn_id",
    "set_task_id",
    "reset_task_id",
    "get_task_id",
]


_turn_id: ContextVar[str | None] = ContextVar("aiko_turn_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("aiko_task_id", default=None)


def set_turn_id(value: str | None) -> Token[str | None]:
    """Set the active turn id and return the reset token."""
    return _turn_id.set(value)


def reset_turn_id(token: Token[str | None]) -> None:
    """Restore the prior turn id (or absence thereof).

    Best-effort: if the token has already been consumed (for example
    because the caller reset twice during error handling) we clear the
    contextvar manually rather than letting a stray ``RuntimeError``
    bubble out of a ``finally`` block. ``ContextVar.reset`` raises
    ``RuntimeError`` for already-used tokens and ``ValueError`` /
    ``LookupError`` for cross-context tokens, so we swallow all three.
    """
    try:
        _turn_id.reset(token)
    except (RuntimeError, ValueError, LookupError):
        _turn_id.set(None)


def get_turn_id() -> str | None:
    """Return the active turn id, or ``None`` if no turn is in flight."""
    return _turn_id.get()


def set_task_id(value: str | None) -> Token[str | None]:
    """Set the active task id and return the reset token.

    Allocated by :class:`TaskOrchestrator` when a handler starts
    running, propagates through every per-handler ``emit`` callback and
    downstream log line via ``contextvars.copy_context`` so the
    correlation survives across threads spawned inside the handler.
    """
    return _task_id.set(value)


def reset_task_id(token: Token[str | None]) -> None:
    """Restore the prior task id (or absence thereof).

    Same swallow-and-clear semantics as :func:`reset_turn_id` — a stray
    reset in error handling must not bubble out of a ``finally``.
    """
    try:
        _task_id.reset(token)
    except (RuntimeError, ValueError, LookupError):
        _task_id.set(None)


def get_task_id() -> str | None:
    """Return the active task id, or ``None`` if no task handler is running."""
    return _task_id.get()

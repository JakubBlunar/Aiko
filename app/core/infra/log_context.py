"""Per-turn correlation IDs for log records.

A `contextvars.ContextVar` carries the active turn id through every log
call inside a single :class:`app.core.session.turn_runner.TurnRunner` invocation.
The :class:`_TurnIdFilter` in :mod:`app.core.infra.crash_logging` reads this
variable and stamps it onto every record so a single grep (``turn=abc12345``)
yields the full trace of one conversation turn across modules and threads.

Usage::

    from app.core.infra.log_context import set_turn_id, reset_turn_id

    token = set_turn_id("abc12345")
    try:
        # ... run a turn ...
    finally:
        reset_turn_id(token)

Background workers that fan out from the turn (LLM streaming, TTS
playback, speaking-window scheduled jobs) inherit the id automatically
because ``contextvars`` are copied into newly-spawned tasks/threads via
``contextvars.copy_context``. Plain ``threading.Thread`` does **not**
copy the context, so workers that explicitly want correlation should
either propagate the id or capture it via :func:`get_turn_id` and pass
it along.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = ["set_turn_id", "reset_turn_id", "get_turn_id"]


_turn_id: ContextVar[str | None] = ContextVar("aiko_turn_id", default=None)


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

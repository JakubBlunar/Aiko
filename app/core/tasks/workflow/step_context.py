"""Logical step identity follows a skill's existing child-spawn adapter."""
from contextvars import ContextVar

current_step: ContextVar[tuple[int, str, int] | None] = ContextVar("workflow_step", default=None)

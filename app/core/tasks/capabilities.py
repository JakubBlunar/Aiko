"""Task capability descriptors — the reusable hook for approvals + config.

A *capability* is a named category of thing a task handler can do
(``file_write``, and later ``shell_exec`` / ``http_post`` / ``send_email``).
It carries just enough metadata for the cross-cutting approval layer to
decide whether an action needs the user's sign-off:

* ``id`` — stable identifier, also the key in the approval-override map
  (``agent.task_approval_overrides``) and the session approve-all set.
* ``label`` — a short human phrase used in the approval prompt
  (``"I'd like to {label}: ..."``).
* ``destructive`` — whether actions under this capability can be
  irreversible / side-effecting enough to warrant an approval gate.
  A non-destructive capability never asks (the gate is a no-op for it).

This module is pure: a frozen dataclass plus a tiny process-wide
registry. Handlers register their capability at import time so the MCP
debug surface (`get_approvals_state`) and the settings layer can
enumerate what exists without importing every handler. Adding a new
destructive task is then: declare a capability here, reference its id
from the handler, and reuse :mod:`app.core.tasks.approval` for the
gate. No approval-specific code per handler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass


log = logging.getLogger("app.tasks.capabilities")


# ── canonical capability ids ─────────────────────────────────────────

CAPABILITY_FILE_WRITE = "file_write"


@dataclass(frozen=True, slots=True)
class TaskCapability:
    """One capability a task handler can exercise.

    Frozen so a registered descriptor can't be mutated out from under
    the approval layer.
    """

    id: str
    label: str
    destructive: bool = False


# Process-wide registry. Small, append-only, last-write-wins (so a
# re-import during a test reload cleanly replaces the slot).
_REGISTRY: dict[str, TaskCapability] = {}


def register_capability(capability: TaskCapability) -> None:
    """Register (or overwrite) a capability by id."""
    cap_id = str(getattr(capability, "id", "") or "").strip()
    if not cap_id:
        raise ValueError("capability must have a non-empty 'id'")
    _REGISTRY[cap_id] = capability
    log.debug(
        "capability registered: id=%s destructive=%s total=%d",
        cap_id,
        capability.destructive,
        len(_REGISTRY),
    )


def get_capability(capability_id: str) -> TaskCapability | None:
    """Look up a registered capability, or ``None`` when unknown."""
    return _REGISTRY.get(str(capability_id))


def all_capabilities() -> list[TaskCapability]:
    """Every registered capability, sorted by id for stable output."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY.keys())]


# ── built-in capabilities ────────────────────────────────────────────

register_capability(
    TaskCapability(
        id=CAPABILITY_FILE_WRITE,
        label="write to a file",
        destructive=True,
    )
)


__all__ = [
    "CAPABILITY_FILE_WRITE",
    "TaskCapability",
    "register_capability",
    "get_capability",
    "all_capabilities",
]

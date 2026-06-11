"""Reusable approval gate for destructive task capabilities.

Pure helpers shared by every handler that can perform a destructive
action (file write today; shell exec / http post / send email later):

* :func:`resolve_approval` — given a capability id and the live policy
  (global mode + per-capability overrides + the session "approve all"
  set), decide ``"auto"`` (proceed silently) or ``"ask"`` (gate).
* :func:`build_request` — construct the standard
  :class:`TaskInputNeeded` an ``ask`` gate emits, with the canonical
  ``[approve / approve all / deny]`` options.
* :func:`parse_decision` — interpret the user's answer (a clicked
  option string OR free-text) into one of the three decisions.

The handler integration pattern (documented in
``docs/task-approvals.md``):

1. In ``start``: build the action, then ``resolve_approval(...)``.
   ``"auto"`` -> perform now. ``"ask"`` -> ``emit(build_request(...))``
   and return an ``awaiting_approval`` state stashing the pending action.
2. In ``on_input``: ``parse_decision(answer)``. ``APPROVE`` /
   ``APPROVE_ALL`` -> perform; ``APPROVE_ALL`` also flips the session
   approve-all flag via the host callback. ``DENY`` -> finish without
   acting.

This module imports only :class:`TaskInputNeeded` from the task-handler
contract — no orchestrator, no settings, no I/O. The "approve all"
scope is *session* (cleared on restart); the persistent default lives
in ``agent.task_approval_mode`` + ``agent.task_approval_overrides``.
"""
from __future__ import annotations

from typing import Iterable

from app.core.tasks.capabilities import TaskCapability
from app.core.tasks.task_handler import TaskInputNeeded


# ── decision constants (also the option-button labels) ───────────────

APPROVE = "approve"
APPROVE_ALL = "approve all"
DENY = "deny"

# Sentinel that, when present in the session approve-all set, approves
# every capability for the rest of the session.
APPROVE_ALL_SCOPE = "all"

# ── approval-mode constants (persistent policy) ──────────────────────

MODE_ASK = "ask"
MODE_AUTO = "auto"
VALID_MODES = frozenset((MODE_ASK, MODE_AUTO))


def normalize_mode(value: str | None, *, default: str = MODE_ASK) -> str:
    """Coerce a raw mode string to a valid mode (fallback ``ask``)."""
    s = str(value or "").strip().lower()
    return s if s in VALID_MODES else default


def resolve_approval(
    capability_id: str,
    *,
    mode: str = MODE_ASK,
    overrides: dict[str, str] | None = None,
    session_approved: Iterable[str] | None = None,
) -> str:
    """Resolve the effective approval mode for ``capability_id``.

    Precedence (first match wins):

    1. Session approve-all: if the session set contains the
       :data:`APPROVE_ALL_SCOPE` sentinel OR this capability id, the
       user already said "stop asking" this session -> ``"auto"``.
    2. Per-capability override from ``agent.task_approval_overrides``.
    3. The global ``agent.task_approval_mode`` default.

    Returns :data:`MODE_AUTO` (proceed) or :data:`MODE_ASK` (gate).
    """
    cap_id = str(capability_id)
    approved = set(session_approved or ())
    if APPROVE_ALL_SCOPE in approved or cap_id in approved:
        return MODE_AUTO
    eff = mode
    if overrides:
        override = overrides.get(cap_id)
        if override is not None:
            eff = override
    return normalize_mode(eff)


def build_request(
    capability: TaskCapability, action_summary: str
) -> TaskInputNeeded:
    """Build the canonical approval :class:`TaskInputNeeded`.

    The prompt reads as Aiko asking permission in the first person so
    the TaskStrip chip + (later) any spoken surface stay in-character.
    Options are the three fixed decision strings; the TaskStrip renders
    them as click buttons and free-text still routes through
    :func:`parse_decision`.
    """
    summary = (action_summary or "").strip()
    label = (capability.label or "do that").strip()
    if summary:
        prompt = f"I'd like to {label}: {summary}. Is that okay?"
    else:
        prompt = f"I'd like to {label}. Is that okay?"
    return TaskInputNeeded(prompt=prompt, options=[APPROVE, APPROVE_ALL, DENY])


# Free-text markers, checked after exact-option matching. Order in
# ``parse_decision`` matters: deny markers win over a stray "ok" so
# "no, don't" reads as DENY; approve-all is detected before approve.
_DENY_MARKERS = (
    "deny", "denied", "cancel", "stop", "nope", "nah",
    "don't", "do not", "dont", "never mind", "nevermind", "no thanks",
)
_ALL_MARKERS = ("all", "always", "everything", "every time", "stop asking")
_APPROVE_MARKERS = (
    "approve", "approved", "yes", "yep", "yeah", "ok", "okay", "sure",
    "go ahead", "do it", "allow", "confirm", "confirmed", "proceed",
    "fine", "please do",
)


def parse_decision(answer: str) -> str:
    """Map a raw answer to :data:`APPROVE` / :data:`APPROVE_ALL` / :data:`DENY`.

    Exact option strings win first (the TaskStrip buttons send these
    verbatim). Otherwise a small free-text heuristic runs; ambiguous /
    empty input is treated as :data:`DENY` (fail safe — never perform a
    destructive action on an unclear answer).
    """
    s = (answer or "").strip().lower()
    if not s:
        return DENY
    # Exact option matches (the click path).
    if s == APPROVE_ALL:
        return APPROVE_ALL
    if s == APPROVE:
        return APPROVE
    if s == DENY:
        return DENY
    # Free-text. A bare "no" (no "approve" anywhere) is a deny.
    if "approve" not in s and (
        s in ("no", "n") or any(m in s for m in _DENY_MARKERS)
    ):
        return DENY
    # "approve all", "yes to all", "always", "stop asking" -> approve all.
    looks_approve = any(m in s for m in _APPROVE_MARKERS)
    if looks_approve and any(m in s for m in _ALL_MARKERS):
        return APPROVE_ALL
    if any(m in s for m in _ALL_MARKERS) and "approve" in s:
        return APPROVE_ALL
    if looks_approve:
        return APPROVE
    return DENY


__all__ = [
    "APPROVE",
    "APPROVE_ALL",
    "DENY",
    "APPROVE_ALL_SCOPE",
    "MODE_ASK",
    "MODE_AUTO",
    "VALID_MODES",
    "normalize_mode",
    "resolve_approval",
    "build_request",
    "parse_decision",
]

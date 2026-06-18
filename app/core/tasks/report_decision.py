"""Worker-model decision: should a finished task interrupt the user?

Backlog C6. When a background task reaches a terminal state, the
structural rule used to be binary: ``notify_aiko=True`` parked a cue +
armed escalation, everything else stayed silent. There was no graded
"is this worth interrupting for *right now*?" judgement, and the task's
provenance (did the user ask for this, or did Aiko start it herself?)
never entered the decision.

This module is that judgement. :func:`decide_task_report` runs a small,
**stripped** worker-LLM pass — persona-lite plus the result facts plus a
handful of live context signals, deliberately *without* the world /
ambient / affect blocks the chat prompt carries — and returns a
:class:`ReportVerdict`:

* ``action`` — ``surface_now`` (proactively break the silence),
  ``park_for_natural_opening`` (fold into the next natural turn, no
  proactive interrupt), or ``drop`` (not worth mentioning).
* ``angle`` — a short, first-person hint for *how* Aiko could bring it
  up. It is NOT a verbatim line: it rides the most-volatile T6 task-cue
  block so the chat model composes the actual reply in Aiko's voice with
  full context. The angle never enters the cached stable prompt prefix.

Design contract:

* The call runs on the **worker** model (``worker_default`` route /
  ``_maintenance_client``), never the chat model, so it neither spends
  chat quota nor invalidates the main brain's prompt cache.
* Every failure path — no client, timeout, malformed JSON — falls back
  to the conservative :data:`ACTION_PARK` (never :data:`ACTION_SURFACE`),
  so a flaky or absent worker can never turn into a barrage of
  interruptions.

This module is pure (no SessionController import): the caller passes in
the client, model, and the context signals it has already gathered.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.task_report_decision")


ReportAction = Literal["surface_now", "park_for_natural_opening", "drop"]

ACTION_SURFACE: ReportAction = "surface_now"
ACTION_PARK: ReportAction = "park_for_natural_opening"
ACTION_DROP: ReportAction = "drop"

_VALID_ACTIONS: frozenset[str] = frozenset(
    (ACTION_SURFACE, ACTION_PARK, ACTION_DROP)
)

# Provenance labels passed to the worker so it can weigh "the user asked
# for this" vs "I started this myself" differently.
PROVENANCE_USER = "user_requested"
PROVENANCE_SELF = "self_initiated"

# Keep the drafted angle tight — it's a hint, not a script.
_ANGLE_MAX_CHARS = 160


@dataclass(frozen=True, slots=True)
class ReportVerdict:
    """The worker's call on a finished task.

    ``action`` is one of the three :data:`ACTION_*` enum values.
    ``angle`` is an optional, short first-person framing hint for the
    chat model (empty when the worker declined or failed). ``reason`` is
    a terse machine/debug string (e.g. ``"fallback"``, ``"llm"``,
    ``"disabled"``) — surfaced in logs + the MCP debug tool, never shown
    to the user.
    """

    action: ReportAction
    angle: str = ""
    reason: str = ""


# The conservative default used on every failure path. Park (not drop)
# so a result still has a chance to surface on the next natural turn,
# but never proactively interrupt off the back of a broken worker call.
_FALLBACK = ReportVerdict(action=ACTION_PARK, angle="", reason="fallback")


def fallback_verdict(reason: str = "fallback") -> ReportVerdict:
    """Return the conservative park verdict with a custom ``reason``."""
    return ReportVerdict(action=ACTION_PARK, angle="", reason=reason)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _build_messages(
    *,
    user_display_name: str,
    origin_prompt: str,
    title: str,
    summary: str,
    status: str,
    provenance: str,
    arc: str,
    idle_seconds: float | None,
    recent_assistant_gist: str,
) -> list[dict[str, Any]]:
    """Assemble the stripped worker prompt (no world/ambient/affect)."""
    name = (user_display_name or "the user").strip() or "the user"
    who = (
        f"{name} explicitly asked for this"
        if provenance == PROVENANCE_USER
        else "you started this yourself, unprompted"
    )
    idle_clause = ""
    if idle_seconds is not None and idle_seconds >= 0:
        mins = idle_seconds / 60.0
        if mins >= 1.0:
            idle_clause = (
                f" {name} last said something about "
                f"{int(round(mins))} min ago."
            )
        else:
            idle_clause = f" {name} was active moments ago."
    arc_clause = f" The current conversation feels like: {arc}." if arc else ""
    gist = _clip(recent_assistant_gist, 240)
    gist_clause = (
        f' Your last few messages were about: "{gist}".' if gist else ""
    )
    origin_clause = (
        f' The task came from this request: "{_clip(origin_prompt, 240)}".'
        if origin_prompt
        else ""
    )

    system = (
        "You are the quiet background judgement of Aiko, an AI companion. "
        "A background task just finished. Decide whether Aiko should "
        "proactively bring its result to "
        f"{name} right now, save it for a natural opening, or let it go. "
        "Interrupting for something trivial is worse than staying quiet. "
        "Reply with JSON ONLY: "
        '{"action": "surface_now" | "park_for_natural_opening" | "drop", '
        '"angle": "<one short first-person hint for how Aiko could bring '
        'it up, or empty>"}.'
    )
    user = (
        f"Task: {title or 'a background task'} (status: {status or 'done'}).\n"
        f"Result: {_clip(summary, 400) or '(no summary)'}.\n"
        f"Provenance: {who}.{origin_clause}{arc_clause}{idle_clause}"
        f"{gist_clause}\n"
        "Guidance: a result the user explicitly asked for usually deserves "
        "surfacing; something you started yourself should clear a higher "
        "bar. A supportive / heavy conversation raises the bar. The angle "
        "is a private hint, not a line to read out loud."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def decide_task_report(
    *,
    ollama: "ChatClient | None",
    model: str | None,
    title: str,
    summary: str,
    status: str = "done",
    provenance: str = PROVENANCE_USER,
    origin_prompt: str = "",
    user_display_name: str = "the user",
    arc: str = "",
    idle_seconds: float | None = None,
    recent_assistant_gist: str = "",
) -> ReportVerdict:
    """Score interrupt-worthiness for a finished task on the worker model.

    Returns a :class:`ReportVerdict`. Any failure (no client, transport
    error, timeout, malformed JSON, unknown action) collapses to the
    conservative :data:`ACTION_PARK` fallback — never
    :data:`ACTION_SURFACE`.
    """
    if ollama is None or not model:
        return fallback_verdict("no_worker_client")

    messages = _build_messages(
        user_display_name=user_display_name,
        origin_prompt=origin_prompt,
        title=title,
        summary=summary,
        status=status,
        provenance=provenance,
        arc=arc,
        idle_seconds=idle_seconds,
        recent_assistant_gist=recent_assistant_gist,
    )
    try:
        content, _usage = ollama.chat_json(
            messages,
            model=model,
            options={"temperature": 0.3, "num_predict": 120},
            format_json=True,
            surface="task_report_decision",
        )
    except Exception:
        log.debug("task-report decision LLM call failed", exc_info=True)
        return fallback_verdict("llm_error")

    try:
        blob = json.loads(content or "{}")
    except Exception:
        log.debug("task-report decision JSON parse failed", exc_info=True)
        return fallback_verdict("parse_error")
    if not isinstance(blob, dict):
        return fallback_verdict("parse_error")

    action = str(blob.get("action") or "").strip().lower()
    if action not in _VALID_ACTIONS:
        return fallback_verdict("bad_action")
    angle = _clip(str(blob.get("angle") or ""), _ANGLE_MAX_CHARS)
    return ReportVerdict(action=action, angle=angle, reason="llm")  # type: ignore[arg-type]


__all__ = [
    "ReportAction",
    "ReportVerdict",
    "ACTION_SURFACE",
    "ACTION_PARK",
    "ACTION_DROP",
    "PROVENANCE_USER",
    "PROVENANCE_SELF",
    "decide_task_report",
    "fallback_verdict",
]

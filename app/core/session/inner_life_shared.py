"""Shared constants + pure helpers for the inner-life provider mixins."""
from __future__ import annotations

import logging
from typing import Any

from app.core.affect import circadian as _circadian  # noqa: F401  (re-exported)

log = logging.getLogger("app.session")


# J8: friendly phrasings for each relationship milestone label (the labels
# come from ``app.core.relationship.relationship._MILESTONES``). ``{name}``
# is filled with the user's display name by ``_render_milestone_block``.
# An unknown label falls back to a humanised default in the renderer.
_MILESTONE_PHRASES: dict[str, str] = {
    "first_hundred_turns": "you and {name} have talked a hundred times now",
    "first_week_together": "it's been a week since you and {name} started talking",
    "first_month_together": "it's been a month since you and {name} first met",
    "hundred_days_together": "it's been a hundred days with {name}",
    "six_months_together": "it's been half a year with {name}",
    "first_year_together": "it's been a whole year with {name}",
}


# J10: shared-moment vibes that read as appreciation-worthy. Excludes
# ``comfort`` / ``vulnerable`` (hard-time moments don't suit a cheerful
# "thanks for that") and ``general`` (too vague to be specific).
_APPRECIATION_VIBES: frozenset[str] = frozenset({
    "warm", "playful", "tender", "proud", "silly",
    "milestone", "gift", "victory", "creative",
})
# kv_meta watermarks for the J10 appreciation cooldown + anti-repeat.
_KV_APPRECIATION_AT = "appreciation.last_surfaced_at"
_KV_APPRECIATION_ANCHOR = "appreciation.last_anchor_id"
# kv_meta watermark for the J9 reciprocal-vulnerability cooldown.
_KV_RECIP_VULN_AT = "reciprocal_vulnerability.last_surfaced_at"


# Brain-orchestration chunk 6: helper for the running-tasks block.
# Pulled out so the rendering rules can be tested in isolation
# (``tests/test_running_tasks_provider.py``) without spinning up a
# full :class:`SessionController`. Pure function — takes a TaskRow,
# returns the formatted bullet line.
def _format_running_task_line(row: Any) -> str:
    """Format one :class:`TaskRow` as a bullet for the running-tasks block.

    Shape: ``- {label} ({status}[, {N}%][, "{last_message}"])``

    * ``label`` is ``row.title`` when set, else ``row.handler_name``.
      Long titles are truncated to 40 chars (with an ellipsis) so a
      title bomb can't blow past the block budget.
    * ``status`` is the raw status string (``running`` /
      ``awaiting_input``). Stays lowercase — Aiko's persona block
      teaches her to read these casually, not formally.
    * ``N%`` only appears when ``row.progress`` is a finite float in
      ``[0, 1]``; clamped + rounded to whole percent.
    * ``last_message`` appears when set, truncated to 60 chars. The
      ``awaiting_input`` cue's question text lives in the parallel
      task-cues block, so we don't repeat it here — the
      ``last_message`` is the handler's progress narration ("scanning
      directory tree", etc.).
    """
    handler = str(getattr(row, "handler_name", "") or "task")
    title = str(getattr(row, "title", "") or "").strip()
    label = title or handler
    if len(label) > 40:
        label = label[:39].rstrip() + "…"
    status = str(getattr(row, "status", "") or "running")
    parts: list[str] = [status]
    progress = getattr(row, "progress", None)
    if progress is not None:
        try:
            p = float(progress)
        except (TypeError, ValueError):
            p = None  # type: ignore[assignment]
        else:
            if p < 0.0:
                p = 0.0
            elif p > 1.0:
                p = 1.0
            parts.append(f"{int(round(p * 100))}%")
    last = getattr(row, "last_message", None)
    if last:
        text = str(last).strip()
        if text:
            if len(text) > 60:
                text = text[:59].rstrip() + "…"
            parts.append(f'"{text}"')
    return f"- {label} ({', '.join(parts)})"


"""K-time3: pre-computed future relative times for the upcoming-horizon cue.

Future date arithmetic is exactly where an LLM companion fails — even with a
"now" anchor in the prompt, the model reasons its way to "in 3 days" /
"next Tuesday" and drifts. The fix is to never make her compute it: a cheap
**forward sweep** over ``future_plan`` memories whose ``event_time`` falls
inside a short horizon (default the next 7 days), rendered as one terse
"coming up" cue with the relative phrasing **already resolved** by the same
canonical :mod:`app.core.infra.timephrase` formatter the RAG path uses
(``(planned for tomorrow morning 09:00)``).

This module is the pure, side-effect-free core (deterministic against an
injected ``now`` so tests don't depend on the wall clock):

- :func:`select_upcoming` — filter + sort the candidate memories to the ones
  whose ``event_time`` lands in ``(now, now + horizon]``;
- :func:`build_signature` — a stable fingerprint of the selected set so the
  provider can tell "the same plans as last turn" from "a new plan appeared /
  one just passed" (drives the anti-nag watermark);
- :func:`render_block` — the prompt-ready heads-up line.

The forward sweep is the missing piece that ``rag_retriever`` /
``temporal_suffix`` don't cover: those only tag a future plan with its
resolved time *if semantic RAG happens to surface it*. K-time3 surfaces it by
**time**, not by relevance, so Aiko can bring it up unprompted — and never
miscounts the days getting there.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from app.core.infra import timephrase

# Hard cap on how long a single rendered plan line may get before it's
# trimmed on a word boundary — keeps the cue terse (a heads-up, not a
# calendar readout).
_MAX_PLAN_CHARS = 100


def _event_dt(mem: Any) -> datetime | None:
    return timephrase.parse_iso(getattr(mem, "event_time", None))


def select_upcoming(
    candidates: Sequence[Any],
    now: datetime,
    *,
    horizon_days: int,
    max_items: int,
) -> list[Any]:
    """Return the candidate memories due within ``(now, now + horizon_days]``.

    ``candidates`` are memory-like objects exposing ``event_time`` (ISO-8601)
    and ``content``. Rows with a missing / unparseable ``event_time``, or one
    that already passed (``<= now``) or sits beyond the horizon, are dropped.
    The survivors are sorted soonest-first and capped at ``max_items``.
    """
    now = timephrase.to_aware(now)
    horizon = now + timedelta(days=max(1, int(horizon_days)))
    dated: list[tuple[datetime, Any]] = []
    for mem in candidates:
        when = _event_dt(mem)
        if when is None:
            continue
        if when <= now or when > horizon:
            continue
        dated.append((when, mem))
    dated.sort(key=lambda pair: pair[0])
    cap = max(1, int(max_items))
    return [mem for _when, mem in dated[:cap]]


def build_signature(events: Sequence[Any]) -> str:
    """Stable fingerprint of the selected set (id + event_time per row).

    Used by the provider's anti-nag watermark: an unchanged signature means
    "same plans as last surface, don't re-nag"; a changed one means a new
    plan appeared or one slid out of the window, which is worth re-surfacing.
    """
    parts = [
        f"{getattr(m, 'id', '')}:{getattr(m, 'event_time', '') or ''}"
        for m in events
    ]
    return "|".join(parts)


def _clean_plan(content: str) -> str:
    text = (content or "").strip().rstrip(".!?").strip()
    if len(text) > _MAX_PLAN_CHARS:
        text = text[: _MAX_PLAN_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return text


def render_block(
    events: Sequence[Any],
    now: datetime,
    user_display_name: str,
) -> str:
    """Render the upcoming-horizon heads-up, or ``""`` when nothing renders.

    Each event line is ``- <plan> — <resolved relative time>`` where the
    relative phrase comes straight from
    :func:`app.core.infra.timephrase.humanize_future`, so the date is already
    worked out and the LLM never recomputes it. The framing tells Aiko this
    is a private heads-up, not a calendar she should recite.
    """
    now = timephrase.to_aware(now)
    name = (user_display_name or "").strip() or "them"
    lines: list[str] = []
    for mem in events:
        plan = _clean_plan(getattr(mem, "content", "") or "")
        if not plan:
            continue
        phrase = timephrase.humanize_future(getattr(mem, "event_time", None), now)
        lines.append(f"- {plan} — {phrase}")
    if not lines:
        return ""
    header = (
        f"Coming up for {name} (relative times already worked out — "
        "use these, don't recalculate the dates yourself):"
    )
    footer = (
        "Private heads-up only — bring one up if it fits the moment, never "
        "recite it like a calendar."
    )
    return header + "\n" + "\n".join(lines) + "\n" + footer


__all__ = ["select_upcoming", "build_signature", "render_block"]

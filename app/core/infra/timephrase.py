"""Canonical relative-time phrasing + the single "now" seam (K-time5/6/7).

Before this module, relative-time phrasing was reimplemented in ~6 places
(``rag_retriever._humanize_past/_future/_temporal_suffix``,
``reconnection.humanize_gap``, ``promise_lifecycle.humanize_age``, the
``prompt_assembler`` K-time1 history age prefix, ``follow_up._humanize_clock``,
``wants_ledger``) with slightly different bandings, so the *same instant*
could read "yesterday" on one surface and "1 day ago" on another in the same
turn. The parsing/normalisation plumbing (ISO parse, ``Z`` handling, naive →
UTC promotion) was copy-pasted too.

This module is the canonical home. It owns:

- the shared primitives (:func:`parse_iso`, :func:`to_aware`, :func:`now`);
- the two precise register formatters the chat RAG path already used
  (:func:`humanize_past`, :func:`humanize_future`) plus the memory bullet
  suffix (:func:`temporal_suffix`) — moved here verbatim so behaviour is
  byte-identical and ``rag_retriever`` simply re-exports them;
- the K-time1 history-message age phrase (:func:`age_prefix`);
- the "now" anchor sentence workers paste into a system prompt
  (:func:`today_anchor`); and
- the **worker toolkit** (K-time7): :func:`format_memory_line` /
  :func:`format_memory_block` / :func:`format_transcript` so background
  workers can feed the LLM memories + transcripts that actually carry their
  timestamps, instead of bare ``- {content}`` lines.

Registers are intentionally *not* flattened to one banding — a companion
saying "it's been about a week" (fuzzy) and a memory bullet saying
"(3 days ago)" (precise) are different on purpose. Callers pick the
formatter that matches their register; what's unified is the plumbing and
the source of "now".

The module-level :func:`now` is the single seam a future debug clock
(DT1 virtual clock / time-travel) plugs into: swap :data:`_now_provider`
and every relative phrase across the app moves together.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

HOUR_SECONDS: float = 3600.0
DAY_SECONDS: float = 86400.0


# ── the single "now" seam ────────────────────────────────────────────────
# Default: the real local wall clock (timezone-aware). A debug clock (DT1)
# can replace this provider so every relative phrase + age tag in the app
# moves together without touching call sites.
def _default_now() -> datetime:
    try:
        return datetime.now().astimezone()
    except Exception:  # pragma: no cover -- exotic platforms only
        return datetime.now(timezone.utc)


_now_provider: Callable[[], datetime] = _default_now


def now() -> datetime:
    """Return the current moment as a timezone-aware datetime.

    Routes through :data:`_now_provider` so a debug clock can override it
    process-wide. Always returns an aware datetime.
    """
    value = _now_provider()
    return to_aware(value)


def set_now_provider(provider: Callable[[], datetime] | None) -> None:
    """Override (or reset, with ``None``) the process-wide "now" source.

    Intended for the DT1 virtual clock and for deterministic tests. Pass
    ``None`` to restore the real wall clock.
    """
    global _now_provider
    _now_provider = provider if provider is not None else _default_now


# ── shared primitives ────────────────────────────────────────────────────
def to_aware(dt: datetime) -> datetime:
    """Promote a tz-naive datetime to UTC. No-op for aware values."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_iso(value: str | None) -> datetime | None:
    """ISO-8601 -> aware datetime, with ``Z`` and naive normalisation.

    Returns ``None`` for empty / non-string / unparseable input so callers
    can fall back to a safe phrase.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return to_aware(dt)


# ── precise past/future register (the chat RAG path's formatters) ─────────
def humanize_past(when_iso: str, now: datetime) -> str:
    """Render ``when_iso`` as a short past-tense phrase relative to ``now``.

    Precise register ("3 days ago", "2 weeks ago"). ``in the past`` is the
    safe fallback when parsing fails. Moved verbatim from
    ``rag_retriever._humanize_past`` (K-time5 consolidation).
    """
    when = parse_iso(when_iso)
    if when is None:
        return "in the past"
    now = to_aware(now)
    delta = (now - when).total_seconds()
    if delta < 0:
        return "moments ago"
    if delta < HOUR_SECONDS:
        minutes = max(1, int(delta // 60))
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if delta < DAY_SECONDS:
        hours = max(1, int(delta // HOUR_SECONDS))
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(delta // DAY_SECONDS)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    if days < 365:
        months = max(1, days // 30)
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = max(1, days // 365)
    return f"{years} year{'s' if years != 1 else ''} ago"


def humanize_future(when_iso: str | None, now: datetime) -> str:
    """Render ``when_iso`` as a short future-tense phrase relative to ``now``.

    Pod-aware ("tonight 20:00", "tomorrow morning 09:00", "on Friday 18:00",
    "next week", "in 2 weeks"). Falls back to ``"soon"`` when missing /
    unparseable. Moved verbatim from ``rag_retriever._humanize_future``.
    """
    if not when_iso:
        return "soon"
    when = parse_iso(when_iso)
    if when is None:
        return "soon"
    now = to_aware(now)
    delta = (when - now).total_seconds()
    if delta <= 0:
        return "earlier"
    when_local = when.astimezone()
    now_local = now.astimezone()
    same_day = when_local.date() == now_local.date()
    tomorrow = (when_local.date() - now_local.date()).days == 1
    clock = when_local.strftime("%H:%M")
    if same_day:
        hour = when_local.hour
        if hour < 5:
            return f"later tonight {clock}"
        if hour < 12:
            return f"this morning {clock}"
        if hour < 17:
            return f"this afternoon {clock}"
        if hour < 22:
            return f"tonight {clock}"
        return f"late tonight {clock}"
    if tomorrow:
        hour = when_local.hour
        if hour < 12:
            return f"tomorrow morning {clock}"
        if hour < 17:
            return f"tomorrow afternoon {clock}"
        if hour < 22:
            return f"tomorrow evening {clock}"
        return f"tomorrow night {clock}"
    days = int(delta // DAY_SECONDS)
    if days < 7:
        return f"on {when_local.strftime('%A')} {clock}"
    if days < 14:
        return "next week"
    if days < 30:
        weeks = days // 7
        return f"in {weeks} week{'s' if weeks != 1 else ''}"
    if days < 365:
        months = max(1, days // 30)
        return f"in {months} month{'s' if months != 1 else ''}"
    years = max(1, days // 365)
    return f"in {years} year{'s' if years != 1 else ''}"


def temporal_suffix(
    *,
    temporal_type: str | None,
    event_time: str | None,
    created_at: str | None,
    now: datetime,
) -> str:
    """Build the parenthetical time tag for a retrieved memory bullet.

    Returns ``""`` for ``durable`` / ``preference`` / unknown types
    (timeless memories render with no suffix). ``ongoing`` gets "(ongoing)".
    ``past_event`` / ``future_plan`` get humanised phrases sourced from
    ``event_time`` (``created_at`` fallback for past events). Moved verbatim
    from ``rag_retriever._temporal_suffix`` (K-time5 consolidation).
    """
    if not temporal_type:
        return ""
    t = temporal_type.lower()
    if t in ("durable", "preference"):
        return ""
    if t == "ongoing":
        return " (ongoing)"
    if t == "past_event":
        anchor = event_time or created_at
        if not anchor:
            return ""
        return f" ({humanize_past(anchor, now)})"
    if t == "future_plan":
        when = parse_iso(event_time)
        if when is None:
            return f" (planned for {humanize_future(event_time, now)})"
        if when <= to_aware(now):
            return (
                f" (was planned for {humanize_future(event_time, when - timedelta(seconds=1))}"
                " — should be done by now)"
            )
        return f" (planned for {humanize_future(event_time, now)})"
    return ""


# ── K-time1 history-message age phrase ───────────────────────────────────
def age_prefix(created_at_iso: str | None, now: datetime) -> str:
    """Clock-anchored relative age for a chat-history / transcript message.

    Register: explicit wall-clock once past the hour, so the model gets a
    "what time was that?" anchor:

    - < 60s              -> ``just now``
    - 1-59 min           -> ``N min ago``
    - same calendar day  -> ``today HH:MM``
    - previous day       -> ``yesterday HH:MM``
    - 2-6 days old        -> ``DayName HH:MM``
    - older              -> ``Mon DD HH:MM``

    Returns ``""`` when ``created_at_iso`` is missing / unparseable so the
    caller can skip the prefix. ``now`` should be timezone-aware. This is the
    canonical home of the K-time1 logic; ``PromptAssembler._format_age``
    delegates here.
    """
    when = parse_iso(created_at_iso)
    if when is None:
        return ""
    now = to_aware(now)
    delta = (now - when).total_seconds()
    if delta < 0:
        # Clock skew between writer and reader — read as "just now".
        return "just now"
    if delta < 60.0:
        return "just now"
    if delta < 3600.0:
        minutes = max(1, int(delta // 60))
        return f"{minutes} min ago"
    when_local = when.astimezone()
    now_local = now.astimezone()
    clock = when_local.strftime("%H:%M")
    day_delta = (now_local.date() - when_local.date()).days
    if day_delta <= 0:
        return f"today {clock}"
    if day_delta == 1:
        return f"yesterday {clock}"
    if day_delta < 7:
        return f"{when_local.strftime('%A')} {clock}"
    return f"{when_local.strftime('%b %d')} {clock}"


# ── the "now" anchor sentence for worker prompts (K-time7) ────────────────
def today_anchor(now_dt: datetime | None = None) -> str:
    """Return the "Today is ..." anchor sentence workers paste into a prompt.

    Format matches the line ``MemoryExtractor._build_system_prompt`` already
    hand-wrote, so any worker can drop it in to let its LLM resolve relative
    phrases ("yesterday", "next Monday") to absolute dates. Example::

        Today is Friday, June 26, 2026, 15:21 CEST (2026-06-26T15:21:00+02:00).

    ``now_dt`` defaults to the live :func:`now`.
    """
    when = to_aware(now_dt) if now_dt is not None else now()
    human = when.strftime("%A, %B %d, %Y, %H:%M %Z").strip()
    return f"Today is {human} ({when.isoformat()})."


# ── worker memory / transcript renderers (K-time7) ───────────────────────
def format_memory_line(mem: Any, now_dt: datetime | None = None) -> str:
    """Render one memory as ``- {content} (age)`` for a worker LLM prompt.

    Unlike the chat RAG block (which leaves durable/preference rows untagged
    because they're timeless to the *speaker*), this always appends a recency
    tag so a worker *reasoning over* the memory can see how fresh it is —
    that's the whole point of K-time9. Precedence: the v10 temporal suffix
    when meaningful (``ongoing`` / ``past_event`` / ``future_plan``),
    otherwise a ``created_at``-based "(N days ago)".
    """
    when = to_aware(now_dt) if now_dt is not None else now()
    content = str(getattr(mem, "content", "") or "").strip()
    created_at = getattr(mem, "created_at", None)
    suffix = temporal_suffix(
        temporal_type=getattr(mem, "temporal_type", None),
        event_time=getattr(mem, "event_time", None),
        created_at=created_at,
        now=when,
    )
    if not suffix and created_at:
        suffix = f" ({humanize_past(str(created_at), when)})"
    return f"- {content}{suffix}"


def format_memory_block(
    mems: Iterable[Any],
    now_dt: datetime | None = None,
    *,
    header: str | None = None,
    max_items: int | None = None,
) -> str:
    """Render a list of memories as an age-tagged bullet block (K-time9).

    Empty input yields ``""``. ``header`` is prepended on its own line when
    given; ``max_items`` caps the rendered rows (the rest are dropped).
    """
    when = to_aware(now_dt) if now_dt is not None else now()
    rows = list(mems)
    if max_items is not None and max_items >= 0:
        rows = rows[:max_items]
    lines = [format_memory_line(m, when) for m in rows]
    lines = [ln for ln in lines if ln.strip() and ln.strip() != "-"]
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"{header}\n{body}" if header else body


_DEFAULT_ROLE_LABELS: dict[str, str] = {
    "user": "User",
    "assistant": "Aiko",
    "aiko": "Aiko",
}


def _row_attr(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def format_transcript(
    rows: Sequence[Any],
    now_dt: datetime | None = None,
    *,
    role_labels: dict[str, str] | None = None,
    with_age: bool = True,
) -> str:
    """Render chat rows as ``[age] Speaker: text`` lines for a worker prompt.

    Each ``row`` may be a dict or object exposing ``role`` (or ``speaker``),
    ``content`` (or ``text``), and ``created_at``. The K-time1 :func:`age_prefix`
    is prepended in brackets when ``with_age`` and a timestamp is present, so
    a transcript-crunching worker (promise / belief / moments / summary) can
    resolve "tonight"/"yesterday" against real wall-clock anchors. Empty rows
    are skipped.
    """
    when = to_aware(now_dt) if now_dt is not None else now()
    labels = role_labels or _DEFAULT_ROLE_LABELS
    out: list[str] = []
    for row in rows:
        text = str(_row_attr(row, "content") or _row_attr(row, "text") or "").strip()
        if not text:
            continue
        role_raw = _row_attr(row, "role") or _row_attr(row, "speaker") or ""
        speaker = labels.get(str(role_raw).strip().lower(), str(role_raw) or "?")
        prefix = ""
        if with_age:
            age = age_prefix(_row_attr(row, "created_at"), when)
            if age:
                prefix = f"[{age}] "
        out.append(f"{prefix}{speaker}: {text}")
    return "\n".join(out)

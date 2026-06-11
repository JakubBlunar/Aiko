"""Promise lifecycle helpers (K43 personality backlog).

Promise memories (``kind="promise"``) gain a small state machine carried
on the existing v7 ``metadata`` JSON column — no schema change:

    open ──> surfaced ──> fulfilled
      │          │
      └──────────┴──────> dropped

* ``open`` — extracted, nothing has happened yet. Legacy rows with no
  ``promise_status`` key read as ``open``.
* ``surfaced`` — the :class:`PromiseFollowthroughWorker` armed a
  follow-through cue for it ("you said you'd check X — close the loop").
* ``fulfilled`` — Aiko's reply (or a finished background task) actually
  delivered on it. Terminal.
* ``dropped`` — it aged out without resolution (default 14 days) and we
  stopped owing it. Terminal.

Sidedness rides ``metadata.promise_who`` (stamped by
:class:`PromiseExtractor` going forward); legacy rows fall back to the
``"Aiko promised:"`` content prefix. Only **assistant-side** promises
participate in follow-through — the user's own commitments are the
:class:`FollowUpWorker` / proactive-callback territory.

Everything here is pure (memory-like objects in, verdicts out); the
post-turn hook and the idle worker own persistence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from app.core.memory.conflict_heuristics import _content_words, _tokenize

log = logging.getLogger("app.promise_lifecycle")


STATUS_OPEN = "open"
STATUS_SURFACED = "surfaced"
STATUS_FULFILLED = "fulfilled"
STATUS_DROPPED = "dropped"

#: Statuses that still "owe" the user something.
ACTIVE_STATUSES: frozenset[str] = frozenset({STATUS_OPEN, STATUS_SURFACED})

#: Content prefixes for legacy sidedness detection (rows written before
#: ``metadata.promise_who`` existed).
_ASSISTANT_PREFIX = "aiko promised"


def promise_status(memory: Any) -> str:
    """Return the lifecycle status of a promise memory (default: open)."""
    metadata = getattr(memory, "metadata", None) or {}
    status = str(metadata.get("promise_status") or "").strip().lower()
    if status in {STATUS_OPEN, STATUS_SURFACED, STATUS_FULFILLED, STATUS_DROPPED}:
        return status
    return STATUS_OPEN


def is_assistant_promise(memory: Any) -> bool:
    """True when the promise was made by Aiko (not the user).

    Prefers the explicit ``metadata.promise_who`` stamp; legacy rows are
    classified by the rendered content prefix ("Aiko promised: ...").
    """
    metadata = getattr(memory, "metadata", None) or {}
    who = str(metadata.get("promise_who") or "").strip().lower()
    if who:
        return who == "assistant"
    content = str(getattr(memory, "content", "") or "").strip().lower()
    return content.startswith(_ASSISTANT_PREFIX)


def promise_what(memory: Any) -> str:
    """Strip the "<actor> promised:" prefix so cues read naturally."""
    content = str(getattr(memory, "content", "") or "").strip()
    head, sep, tail = content.partition("promised:")
    if sep and len(head) <= 40:
        return tail.strip() or content
    return content


def promise_age_hours(memory: Any, *, now: datetime | None = None) -> float | None:
    """Age of the promise in hours, or ``None`` on unparseable timestamps."""
    created = _parse_iso(getattr(memory, "created_at", None))
    if created is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - created).total_seconds() / 3600.0)


def humanize_age(age_hours: float) -> str:
    """Short friendly age string for the rendered cue."""
    if age_hours < 20.0:
        return "earlier today" if age_hours < 12.0 else "yesterday"
    days = int(round(age_hours / 24.0))
    if days <= 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    weeks = max(1, days // 7)
    return "a week ago" if weeks == 1 else f"{weeks} weeks ago"


def find_fulfilled(
    promises: Sequence[Any],
    reply_text: str,
    *,
    min_overlap: int = 3,
) -> list[Any]:
    """Return active assistant promises this reply plausibly delivered on.

    Lexical only (same content-word overlap idea as revival detection /
    the K38 shortlist): a promise counts as fulfilled when the reply
    shares at least ``min_overlap`` content words with the promise body.
    Conservative on purpose — a false fulfil silently closes a loop the
    user still expects, so short promises whose body has fewer than
    ``min_overlap`` content words require *all* of them to appear.
    """
    reply_words = _content_words(_tokenize(reply_text or ""))
    if not reply_words:
        return []
    out: list[Any] = []
    for mem in promises:
        if promise_status(mem) not in ACTIVE_STATUSES:
            continue
        if not is_assistant_promise(mem):
            continue
        body_words = _content_words(_tokenize(promise_what(mem)))
        if not body_words:
            continue
        needed = min(int(min_overlap), len(body_words))
        if needed <= 0:
            continue
        if len(body_words & reply_words) >= needed:
            out.append(mem)
    return out


def _parse_iso(value: Any) -> datetime | None:
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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "STATUS_OPEN",
    "STATUS_SURFACED",
    "STATUS_FULFILLED",
    "STATUS_DROPPED",
    "ACTIVE_STATUSES",
    "promise_status",
    "is_assistant_promise",
    "promise_what",
    "promise_age_hours",
    "humanize_age",
    "find_fulfilled",
]

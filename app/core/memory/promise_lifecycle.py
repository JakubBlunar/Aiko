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
are ever *surfaced* by follow-through: chasing the user over his own
commitments is a different and much louder product decision than Aiko
closing her own loops.

Both sides age out, though, and H41 is why that sentence had to be
written down. This module used to say the user's commitments were
:class:`FollowUpWorker` territory, and the worker on the other end of
that handoff selects on ``temporal_type == "future_plan"`` — which no
promise has ever had, since :meth:`MemoryStore.add` defaults promises to
``durable``. So the delegation named a real worker that could not see a
single row, and every user-side promise stayed ``open`` forever: 86 of
them, the oldest 86 days, still scoring into retrieval. Nothing logged
anything, because nothing was failing — the two halves simply never met.

A promise also carries ``metadata.promise_deadline`` when the transcript
named one, and *when it is due* is a separate axis from *how long ago it
was made*. Keeping them apart is the difference between noticing a
commitment was missed and noticing one is merely old.

That makes ``promise_who`` load-bearing rather than descriptive, so the
extractor refuses to guess it: a ``who`` naming neither side is dropped
by :func:`~app.core.memory.promise_worker.resolve_promise_who` instead of
defaulting to one, because a wrong side either leaves Aiko owing nothing
for something she said or has the follow-up worker chase the user over a
commitment that was hers — and both look correct in every log line. The
content-prefix fallback below therefore only ever serves rows written
before the stamp existed.

Everything here is pure (memory-like objects in, verdicts out); the
post-turn hook and the idle worker own persistence.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Sequence

from app.core.memory.conflict_heuristics import _content_words, _tokenize
from app.core.infra import timephrase

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


#: The trailing ``(by …)`` the worker writes into promise content. Only
#: matched at the end, so a promise that mentions a bracketed aside
#: mid-sentence keeps it.
_DEADLINE_SUFFIX_RE = re.compile(r"\s*\(by [^)]{0,60}\)\s*$", re.IGNORECASE)


def promise_what(memory: Any) -> str:
    """The bare action, without the "<actor> promised:" or "(by …)" wrapping.

    Both halves are storage format rather than content. Dropping the
    deadline suffix here rather than at each call site keeps it out of
    three places at once: the spoken cue (which states the timing in its
    own words, from the parsed deadline), the dedupe fingerprint (where
    the date tokens made two takes on one commitment look unalike), and
    the fulfilment overlap check (where they could never match a reply
    and so only raised the bar).
    """
    content = str(getattr(memory, "content", "") or "").strip()
    head, sep, tail = content.partition("promised:")
    body = tail.strip() if (sep and len(head) <= 40) else content
    return _DEADLINE_SUFFIX_RE.sub("", body).strip() or body


def promise_age_hours(memory: Any, *, now: datetime | None = None) -> float | None:
    """Age of the promise in hours, or ``None`` on unparseable timestamps.

    Age is how long ago the commitment was *made*, which is a different
    question from whether it is late; see :func:`overdue_hours`.
    """
    created = _parse_iso(getattr(memory, "created_at", None))
    if created is None:
        return None
    ref = now or timephrase.utcnow()
    return max(0.0, (ref - created).total_seconds() / 3600.0)


def promise_deadline(memory: Any) -> datetime | None:
    """When the promise is owed by, or ``None`` when it never said.

    Read from ``metadata.promise_deadline``, stamped at extraction by
    :class:`PromiseExtractionWorker`. ``None`` is the common answer --
    most commitments name no time -- and it means *unknown*, never "not
    due yet".
    """
    metadata = getattr(memory, "metadata", None) or {}
    return _parse_iso(metadata.get("promise_deadline"))


def overdue_hours(memory: Any, *, now: datetime | None = None) -> float | None:
    """How many hours past its deadline the promise is, else ``None``.

    ``None`` covers both "no deadline was stated" and "not late yet", and
    callers only ever branch on truthiness, so collapsing them is safe
    here in a way it is not in :func:`promise_deadline`.

    This is the distinction the worker lacked until H41: it measured
    lateness with :func:`promise_age_hours` and a fixed threshold, so a
    promise made this morning and due by lunch read as fresh all
    afternoon, while a standing commitment with no deadline at all read
    as late purely for having been made a while ago.
    """
    deadline = promise_deadline(memory)
    if deadline is None:
        return None
    ref = now or timephrase.utcnow()
    late = (ref - deadline).total_seconds() / 3600.0
    return late if late > 0.0 else None


def is_overdue(memory: Any, *, now: datetime | None = None) -> bool:
    """True when the promise stated a deadline and that deadline passed."""
    return overdue_hours(memory, now=now) is not None


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
    "promise_deadline",
    "overdue_hours",
    "is_overdue",
    "humanize_age",
    "find_fulfilled",
]

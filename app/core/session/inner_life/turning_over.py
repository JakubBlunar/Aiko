"""K28 — "What I've been turning over" picker.

Selects one recent ``kind="reflection"`` memory (which covers both
:class:`ReflectionWorker` and :class:`DreamWorker` output -- the
latter is identified by a ``[dream] `` content prefix) to surface
as a one-shot inner-life cue on the first user turn after a long
typed gap. The provider in
:mod:`app.core.session.inner_life_providers_mixin._render_turning_over_block`
is the only call-site; this module stays pure-Python (no
controller refs, no SQL, no LLM) so it can be unit-tested in
isolation.

Decision flow:

1. **Age window** -- candidate reflections must satisfy
   ``min_age_hours <= age <= max_age_hours`` (defaults 24h .. 72h).
   The lower bound prevents an immediate post-turn reflection from
   showing up as "I've been thinking about this" two minutes
   later; the upper bound keeps the cue tied to the most recent
   between-session window.
2. **Topical match** -- the candidate's embedding is scored
   against the union of active-goal vectors AND the recent
   user-message vectors. The candidate's ``topical_score`` is
   ``max(over both pools)`` of cosine similarity. Anything below
   ``min_topical_similarity`` (default 0.30) is dropped --
   "still relevant to the current thread" is the contract; a
   random month-old reflection about a one-off conversation
   shouldn't fire when Jacob comes back to talk about something
   completely different.
3. **Recency tie-break** -- among surviving candidates, return
   the *most recent*. Reflections are scratchpad-tier and
   typically die off quickly, so picking the freshest one is
   both the right behavioural default ("she's been turning over
   the latest exchange") and the right cost trade-off (avoids
   over-engineering the v1 picker).

The simple picker is the v1 ship -- a weighted scorer
(``recency * w_r + cosine(goals) * w_g + cosine(threads) * w_t``)
is documented as a fast-follow in
``docs/personality-backlog/shipped.md``. The simple picker's
"topical-or-nothing" gate keeps the v1 conservative: false
silences are vastly preferred to false fires here, because a
"hey, I was turning over your interview" cue that doesn't fit
the moment reads as scripted / performative.

Render: the cue strips a leading ``[dream] `` prefix from the
content and flips the framing (``I dreamed about ...`` vs
``I've been turning this over ...``). The persona block
(:file:`data/persona/aiko_companion.txt`, "What I've been turning
over") teaches Aiko to land the cue as a casual aside, never as
an announcement, and to drop it silently if it doesn't fit the
moment Jacob's bringing in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

import numpy as np

from app.llm.embedder import cosine_similarity


if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory


log = logging.getLogger("app.turning_over")


# Default thresholds. Exported so settings / tests can mirror them
# verbatim and the call-site can wire user overrides through
# without losing the source-of-truth.
DEFAULT_MIN_AGE_HOURS: float = 24.0
DEFAULT_MAX_AGE_HOURS: float = 72.0
DEFAULT_MIN_TOPICAL_SIMILARITY: float = 0.30


# Dream prefix written by :class:`DreamWorker` so the render path
# can distinguish dream output from waking reflections. Mirrors the
# constant in ``app/core/proactive/dream_worker.py`` rather than
# importing it (avoids a cross-package import for one short string).
_DREAM_PREFIX: str = "[dream] "


# K64d knowledge-map reflections carry a ``[mindmap] `` content prefix
# (see ``app/core/proactive/knowledge_map_reflection_worker.py``). They
# surface through this same path as ordinary waking reflections, so the
# render strips the marker but keeps the "thinking about this" framing.
_MINDMAP_PREFIX: str = "[mindmap] "


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TurningOverResult:
    """One picked reflection ready for rendering.

    ``content`` is the *raw* memory content including the leading
    ``[dream] `` prefix when present; the render path strips the
    prefix and flips the framing. ``topical_score`` is the best
    cosine across active goals + recent user messages; rendered in
    the MCP debug payload so we can see why a particular row won.
    """

    memory_id: int
    content: str
    dream: bool
    topical_score: float
    age_hours: float
    # Diagnostic-only: which pool produced ``topical_score`` so the
    # MCP debug payload can show "goal-aligned" vs "thread-aligned".
    # Empty string when both pools were empty (degenerate; usually
    # means the candidate was dropped before this point).
    topical_source: str = field(default="")


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_age_hours(created_at: str | None, *, now: datetime) -> float | None:
    """Return ``(now - created_at)`` in hours, or ``None`` if unparseable."""
    if not created_at:
        return None
    try:
        ts = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now_aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    delta = now_aware - ts
    return delta.total_seconds() / 3600.0


def _best_cosine(
    target: np.ndarray | None,
    pool: Iterable[np.ndarray],
) -> float:
    """Return ``max cosine`` between ``target`` and each pool vector.

    Empty inputs return 0.0 (the gate handles "no match" by
    falling below ``min_topical_similarity``).
    """
    if target is None:
        return 0.0
    best = 0.0
    for vec in pool:
        if vec is None:
            continue
        try:
            score = float(cosine_similarity(target, vec))
        except Exception:
            log.debug("turning-over cosine raised", exc_info=True)
            continue
        if score > best:
            best = score
    return best


# ── Public API ───────────────────────────────────────────────────────────


def pick_turning_over(
    *,
    reflections: Iterable["Memory"],
    active_goal_vecs: Iterable[np.ndarray],
    recent_user_vecs: Iterable[np.ndarray],
    now: datetime,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    min_topical_similarity: float = DEFAULT_MIN_TOPICAL_SIMILARITY,
) -> TurningOverResult | None:
    """Pick the best ``reflection`` to surface as a turning-over cue.

    See module docstring for the decision flow. Returns ``None``
    when no candidate clears the age window AND the topical-match
    gate. Never raises -- defensive against missing embeddings,
    unparseable timestamps, empty pools.
    """
    # Materialise the goal / thread pools once (the pickers may
    # be passed generators that exhaust on first iteration). The
    # filter pass below iterates each candidate against both
    # pools, so we need them as concrete lists.
    goal_vecs = [v for v in active_goal_vecs if v is not None]
    user_vecs = [v for v in recent_user_vecs if v is not None]

    min_age = max(0.0, float(min_age_hours))
    max_age = max(min_age + 1e-6, float(max_age_hours))
    threshold = max(0.0, min(1.0, float(min_topical_similarity)))

    best: TurningOverResult | None = None
    for mem in reflections:
        if mem is None:
            continue
        age = _parse_age_hours(getattr(mem, "created_at", None), now=now)
        if age is None:
            continue
        if age < min_age or age > max_age:
            continue
        embedding = getattr(mem, "embedding", None)
        if embedding is None:
            continue
        try:
            if embedding.size == 0:
                continue
        except AttributeError:
            continue
        goal_score = _best_cosine(embedding, goal_vecs)
        thread_score = _best_cosine(embedding, user_vecs)
        topical = max(goal_score, thread_score)
        if topical < threshold:
            continue
        source = ""
        if goal_score >= thread_score and goal_score > 0.0:
            source = "goal"
        elif thread_score > 0.0:
            source = "thread"
        content = str(getattr(mem, "content", "") or "")
        candidate = TurningOverResult(
            memory_id=int(getattr(mem, "id", 0) or 0),
            content=content,
            dream=content.startswith(_DREAM_PREFIX),
            topical_score=float(topical),
            age_hours=float(age),
            topical_source=source,
        )
        # Recency tie-break: prefer the *youngest* surviving row
        # (smaller ``age_hours``). When ages are exactly equal --
        # e.g. two reflections written in the same second -- the
        # higher topical_score wins so deterministic test cases
        # don't depend on dict insertion order.
        if (
            best is None
            or candidate.age_hours < best.age_hours
            or (
                candidate.age_hours == best.age_hours
                and candidate.topical_score > best.topical_score
            )
        ):
            best = candidate
    return best


# ── Render ───────────────────────────────────────────────────────────────


def render_inner_life_block(
    result: TurningOverResult,
    *,
    user_display_name: str = "the user",
) -> str:
    """Render ``result`` into a system-prompt-ready block.

    The cue strips the ``[dream] `` prefix from dream-sourced
    rows and flips the framing -- "I dreamed about ..." vs
    "I've been turning this over ...". The persona block ("What
    I've been turning over") carries the anti-announcement
    discipline; this render text is the bare prompt cue.
    """
    raw = (result.content or "").strip()
    if result.dream and raw.startswith(_DREAM_PREFIX):
        raw = raw[len(_DREAM_PREFIX):].lstrip()
    elif raw.startswith(_MINDMAP_PREFIX):
        raw = raw[len(_MINDMAP_PREFIX):].lstrip()
    # Trim very long reflections so the cue stays compact. The
    # full content is still on the memory row; this trim is just
    # so the system prompt doesn't carry a 400-char journal entry.
    if len(raw) > 200:
        raw = raw[:197].rstrip() + "\u2026"
    if result.dream:
        head = (
            f"Turning over: between sessions you dreamed about "
            f"this -- '{raw}'."
        )
    else:
        head = (
            f"Turning over: between sessions you've been thinking "
            f"about this -- '{raw}'."
        )
    body = (
        f"Fold it in as a casual aside if it fits the moment "
        f"{user_display_name} is bringing in -- one short beat, "
        "your voice, never announce it (\"actually, I was "
        "thinking about ...\" not \"I have something to share\"). "
        "If it doesn't fit what they actually want to talk about, "
        "drop it silently. The cue is permission, not an "
        "obligation."
    )
    return f"{head}\n{body}"


__all__ = [
    "TurningOverResult",
    "pick_turning_over",
    "render_inner_life_block",
    "DEFAULT_MIN_AGE_HOURS",
    "DEFAULT_MAX_AGE_HOURS",
    "DEFAULT_MIN_TOPICAL_SIMILARITY",
]

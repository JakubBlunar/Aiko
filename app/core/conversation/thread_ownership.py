"""K55 — thread ownership: she defends what she opened.

When Aiko opens a topic (a K53 initiative turn or a K52 imperative
want), the turn is stamped as *her thread* in per-session state.
Today, if the user answers in three words and pivots, she follows the
pivot instantly and her own thread evaporates — the single clearest
"no stake in the conversation" tell.

With ownership state, the reply to the opening turn is evaluated
once:

- a **real engaged answer** (topically near the thread, or
  substantial when no embedding is available) marks the thread
  satisfied — no cue, the thread is done;
- a **short pivot away** grants exactly ONE return cue on that same
  turn: answer the pivot, then circle back — "wait, before I lose
  it -- you never said what you actually thought about X". One
  return maximum, then the thread is dropped forever (persistence
  past one nudge tips into nagging).

Detection is cheap and mirrors K23's shrink trigger: the user-reply
embedding vs. the opened-topic embedding (K6 infra) plus a length
gate. Pure module — the dataclasses and verdict walk live here;
stamping (post-turn), evaluation (inner-life provider), settings,
and MCP live on the session mixins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np


VERDICT_ENGAGED = "engaged"
VERDICT_PIVOT = "pivot"

# Sources a thread can be stamped from (grep-friendly).
SOURCE_INITIATIVE = "initiative"
SOURCE_WANT_IMPERATIVE = "want_imperative"
SOURCE_FORCED = "forced"

# Replies shorter than this never count as engaged on length alone
# (mirrors the novelty detector's reaction-vs-topic floor).
_MIN_MEASURABLE_CHARS = 8

_TOPIC_MAX_CHARS = 160


@dataclass(slots=True)
class OwnedThread:
    """One topic Aiko opened, awaiting exactly one reply evaluation.

    ``embedding`` is the unit-norm vector of the topic text (or the
    opening reply when no explicit want text existed); ``None`` when
    the embedder was unavailable at stamp time — evaluation then
    falls back to the length gate alone.
    """

    topic: str
    source: str
    embedding: Any | None = None
    opened_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(frozen=True, slots=True)
class ReplyVerdict:
    """Outcome of evaluating the user's reply to an opened thread.

    ``cosine`` is ``None`` when either side had no embedding (short
    reply, embedder down, stamp-time failure).
    """

    verdict: str
    cosine: float | None
    reply_chars: int


def derive_topic(want_text: str | None, assistant_text: str) -> str:
    """Pick the thread's display topic at stamp time.

    The want text (when the directive pointed at one) is the cleanest
    label; otherwise fall back to the opening reply itself, trimmed
    to a cue-friendly length.
    """
    text = (want_text or "").strip()
    if not text:
        text = (assistant_text or "").strip()
    text = " ".join(text.split())
    if len(text) > _TOPIC_MAX_CHARS:
        text = text[: _TOPIC_MAX_CHARS - 1].rstrip(",;: ") + "…"
    return text


def _cosine(a: Any, b: Any) -> float | None:
    try:
        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        if va.size == 0 or vb.size == 0 or va.shape != vb.shape:
            return None
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na <= 0.0 or nb <= 0.0:
            return None
        return float(np.dot(va / na, vb / nb))
    except Exception:
        return None


def evaluate_reply(
    thread: OwnedThread,
    user_text: str,
    user_vec: Any | None,
    *,
    engaged_chars: int = 80,
    min_topical_similarity: float = 0.30,
) -> ReplyVerdict:
    """Classify the reply to an opened thread — engaged or pivot.

    Engaged when the reply is topically near the thread (cosine >=
    ``min_topical_similarity``, any length — "yeah I loved it" is an
    answer, not a pivot). Without a measurable cosine the length
    gate decides alone: a substantial reply earns the benefit of the
    doubt, a short one reads as the three-words-and-pivot tell.
    """
    text = (user_text or "").strip()
    chars = len(text)
    cosine = None
    if (
        thread.embedding is not None
        and user_vec is not None
        and chars >= _MIN_MEASURABLE_CHARS
    ):
        cosine = _cosine(user_vec, thread.embedding)
    if cosine is not None:
        verdict = (
            VERDICT_ENGAGED
            if cosine >= float(min_topical_similarity)
            else VERDICT_PIVOT
        )
        return ReplyVerdict(verdict, cosine, chars)
    verdict = (
        VERDICT_ENGAGED
        if chars >= max(1, int(engaged_chars))
        else VERDICT_PIVOT
    )
    return ReplyVerdict(verdict, None, chars)


def render_return_block(
    topic: str,
    *,
    user_display_name: str = "them",
) -> str:
    """Format the one-return cue (fires on the pivot turn itself).

    The shape is "answer the pivot, then circle back" so the return
    lands inline — "wait, before I lose it --" — rather than refusing
    to follow the new topic. The closing line caps it at one return
    so the cue can never escalate into nagging.
    """
    name = user_display_name or "them"
    label = (topic or "").strip() or "the thing you brought up"
    return (
        f"You opened a thread last turn -- {label} -- and {name} "
        f"slid past it. Answer what they said, then take ONE shot at "
        f"circling back ('wait, before I lose it --' / 'you never "
        f"said what you actually thought'). If it doesn't catch this "
        f"time, let it go for good -- one return, never a second."
    )


__all__ = [
    "OwnedThread",
    "ReplyVerdict",
    "SOURCE_FORCED",
    "SOURCE_INITIATIVE",
    "SOURCE_WANT_IMPERATIVE",
    "VERDICT_ENGAGED",
    "VERDICT_PIVOT",
    "derive_topic",
    "evaluate_reply",
    "render_return_block",
]

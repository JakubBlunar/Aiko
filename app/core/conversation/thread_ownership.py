"""K55 / K89 — thread ownership: she defends what she opened.

When Aiko opens a topic (a K53 initiative turn or a K52 imperative
want), the turn is stamped as *her thread* in per-session state.
Without it, if the user answers in three words and pivots, she follows
the pivot instantly and her own thread evaporates — the single
clearest "no stake in the conversation" tell.

**K89 turned the one-shot slot into a short-lived stake.** The
original design consumed its single evaluation on the first reply,
which models a polite attempt rather than an interest: one nudge, then
the topic is gone forever whatever happened. A thread now carries a
``stake`` that decays every turn it goes unanswered and survives more
than one reply, so she can come back to something twice — and, more
importantly, so the *give-up* becomes a decision rather than a
side effect of running out of slots.

The reply to a thread is one of three things:

- an **engaged answer** (topically near the thread, or substantial
  when no embedding is available) satisfies it — no cue, done;
- a **short pivot away** — the three-words-and-slide tell — spends
  part of the stake and buys a return cue: answer the pivot, then
  circle back ("wait, before I lose it -- you never said what you
  actually thought about X");
- **moving on** — a substantial reply that is topically elsewhere.
  He is not brushing her off, he is talking about something else, and
  circling back over a real answer is the nagging this whole feature
  has to avoid. The thread retires silently.

Three ways a thread dies without being answered, and they matter more
than the persistence does: the stake decays below ``min_stake``, the
returns run out, or the cosine keeps falling across returns — he isn't
biting, and the second nudge is retired before it is spent.

Detection is cheap and mirrors K23's shrink trigger: the user-reply
embedding vs. the opened-topic embedding (K6 infra) plus a length
gate. Pure module — the dataclasses, the verdict walk and the stake
arithmetic live here; stamping (post-turn), evaluation (inner-life
provider), settings, and MCP live on the session mixins.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import numpy as np
from app.core.infra import timephrase


VERDICT_ENGAGED = "engaged"
VERDICT_PIVOT = "pivot"
# K89: a substantial reply that is topically somewhere else. Distinct
# from ``pivot`` because the right response is the opposite one --
# a brush-off is worth a nudge, a genuine change of subject is not.
VERDICT_MOVED_ON = "moved_on"

# Why a thread stopped being live, for the log line and the MCP dump.
RETIRE_SATISFIED = "satisfied"
RETIRE_MOVED_ON = "moved_on"
RETIRE_STAKE_SPENT = "stake_spent"
RETIRE_RETURNS_SPENT = "returns_spent"
RETIRE_NOT_BITING = "not_biting"
RETIRE_TOO_OLD = "too_old"

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
    """One topic Aiko opened, and how much of a stake she still has in it.

    ``embedding`` is the unit-norm vector of the topic text (or the
    opening reply when no explicit want text existed); ``None`` when
    the embedder was unavailable at stamp time — evaluation then
    falls back to the length gate alone.

    ``stake`` is the K89 pressure: it starts full, loses ``decay`` on
    every reply that doesn't answer the thread, and the thread retires
    when it runs out. ``last_cosine`` remembers how close the previous
    unanswered reply came, which is the only evidence available for
    whether a second nudge is worth taking.
    """

    topic: str
    source: str
    embedding: Any | None = None
    opened_at: datetime = field(
        default_factory=lambda: timephrase.utcnow()
    )
    stake: float = 1.0
    returns_used: int = 0
    last_cosine: float | None = None


@dataclass(frozen=True, slots=True)
class ThreadOutcome:
    """What a reply did to a thread: the next state, and whether to nudge.

    ``thread`` is ``None`` once the thread is retired. ``reason`` is
    one of the ``RETIRE_*`` constants when it is, and ``"returning"``
    while the thread is still live.
    """

    thread: "OwnedThread | None"
    cue: bool
    reason: str


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
    """Classify the reply to an opened thread.

    Engaged when the reply is topically near the thread (cosine >=
    ``min_topical_similarity``, any length — "yeah I loved it" is an
    answer, not a pivot). Off-topic splits on length, which is the
    K89 distinction the one-shot design couldn't make: a short reply
    is the three-words-and-slide brush-off and is worth a nudge, while
    a substantial one is a person who answered and then went somewhere
    else, and nudging *that* is the nagging the feature must not do.

    Without a measurable cosine the length gate decides alone. Nothing
    can be called ``moved_on`` there — with no topical read, a long
    reply is indistinguishable from a long answer, so it gets the
    benefit of the doubt exactly as before.
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
        if cosine >= float(min_topical_similarity):
            return ReplyVerdict(VERDICT_ENGAGED, cosine, chars)
        verdict = (
            VERDICT_MOVED_ON
            if chars >= max(1, int(engaged_chars))
            else VERDICT_PIVOT
        )
        return ReplyVerdict(verdict, cosine, chars)
    verdict = (
        VERDICT_ENGAGED
        if chars >= max(1, int(engaged_chars))
        else VERDICT_PIVOT
    )
    return ReplyVerdict(verdict, None, chars)


def advance(
    thread: OwnedThread,
    verdict: ReplyVerdict,
    *,
    now: datetime | None = None,
    max_returns: int = 2,
    stake_decay: float = 0.35,
    min_stake: float = 0.25,
    max_age_minutes: float = 45.0,
    cooling_margin: float = 0.05,
) -> ThreadOutcome:
    """Walk one reply against the thread's stake (K89).

    The persistence half is trivial — keep the thread, spend a return.
    The design is entirely in the five ways it stops, because a thread
    that never lets go is worse than one that never persists:

    - **satisfied** — he answered it;
    - **moved on** — he answered *something*, elsewhere. A return here
      would be talking over a real reply;
    - **too old** — a thread is a live thing. One from an hour ago is
      a resurrection, not a return, however much stake is left;
    - **stake spent** — the ordinary end. A return costs
      ``stake_decay`` and is only granted while what's left after
      paying stays above ``min_stake``, so the defaults buy exactly
      two. ``max_returns`` is a guard rail on top of that arithmetic,
      not the thing that normally decides: no combination of settings
      can turn this into a third nudge;
    - **not biting** — the soft one, and the only one that reads the
      conversation. If a second unanswered reply is *further* from the
      thread than the first was, the drift is away from her, and the
      remaining return is retired rather than spent. This is what
      stops "two returns" from meaning "two nudges, always".
    """
    if verdict.verdict == VERDICT_ENGAGED:
        return ThreadOutcome(None, False, RETIRE_SATISFIED)
    if verdict.verdict == VERDICT_MOVED_ON:
        return ThreadOutcome(None, False, RETIRE_MOVED_ON)

    if thread.returns_used >= max(1, int(max_returns)):
        return ThreadOutcome(None, False, RETIRE_RETURNS_SPENT)
    if _age_minutes(thread, now) > max(0.0, float(max_age_minutes)):
        return ThreadOutcome(None, False, RETIRE_TOO_OLD)

    stake = float(thread.stake) - max(0.0, float(stake_decay))
    if stake < float(min_stake):
        return ThreadOutcome(None, False, RETIRE_STAKE_SPENT)
    if (
        thread.returns_used > 0
        and thread.last_cosine is not None
        and verdict.cosine is not None
        and verdict.cosine
        < thread.last_cosine - max(0.0, float(cooling_margin))
    ):
        return ThreadOutcome(None, False, RETIRE_NOT_BITING)

    nxt = replace(
        thread,
        stake=stake,
        returns_used=thread.returns_used + 1,
        last_cosine=(
            verdict.cosine
            if verdict.cosine is not None
            else thread.last_cosine
        ),
    )
    # Retire on the way out when nothing could buy another return, so
    # the cue can honestly say it is the last one and no spent thread
    # is left sitting in the slot waiting to be told so.
    if nxt.returns_used >= max(1, int(max_returns)):
        return ThreadOutcome(None, True, RETIRE_RETURNS_SPENT)
    if nxt.stake - max(0.0, float(stake_decay)) < float(min_stake):
        return ThreadOutcome(None, True, RETIRE_STAKE_SPENT)
    return ThreadOutcome(nxt, True, "returning")


def _age_minutes(thread: OwnedThread, now: datetime | None) -> float:
    """Minutes since the thread was opened; ``0`` if unmeasurable.

    An unreadable stamp must not retire a live thread, so a bad clock
    reads as brand new rather than as expired.
    """
    try:
        moment = now if now is not None else timephrase.utcnow()
        opened = thread.opened_at
        if opened.tzinfo is None or moment.tzinfo is None:
            opened = opened.replace(tzinfo=None)
            moment = moment.replace(tzinfo=None)
        return max(0.0, (moment - opened).total_seconds() / 60.0)
    except Exception:
        return 0.0


def render_return_block(
    topic: str,
    *,
    user_display_name: str = "them",
    attempt: int = 1,
    last: bool = True,
) -> str:
    """Format a return cue (fires on the pivot turn itself).

    The shape is "answer the pivot, then circle back" so the return
    lands inline — "wait, before I lose it --" — rather than refusing
    to follow the new topic.

    A second return is quieter than the first by construction. The
    first can be a direct ask; the second has to be light enough that
    it costs him nothing to ignore, because he already has once. The
    closing line differs on ``last`` so the model knows whether it is
    holding anything back.
    """
    name = user_display_name or "them"
    label = (topic or "").strip() or "the thing you brought up"
    if attempt <= 1:
        opening = (
            f"You opened a thread last turn -- {label} -- and {name} "
            f"slid past it. Answer what they said, then take ONE shot "
            f"at circling back ('wait, before I lose it --' / 'you "
            f"never said what you actually thought')."
        )
    else:
        opening = (
            f"You're still holding a thread {name} hasn't picked up -- "
            f"{label}. Answer what they said, then touch it ONCE more, "
            f"lighter than last time -- half a sentence, easy to walk "
            f"past ('still curious about that, by the way'). No "
            f"reproach, and don't point out that you already asked."
        )
    closing = (
        " If it doesn't catch this time, let it go for good."
        if last
        else " If it doesn't catch, you get one more shot later -- so "
        "don't spend everything on this one."
    )
    return opening + closing


__all__ = [
    "OwnedThread",
    "ReplyVerdict",
    "RETIRE_MOVED_ON",
    "RETIRE_NOT_BITING",
    "RETIRE_RETURNS_SPENT",
    "RETIRE_SATISFIED",
    "RETIRE_STAKE_SPENT",
    "RETIRE_TOO_OLD",
    "SOURCE_FORCED",
    "SOURCE_INITIATIVE",
    "SOURCE_WANT_IMPERATIVE",
    "ThreadOutcome",
    "VERDICT_ENGAGED",
    "VERDICT_MOVED_ON",
    "VERDICT_PIVOT",
    "advance",
    "derive_topic",
    "evaluate_reply",
    "render_return_block",
]

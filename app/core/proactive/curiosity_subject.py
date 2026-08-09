"""Is this curiosity about him, or about something? (K87)

Three generators draft the things Aiko is curious about --
:class:`~app.core.proactive.curiosity_worker.CuriosityWorker` (next-turn
follow-ups), :class:`~app.core.proactive.curiosity_seed_worker.CuriositySeedWorker`
(lateral topics) and
:class:`~app.core.proactive.forward_curiosity_worker.ForwardCuriosityWorker`
(gap-return wonderings) -- and every one of them was written to be
curious about the *user*. All ten stored ``open_question`` rows began
"Maybe ask Jacob", the seed prompt asked for topics she is curious about
"with {user}", and the forward worker draws its candidates exclusively
from his ``future_plan`` and ``callback`` memories. So the more of the
curiosity stack ran, the further into interview mode she went, and
nothing downstream could tell the difference because there was no
difference to see.

This module is the small shared vocabulary for fixing that. It holds no
state and imports nothing first-party, so all three generators can use
the same definition of "about a subject" and the prompt assembler can
use it to decide how to *frame* a note it did not write.

**The quota is a deficit check, not a dice roll.** A coin flip at p=0.4
gives you 40% in expectation and long runs of neither, and these
generators fire a handful of times a day -- a week of bad luck is a week
of pure interviewing. :func:`wants_subject` instead asks whether the
observed share is already at quota, which converges on the ratio from
the first draft and cannot drift.
"""
from __future__ import annotations

import math
import re

MODE_SUBJECT = "subject"
MODE_PERSON = "person"

# Pronouns and possessives that point at the person being talked to or
# about. A note carrying any of these is asking after him rather than
# after a subject.
_PERSON_TOKENS: frozenset[str] = frozenset({
    "you", "your", "yours", "you're", "youre", "yourself",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves",
})

_WORD_RE = re.compile(r"[a-z0-9']+")


def is_person_directed(text: str, user_name: str = "") -> bool:
    """Does this curiosity point at a person rather than a subject?

    Deliberately conservative in one direction: a subject wondering that
    happens to mention him ("what he said about fermentation") is
    classified as person-directed. Over-counting the interview side
    means the quota errs toward producing more subject material, which
    is the direction we want to be wrong in.
    """
    tokens = set(_WORD_RE.findall((text or "").lower()))
    if tokens & _PERSON_TOKENS:
        return True
    name = (user_name or "").strip().lower()
    return bool(name) and name in tokens


def subject_share(modes: object) -> float:
    """Share of ``modes`` that were subject-mode. ``0.0`` when empty."""
    items = [str(m) for m in (modes or ())]  # type: ignore[union-attr]
    if not items:
        return 0.0
    return sum(1 for m in items if m == MODE_SUBJECT) / len(items)


def wants_subject(modes: object, *, quota: float) -> bool:
    """Should the next draft be about a subject rather than about him?

    ``modes`` is the recent history, oldest first. With no history the
    answer is "yes" for any positive quota, which front-loads the first
    draft after a restart onto her own material -- the side that has
    been starved.
    """
    target = max(0.0, min(1.0, float(quota)))
    if target <= 0.0:
        return False
    if target >= 1.0:
        return True
    return subject_share(modes) < target


def deficit(modes: object, *, quota: float, total: int) -> int:
    """How many of ``total`` items must be subject-mode to hit ``quota``.

    Used by the seed worker, which decides a batch at a time rather than
    one draft at a time. Counts what is already in stock so a pool that
    is already rich in subject seeds does not force more of them.

    Rounds up, matching :func:`wants_subject`'s cold-start behaviour. A
    batch of one at a 0.4 quota would round down to nothing forever
    otherwise, and a batch of one is the common case -- the seed worker
    writes at most ``curiosity_seed_max_per_run`` per tick.
    """
    target = max(0.0, min(1.0, float(quota)))
    if target <= 0.0 or total <= 0:
        return 0
    items = [str(m) for m in (modes or ())]  # type: ignore[union-attr]
    have = sum(1 for m in items if m == MODE_SUBJECT)
    want = math.ceil(target * (len(items) + total) - 1e-9)
    return max(0, min(total, want - have))


__all__ = [
    "MODE_PERSON",
    "MODE_SUBJECT",
    "deficit",
    "is_person_directed",
    "subject_share",
    "wants_subject",
]

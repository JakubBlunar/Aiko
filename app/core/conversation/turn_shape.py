"""K95 — what his turn was doing, for the mechanisms that talk over it.

One question, asked by more than one caller: *did he just put something
to her that deserves an answer before she brings up her own thing?*

Why this is its own module rather than a helper on either consumer. The
read is needed in two places that must not be allowed to disagree. K92's
:func:`~app.core.conversation.stance.compute_ceiling` uses it to cap the
stance ladder, and K53's :func:`~app.core.conversation.initiative_director.decide`
uses it to defer the floor-taking directive. Those two reaching different
conclusions about the same message would produce the exact failure K95
exists to insure against -- the ledger recording that she should have
followed while the prompt told her to take the floor -- and a shared
constant is the only arrangement where that cannot happen. ``stance.py``
already learned this lesson once with ``SUBSTANTIAL_CHARS``.

**The gap this closed.** K95 was filed as cheap insurance against a
regression later phases might introduce. It was already load-bearing.
K92 phase 1 built the reader and phase 2 measured it, but nothing
*enforced* it: the stance block speaks only for ``FOLLOW`` and brevity,
so the ceiling was recorded and not obeyed, while K53 -- the most
deliberate floor-taking move Aiko makes -- gated only on his message
being **240 characters or longer**. A length proxy correctly protects a
long explanation and does nothing whatsoever for a short direct
question, which is precisely the case where taking the floor reads
worst. Measured over the stance ledger: ``initiative_block`` rendered on
75 turns and **17 of them (23%) sat under a ``direct_question``
ceiling** the director could not see.

**Why enforcing it costs no initiative.** K53's counter resets only when
the directive actually fires, so a gate here *defers* the beat to the
next turn that is not a question rather than spending it. Same rate,
better placement -- the ``user_substantial`` pattern, which has worked
this way since K53 shipped.

Deliberately not a score. K95's own argument is that interruption cost
must be a **hard filter**: expressed as a weight it can be outvoted by
an accumulated want, and being outvoted is the whole failure mode. So
this returns a bool and its callers branch on it.
"""
from __future__ import annotations

import re
from typing import Any

# The K4 dialogue-act label meaning "he asked something".
_QUESTION_ACT = "question"


def is_direct_question(
    user_text: str | None,
    dialogue_act: Any = None,
) -> bool:
    """Did he actually ask something, as opposed to merely wondering?

    Two signals, OR-ed, and the asymmetry between them is deliberate.
    The K4 dialogue-act tag is the better read of intent but it is
    stamped post-turn, so at assembly time it describes his *previous*
    message; the question mark on the live text is the weaker signal
    about a message we know is the current one. Taking either means a
    stale tag can only ever *add* a deferral.

    That direction is the safe one for a guard. A false positive costs
    one deferred initiative beat, which K53's counter will re-offer on
    the next turn. A false negative is her talking over something he
    actually asked, which K95 exists to prevent and which costs more
    trust than a week of good initiative earns.
    """
    if str(dialogue_act or "").strip().lower() == _QUESTION_ACT:
        return True
    return (user_text or "").rstrip().endswith("?")


_COMPLETION = re.compile(
    r"(?:thanks[,!]?\s+)?(?:that (?:fixed|solved|answers|answered) it|"
    r"(?:that|this|it) (?:works(?: perfectly)?|makes sense)|"
    r"(?:that'?s|that is) (?:all|enough|what i needed)|"
    r"(?:we'?re|we are|i'?m|i am) (?:done|all set))"
    r"(?:[,!]?\s+(?:thanks|thank you))?[.!]*",
    re.IGNORECASE,
)
_ACKNOWLEDGEMENT = re.compile(
    r"(?:thanks(?: so much)?|thank you(?: so much)?|got it|understood|"
    r"sounds good|perfect|great|haha|lol|hehe)[.!]*", re.IGNORECASE,
)
_CLOSED_QUESTION = re.compile(
    r"(?:^|[.!?]\s+)(?:is|are|was|were|do|does|did|can|could|would|will|should|"
    r"have|has)\b[^.!?]*\?\s*$", re.IGNORECASE,
)


def brief_reply_kind(user_text: str, previous_assistant_text: str = "") -> str:
    """Recognize bounded positive evidence, never infer withdrawal from its absence."""
    text = (user_text or "").replace("\u2019", "'").strip()
    if not text or len(text.split()) > 12:
        return ""
    if _COMPLETION.fullmatch(text):
        return "completion"
    if _ACKNOWLEDGEMENT.fullmatch(text):
        return "acknowledgement"
    if (
        _CLOSED_QUESTION.search(previous_assistant_text or "")
        and re.match(r"^(?:yes|no|yep|nope|sure|absolutely)\b", text, re.IGNORECASE)
        and not re.search(
            r"[?]|\b(?:but|except|actually|wrong|misunderstood|stop)\b", text, re.IGNORECASE,
        )
    ):
        return "answer"
    return ""


__all__ = ["is_direct_question", "brief_reply_kind"]

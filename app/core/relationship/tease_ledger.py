"""K59 — tease economy: "you'll pay for that one".

A small payback ledger of mock-grudges. When the user pushes back hard
on Aiko's stance (K29 already detects the contradiction), or a light
offence comes through the K57 trigger lane (a brushed-off thread at
comedy weight rather than sulk weight), Aiko banks a debt. She collects
later — a callback tease one or three conversations down the line ("oh,
like the time you swore my playlist was 'objectively chaotic'? I
remember things."). The memory-backed callback is what makes it feel
like a real ongoing relationship rather than per-turn improv.

**Storage is the shared cue pool**, as ``cue_type="tease_ledger"``. This
module used to carry its own ledger in a ``kv_meta`` JSON key along with
hand-written versions of expiry, capping, offer-stamping and collection
matching -- all five of which the pool already does, and does the same
way for every other cue:

- banking is ``_queue_pool_cue``, and a debt is one ``pending`` row
- the offer stamp is ``mark_surfaced`` / ``state=surfaced``
- a collection miss is ``release()``, which is what brings it round again
- the two-week expiry is ``ttl_hours``, swept with everything else
- collection is ordinary stage-A matching: three shared content words
  between the row's subject and Aiko's reply

Two behaviours the pool has no opinion about stay here, and both are
about the *comedy* rather than the bookkeeping: which debt to reach for
(the oldest -- see ``pick_order`` on the policy) and what counts as the
same grudge banked twice (:func:`is_duplicate`).

What survives in this module is the domain vocabulary: how to name a
debt, how to tell two of them apart, and how the offer reads.
"""
from __future__ import annotations

from app.core.memory import echo_detector


# Shared with consumption on purpose. The pool decides Aiko collected a
# debt by looking for its subject in her reply, so "these two grudges are
# the same" and "this reply is about that grudge" have to mean the same
# thing by the same tokeniser -- otherwise a near-duplicate we let in
# would settle its twin the moment either was offered.
DUPLICATE_OVERLAP = 3


def subject_for(*, what: str, context: str) -> str:
    """The part of a debt that is actually about something.

    The subject is a key before it is a display string: it decides
    supersession, and it is what a collection is matched against. That
    makes the choice load-bearing here in a way it is not for most cues,
    because ``what`` is *generic* on the K29 lane -- literally "they
    pushed back hard on a take of yours" every single time. Keyed on
    that, every pushback debt would supersede the one before it and the
    ledger would never hold more than one; matched on it, a reply
    containing "pushed", "back" and "hard" would settle a debt about
    something else entirely.

    So the specific half wins: the quote for a pushback, the trigger's
    own cause for a light offence. Callers with a better subject than
    either (the K29 sites, which hold the bare quote) pass it directly.
    """
    return " ".join((context or what or "").split())


def is_duplicate(
    subject: str,
    known: set[str],
    *,
    min_overlap: int = DUPLICATE_OVERLAP,
) -> bool:
    """Is this grudge already on the shelf, give or take the wording?

    The pool supersedes on an exact subject match, which is right for a
    topic slug and too strict for a quote: "your playlist is objectively
    chaotic" and "that playlist of yours is chaotic" are one grudge and
    two strings. Banking both would have her collect twice on the same
    joke, which is the one thing the running bit cannot survive.
    """
    words = echo_detector.tokens(subject)
    if not words:
        return False
    threshold = max(1, int(min_overlap))
    return any(
        len(words & echo_detector.tokens(other)) >= threshold
        for other in known
    )


def render_block(
    *,
    what: str,
    context: str = "",
    user_display_name: str = "them",
) -> str:
    """The cue line offering a collection.

    Just the offer. The rails that used to ride along with it -- one
    callback tease, light and affectionate, no opening means skip it --
    are in the ``Collecting on the ledger:`` handling note now, which
    goes out alongside this line whenever the block renders and is
    editable in ``conditional_handling.txt`` like every other one.
    """
    name = user_display_name or "them"
    detail = f" ({context})" if context else ""
    return (
        f"Tease ledger: {name} still owes you for this one -- "
        f"{what}{detail}."
    )


__all__ = [
    "DUPLICATE_OVERLAP",
    "is_duplicate",
    "render_block",
    "subject_for",
]

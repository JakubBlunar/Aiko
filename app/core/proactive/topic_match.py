"""Is the live message about this cue's subject? (H43)

Five cue providers -- ``concept_hypothesis``, ``curiosity_gradient``,
``interest_drift``, ``associative_wander``, ``knowledge_gap_notice`` --
share one answer to that question, and between them it is the single
largest reason Aiko says nothing: ``topic_miss`` is **1,873 of 1,981
eligible cue declines, 94.5%**. So the predicate deciding it is worth more
scrutiny than its fourteen lines had received.

What it was
-----------
One word of three or more characters shared between the cue's label and
his message. The comment called it "a reasonable 'we're talking about this
right now' signal -- enough to keep the gap notice in context without an
embedding round-trip", which is a fair description of the intent and not
of the behaviour.

The consumption half of the cue system had, meanwhile, been banking a
cosine on **every** verdict for months against precisely this question,
with a note that the read was worth retrying once verdicts accumulated.
Retried, over 140 recorded verdicts:

* decided by word overlap -- median cosine **0.370**
* decided semantically ---- median cosine **0.530**, p10 0.510

and the null for this embedder, measured over 4,000 random
message-against-cue pairs, is median **0.369**, p95 0.496, p99 0.559.

The first and third numbers are the same number. **When word overlap said
"this is the moment", the two texts were as related as two texts drawn at
random.** Hand-checked pairs bracket it: genuinely related ones land 0.55
to 0.69, genuinely unrelated 0.25 to 0.36.

Why, and why a stoplist is most of the fix
------------------------------------------
Counting which tokens actually carry the matches across the real corpus:
**82% are function words.** The top carriers are ``and`` (39k matches),
``the`` (34k), ``you`` (26k), then ``your``, ``with``, ``that``, ``when``,
``for``. A three-character floor was doing the work a stoplist should do,
and English's most common words are three and four letters long.

Her own name is on that list too, at 5.5k. Cue subjects are written *about*
the two of them ("jacob's technical projects", "jacob's feelings for
aiko"), and he addresses her by name constantly, so the names match
everything and mean nothing here. They are not hardcoded -- callers pass
them through ``extra_stop``, since they are user-configurable.

Rank, don't gate -- and why that is the whole design
---------------------------------------------------
The obvious fix is to tighten the predicate. Measured, that is the wrong
move, and the measurement is worth keeping because it inverts the premise
the fix started from.

Over every real (subject, message) pair, the gate **as shipped accepts
33.2%**. With a shelf of five cues that is an ~87% chance that something
"matches" every turn: the word test is very nearly a *no-op*. So it was
never what was declining those 1,873 turns -- that was cadence holds
mislabelled, fixed in
:meth:`~app.core.session.cue_pool_mixin.CuePoolMixin.take_pool_cue`.
Stoplist plus cosine together accept 3.8%, a **9x tightening**, which
would make five cue types dramatically quieter in service of a problem
they did not have. The K92-K95 family exists to get *more* of her own
material into conversations, not less.

What the numbers actually indict is not admission but **choice**.
``pick_pool_cue`` returned the *first* row passing the gate, ordered by
surfacings and recency -- nothing to do with what he just said. A shelf
where 33% of everything "matches" plus first-past-the-post selection is
precisely how a cue gets surfaced on a shared ``and``, and precisely why
verdicts decided lexically sit at the null.

So the cosine is used to **order the admitted candidates** rather than to
veto them. The acceptance set is unchanged -- reach cannot fall -- while
the cue she is handed becomes the most relevant one available instead of
the one that happened to be least recently shown. This is the same
correction K93 made to the wants ledger, for the same reason: a signal
good enough to rank with is rarely good enough to gate on, because gating
throws away the cases the signal is only *approximately* right about.

The cosine arm *is* additive at admission, since it can only let through
pairs the word test missed (+1.2% of pairs). Its threshold is sited on the
measured null rather than picked: ~2% of unrelated pairs clear 0.55.

The stoplist is implemented, measured, and **off by default** at
admission. Ranking removes most of its value -- a coincidental ``and``
match now only wins when nothing better is on the shelf -- and turning it
on costs 30.7% of pairs, which is a change to make on production evidence
rather than on a pair-population estimate. What it drops is genuinely
noise (median cosine 0.380 against a null median of 0.392; only 2.1% of
dropped pairs clear the null's p95), so the case for it is sound; it is
the *size* of the change that wants a separate, measured step.

See ``scripts/topic_gate_report.py`` for all of the above, and re-run it
rather than adjusting these constants by feel.

Deliberately not an LLM call. This runs on every pooled cue on every turn,
five times over; the embedding of his message is already computed once per
turn and cached, and the cue's vector was written when the cue was
produced. The comparison is a dot product.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Collection
from typing import Any

log = logging.getLogger("app.topic_match")

_WORD_RE = re.compile(r"[a-z0-9]+")

# Which arm decided a hit. Recorded so the two can be compared in the
# ledger later -- the whole reason the old gate went unexamined for so
# long is that a bool leaves no evidence behind.
ARM_LEXICAL = "lexical"
ARM_COSINE = "cosine"
ARM_NONE = "none"

# Sited on the measured null (p99 = 0.559), so ~1% of unrelated pairs
# clear it. Not a guess, and not to be nudged without re-running the
# report named in the module docstring.
DEFAULT_MIN_COSINE = 0.55

# Function words and conversational filler: tokens that co-occur between
# any two English texts and therefore carry no evidence about subject.
# Assembled from the measured match-carrier counts rather than from a
# generic list, so it covers what actually produced false matches here
# (the top eight carriers alone accounted for ~160k of 187k).
#
# Kept deliberately tight: only words that are *never* a subject. Content
# words that happen to be common ("sleep", "work", "food") stay in, since
# a shared "sleep" genuinely is a topical signal.
STOPWORDS: frozenset[str] = frozenset({
    # articles, conjunctions, prepositions
    "the", "and", "but", "for", "nor", "yet", "with", "without", "from",
    "into", "onto", "over", "under", "about", "after", "before", "during",
    "than", "then", "that", "this", "these", "those", "there", "here",
    "while", "when", "where", "which", "whose", "whom", "what", "why",
    "how", "who", "because", "though", "although", "unless", "until",
    "upon", "off", "out", "own", "per", "via", "not", "nope", "yes",
    "yeah", "yep", "okay", "sure", "also", "too", "very", "quite",
    "really", "just", "even", "still", "already", "again", "ever",
    "never", "always", "maybe", "perhaps", "probably", "actually",
    # pronouns and possessives
    "you", "your", "yours", "she", "her", "hers", "him", "his", "they",
    "them", "their", "theirs", "our", "ours", "its", "itself", "myself",
    "yourself", "herself", "himself", "themselves", "one", "ones",
    "some", "any", "anything", "something", "nothing", "everything",
    "anyone", "someone", "everyone", "nobody", "both", "each", "other",
    "others", "another", "same", "such",
    # auxiliaries and light verbs
    "was", "were", "been", "being", "are", "isnt", "aint", "have", "has",
    "had", "having", "does", "did", "doing", "done", "can", "cant",
    "could", "would", "should", "will", "wont", "shall", "may", "might",
    "must", "get", "gets", "got", "getting", "make", "makes", "made",
    "making", "let", "lets", "put", "take", "takes", "took", "come",
    "comes", "came", "coming", "goes", "going", "went", "gone", "say",
    "says", "said", "saying", "tell", "tells", "told", "seem", "seems",
    "look", "looks", "looking", "keep", "keeps", "kept", "give", "gives",
    "gave", "want", "wants", "wanted", "need", "needs", "needed",
    "like", "likes", "liked", "know", "knows", "knew", "think", "thinks",
    "thought", "feel", "feels", "felt", "mean", "means", "meant",
    "try", "tries", "tried", "use", "uses", "used", "using",
    # vague nouns and quantifiers
    "thing", "things", "stuff", "way", "ways", "bit", "lot", "lots",
    "kind", "sort", "part", "parts", "time", "times", "day", "days",
    "today", "tomorrow", "yesterday", "now", "soon", "later", "much",
    "many", "more", "most", "less", "least", "few", "little", "big",
    "good", "bad", "nice", "great", "fine", "new", "old", "next", "last",
    "first", "second", "long", "short", "high", "low", "right", "wrong",
    "all", "only", "back", "down", "away", "around", "together",
    "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten",
})


def content_words(
    text: str,
    *,
    extra_stop: Collection[str] | None = None,
    drop_stopwords: bool = True,
) -> set[str]:
    """The tokens of ``text`` that could plausibly name a subject.

    Three-character floor kept from the original, but with
    ``drop_stopwords`` it is doing only the job it can do -- dropping
    fragments -- rather than standing in for a stoplist.

    ``drop_stopwords=False`` reproduces the shipped behaviour exactly,
    which is what the admission path still uses; see the module docstring
    on why that is deliberate rather than pending.
    """
    stop: frozenset[str] | set[str] = STOPWORDS if drop_stopwords else frozenset()
    if extra_stop and drop_stopwords:
        stop = set(stop) | {str(w).strip().lower() for w in extra_stop if w}
    return {
        w for w in _WORD_RE.findall((text or "").lower())
        if len(w) >= 3 and w not in stop
    }


def lexical_overlap(
    topic: str,
    user_text: str,
    *,
    extra_stop: Collection[str] | None = None,
    drop_stopwords: bool = True,
) -> set[str]:
    """Content words the cue's label and his message actually share."""
    tw = content_words(
        topic, extra_stop=extra_stop, drop_stopwords=drop_stopwords,
    )
    if not tw:
        return set()
    return tw & content_words(
        user_text, extra_stop=extra_stop, drop_stopwords=drop_stopwords,
    )


def cosine(a: Any, b: Any) -> float | None:
    """Cosine between two stored vectors, or ``None`` if either is absent.

    Tolerant of both list and ndarray, because cue embeddings come back
    from SQLite as lists and the live embed returns an ndarray. Returns
    ``None`` rather than 0.0 on any problem: a missing vector must read as
    "no opinion" so the caller falls through to the lexical arm, and a
    zero would read as "definitely unrelated".
    """
    if a is None or b is None:
        return None
    try:
        import numpy as np

        va = np.asarray(a, dtype=np.float32).reshape(-1)
        vb = np.asarray(b, dtype=np.float32).reshape(-1)
        if va.size == 0 or va.size != vb.size:
            return None
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na <= 0.0 or nb <= 0.0:
            return None
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        log.debug("topic cosine failed", exc_info=True)
        return None


def topical(
    topic: str,
    user_text: str,
    *,
    topic_vec: Any = None,
    user_vec: Any = None,
    min_cosine: float | None = DEFAULT_MIN_COSINE,
    extra_stop: Collection[str] | None = None,
    drop_stopwords: bool = False,
) -> tuple[bool, str, float | None]:
    """``(hit, arm, cosine)`` -- is his message about this subject?

    Lexical first, because it is free and because an exact shared content
    word is the stronger evidence when it exists. The cosine is computed
    either way when both vectors are present, so the returned value is
    available to the caller even on a lexical hit -- that asymmetry is
    what made the consumption gate's own calibration unreadable for its
    first few hundred verdicts, and it is not repeated here.

    ``drop_stopwords`` defaults to ``False`` so that admission matches
    what shipped: the arm is additive only. The score comes back on every
    verdict regardless, which is what lets the caller *rank*.
    """
    score = cosine(topic_vec, user_vec)
    if lexical_overlap(
        topic, user_text,
        extra_stop=extra_stop, drop_stopwords=drop_stopwords,
    ):
        return True, ARM_LEXICAL, score
    if min_cosine is not None and score is not None and score >= min_cosine:
        return True, ARM_COSINE, score
    return False, ARM_NONE, score


__all__ = [
    "ARM_COSINE",
    "ARM_LEXICAL",
    "ARM_NONE",
    "DEFAULT_MIN_COSINE",
    "STOPWORDS",
    "content_words",
    "cosine",
    "lexical_overlap",
    "topical",
]

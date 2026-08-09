"""K90: pure metrics for how much Aiko *leads* a conversation vs *follows* it.

K52-K56 shipped on judgement, and the result was five interacting
mechanisms that all "work" while the behaviour they exist to fix
persists. The only way that was discoverable was hand-reading
transcripts. This module is the instrument: a handful of cheap, stable
measurements over reply text that turn "she's summarising again" into a
number you can diff across a change.

**Nothing here reads the database or the settings.** It takes strings
and returns numbers, so the same functions serve the offline report
(:mod:`scripts.lead_follow_report`) and the REST diagnostics panel.

The anaphoric-opener detector itself lives one level down in
:mod:`app.core.persona.anaphora`, because the K88 style-tracker band
needs it too and this module already depends on the tracker. Sharing it
is the point either way: a second definition of "does this reply open
anaphorically" would drift from the band that fires on it, and the
report would end up measuring something the cue isn't reacting to.

The measurements, and what a bad number looks like:

- **question-end rate** -- she closes on a question instead of leaving
  something to react to. Interviewing.
- **reply length** (mean + median) -- sprawl.
- **opener echo** -- her first sentence is built from his content words.
  Parroting.
- **anaphoric-opener rate** -- her first sentence *cannot stand without
  his*: "Then...", "Exactly.", "That makes sense", "So am I". This is
  the syntactic tell for following, and the one the persona's standing
  DON'T PARROT rule has never been able to catch, because a standing
  rule can't see a rate.
- **own-material ratio** -- what share of her content words were hers
  rather than recycled from his turn or the recent history. The only
  positive signal here; the rest are all absences.

One thing this deliberately does *not* measure: whether she changed the
subject. The backlog specified "share of turns introducing a subject
absent from the user's message", and no bag of words can deliver it --
see the note beside the thresholds below for the two attempts and the
numbers that killed them.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.core.conversation.wants_ledger import content_words

# Reaching into the sibling module for the shared primitives is
# deliberate. ``_extract_features`` already defines "ends on a question"
# (including the trailing-quote case) and "how long is this reply" for
# the K88 band that fires on these same numbers; a private copy here
# would be a second definition that silently drifts from it.
from app.core.persona.aiko_style_tracker import (
    _MIN_TURN_WORDS,
    _extract_features,
)
from app.core.persona.anaphora import (
    PARTICLES,
    first_sentence,
    is_anaphoric_opener,
)


# A turn has to carry at least this many words of its own before it
# counts as adding substance. Below it, a high ratio is just noise off a
# short reply -- two unseen words in a six-word answer is not material.
OWN_MATERIAL_MIN_WORDS = 3
# ...and they have to be a real share of what she said, so a long reply
# that restates him plus one aside doesn't score.
OWN_MATERIAL_MIN_RATIO = 0.30

# No pivot / new-subject metric here, and that is a finding rather than
# an omission. Both were built and both were measured against the live
# log before being cut. "Share of turns introducing a subject absent
# from the user's message" -- the backlog's wording -- reads 95% under
# any bag-of-words definition, because elaborating on his subject
# introduces words he didn't say. Adding an overlap gate on top ("she
# carried substance touching none of his nouns") read 40%, which is
# synonymy: he says coffee, she says caffeine, and the counter calls it
# a change of subject. Both numbers look precise and neither means what
# its name claims, which is the worst thing a diagnostic can do. Telling
# elaboration from a pivot needs sentence embeddings; until something
# here needs them for another reason, the honest report is the four
# metrics below plus the continuous own-material ratio.

# Generic vocabulary: words that can turn up in a reply about anything,
# and therefore never constitute a *subject*. Without this the
# new-subject metric measures vocabulary novelty instead -- measured on
# the live log it read 95%, because "good", "sounds", "fine", "tiny" and
# "part" are all technically absent from his last message.
#
# A static list rather than a corpus-derived one, and that is the whole
# point: this number exists to be compared against itself across a
# change, so the definition has to be stable. Deriving a stopword set
# from document frequency on each run would silently redefine the metric
# between the "before" and the "after" -- and it also can't distinguish
# generic vocabulary from a subject they genuinely discuss often, which
# is a real subject when she reopens it.
#
# It over-prunes (a reply whose only subject is "the world" scores as
# leading nothing) and that is the safe direction: a false negative
# costs one turn, a permissive list costs the metric.
_GENERIC: frozenset[str] = frozenset({
    # modals, auxiliaries, and the shape-words of any sentence
    "would", "could", "should", "will", "have", "having", "been", "being",
    "does", "doesn", "didn", "isn", "aren", "wasn", "weren", "won",
    "can't", "cant", "wont", "might", "must", "shall", "gonna", "wanna",
    "going", "goes", "went", "gone", "done", "doing",
    # pronouns, determiners, quantifiers
    "that", "this", "these", "those", "what", "when", "where", "which",
    "whose", "whom", "your", "yours", "their", "theirs", "them", "they",
    "mine", "ours", "hers", "itself", "myself", "yourself", "himself",
    "herself", "ourselves", "everyone", "someone", "anyone", "nobody",
    "everybody", "anybody", "everything", "something", "anything",
    "nothing", "each", "every", "both", "either", "neither", "many",
    "much", "more", "most", "less", "least", "some", "such", "none",
    "other", "others", "another", "same", "enough", "else",
    # adverbs and connectives
    "really", "very", "just", "also", "quite", "rather", "still", "even",
    "only", "ever", "never", "always", "often", "sometimes", "usually",
    "maybe", "perhaps", "probably", "actually", "honestly", "literally",
    "basically", "simply", "almost", "nearly", "already", "again",
    "once", "twice", "here", "there", "then", "than", "soon", "later",
    "well", "away", "back", "together", "around", "about", "over",
    "under", "into", "onto", "from", "with", "within", "without",
    "between", "through", "during", "before", "after", "while", "until",
    "since", "because", "though", "although", "however", "anyway",
    "instead", "besides", "otherwise", "somehow", "anymore",
    # the verbs of general conversation
    "make", "makes", "made", "making", "take", "takes", "took", "taking",
    "come", "comes", "came", "coming", "give", "gives", "gave", "giving",
    "know", "knows", "knew", "knowing", "think", "thinks", "thought",
    "thinking", "want", "wants", "wanted", "wanting", "need", "needs",
    "needed", "feel", "feels", "felt", "feeling", "look", "looks",
    "looked", "looking", "seem", "seems", "seemed", "sound", "sounds",
    "sounded", "keep", "keeps", "kept", "keeping", "lets", "said",
    "says", "tell", "tells", "told", "telling", "asks", "asked",
    "asking", "find", "finds", "found", "help", "helps", "helped",
    "turn", "turns", "turned", "start", "starts", "started", "starting",
    "stop", "stops", "stopped", "stay", "stays", "stayed", "staying",
    "leave", "leaves", "left", "hear", "hears", "heard", "hearing",
    "show", "shows", "showed", "mean", "means", "meant", "talk", "talks",
    "talked", "talking", "live", "lives", "lived", "living", "love",
    "loves", "loved", "loving", "like", "likes", "liked", "wish",
    "wishes", "hope", "hopes", "hoped", "guess", "guessing", "remember",
    "forget", "forgot", "understand", "believe", "wonder", "wondering",
    "happen", "happens", "happened", "become", "becomes", "became",
    "using", "used", "trying", "tried", "tries", "getting", "gets",
    "putting", "puts", "sitting", "sits", "watch", "watching", "saying",
    "wait", "waits", "waited", "waiting", "supposed", "able",
    # the adjectives of general conversation
    "good", "better", "best", "great", "nice", "fine", "okay", "sure",
    "wrong", "false", "real", "whole", "half", "full", "empty",
    "different", "next", "last", "first", "second", "early", "late",
    "long", "short", "small", "little", "tiny", "huge", "large",
    "high", "hard", "easy", "soft", "warm", "cold", "cool", "quiet",
    "loud", "sweet", "kind", "kinds", "funny", "weird", "strange",
    "sorry", "glad", "happy", "tired", "busy", "ready", "safe", "free",
    "certain", "clear", "close", "open", "important", "possible",
    "simple", "serious", "perfect", "terrible", "awful", "lovely",
    # abstract nouns that stand in for a subject without being one
    "thing", "things", "time", "times", "moment", "moments", "part",
    "parts", "sort", "sorts", "ways", "point", "points", "place",
    "places", "side", "sides", "lots", "stuff", "idea", "ideas",
    "reason", "reasons", "sense", "chance", "case", "fact", "facts",
    "bits", "kinda", "sorta", "yeah", "course", "pretty", "quick",
    "quickly", "obviously", "unrelated", "rest",
})


# ── primitives ──────────────────────────────────────────────────────


def _normalise(word: str) -> str:
    """Drop a possessive or contraction clitic: ``it's`` -> ``it``.

    :func:`content_words` admits apostrophes mid-token, so "you're",
    "i'll" and "it's" all arrive as four-plus-character "content". Cut
    at the apostrophe and the length filter below disposes of them.
    """
    return word.split("'", 1)[0]


def _content(text: str) -> set[str]:
    """Content words that could plausibly name a subject.

    :func:`content_words` keeps any token of four-plus characters that
    isn't on its short stopword list, which is right for the wants
    ledger's overlap check and far too permissive here: it lets through
    "yeah", contractions, and the whole generic vocabulary of English.
    Three filters, applied in order -- clitic, particle, generic.
    """
    words = {_normalise(w) for w in content_words(text)}
    return {
        w for w in words
        if len(w) >= 4 and w not in PARTICLES and w not in _GENERIC
    }


def opener_echo(reply: str, user_text: str) -> float | None:
    """Share of the reply's opening-sentence content words taken from his.

    ``None`` when the opener carries no content words at all ("Yeah.",
    "Oh, absolutely."), which is a different failure from parroting and
    is already counted by :func:`is_anaphoric_opener`. Folding it in as
    a zero would read as "she echoed nothing", i.e. as a good score.
    """
    mine = _content(first_sentence(reply))
    if not mine:
        return None
    theirs = _content(user_text)
    if not theirs:
        return 0.0
    return len(mine & theirs) / float(len(mine))


def own_material_words(
    reply: str,
    user_text: str,
    recent_texts: Iterable[str] = (),
) -> set[str]:
    """Content words in the reply that are in neither his turn nor history."""
    mine = _content(reply)
    if not mine:
        return set()
    seen = _content(user_text)
    for text in recent_texts:
        seen |= _content(text)
    return mine - seen


def brings_own_material(
    reply: str,
    user_text: str,
    recent_texts: Iterable[str] = (),
    *,
    min_words: int = OWN_MATERIAL_MIN_WORDS,
    min_ratio: float = OWN_MATERIAL_MIN_RATIO,
) -> bool:
    """Did she add substance, or recycle what was already on the table?

    **This is not "did she change the subject".** The backlog framed it
    that way and a bag of words cannot deliver it: when he mentions
    cookies and she answers with "chocolate, crunch, fresh, snack",
    every one of those is a word he did not say, and she has still not
    left his subject. Telling elaboration from a pivot needs sentence
    embeddings, and buying that would make this module depend on the
    embedder and stop being runnable over a cold log.

    So it measures the thing bag-of-words *can* see honestly: how much
    of what she said is hers rather than recycled. That is still a real
    lead/follow signal -- "Fine. That's a good way to keep your princess
    from escaping" recycles nearly everything, while an answer that
    brings its own detail does not. For the stronger claim, see
    :func:`is_pivot`.

    Both gates have to clear: enough of her own words to be substance
    rather than an aside, and a big enough share of the reply that a
    long restatement with one novel noun doesn't score.
    """
    mine = _content(reply)
    if not mine:
        return False
    fresh = own_material_words(reply, user_text, recent_texts)
    if len(fresh) < max(1, int(min_words)):
        return False
    return (len(fresh) / float(len(mine))) >= float(min_ratio)


def own_material_ratio(
    reply: str,
    user_text: str,
    recent_texts: Iterable[str] = (),
) -> float | None:
    """What share of her content words were hers rather than recycled?

    Reported as the headline instead of a thresholded rate, because a
    threshold here would be arbitrary -- there is no principled bar at
    which "enough of this was hers" becomes true -- and a continuous
    mean diffs across a change far better than a rate that spends its
    life pinned near 100%.

    ``None`` when the reply carries no content words at all.
    """
    mine = _content(reply)
    if not mine:
        return None
    fresh = own_material_words(reply, user_text, recent_texts)
    return len(fresh) / float(len(mine))


# ── per-turn + aggregate ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TurnMetrics:
    """One assistant reply, measured against the user turn it answered."""

    word_count: int
    question_count: int
    ends_with_question: bool
    anaphoric_opener: bool
    opener: str
    opener_echo: float | None
    own_material: float | None


@dataclass(frozen=True, slots=True)
class LeadFollowSummary:
    """Aggregate over a run of turns. All rates are fractions in [0, 1]."""

    turns: int
    question_end_rate: float
    avg_questions: float
    mean_words: float
    median_words: float
    anaphoric_opener_rate: float
    mean_opener_echo: float
    opener_echo_turns: int
    mean_own_material: float
    own_material_turns: int
    top_openers: tuple[tuple[str, int], ...]


def measure_turn(
    reply: str,
    user_text: str = "",
    recent_texts: Iterable[str] = (),
) -> TurnMetrics:
    """Measure one assistant reply. Expects meta tags already stripped."""
    features = _extract_features(reply or "")
    return TurnMetrics(
        word_count=features.word_count,
        question_count=features.question_count,
        ends_with_question=features.ends_with_question,
        anaphoric_opener=is_anaphoric_opener(reply),
        opener=features.opener,
        opener_echo=opener_echo(reply, user_text),
        own_material=own_material_ratio(reply, user_text, recent_texts),
    )


def is_measurable(reply: str) -> bool:
    """Is this reply long enough to mean anything?

    Same floor the style tracker uses, for the same reason: a one-word
    "yeah." is a reaction, not a measurable reply, and letting them into
    the corpus drags every average toward zero.
    """
    return len((reply or "").strip().split()) >= _MIN_TURN_WORDS


def summarise(turns: Sequence[TurnMetrics]) -> LeadFollowSummary:
    """Fold per-turn measurements into the report's headline numbers."""
    total = len(turns)
    if total == 0:
        return LeadFollowSummary(
            turns=0,
            question_end_rate=0.0,
            avg_questions=0.0,
            mean_words=0.0,
            median_words=0.0,
            anaphoric_opener_rate=0.0,
            mean_opener_echo=0.0,
            opener_echo_turns=0,
            mean_own_material=0.0,
            own_material_turns=0,
            top_openers=(),
        )

    words = [t.word_count for t in turns]
    echoes = [t.opener_echo for t in turns if t.opener_echo is not None]
    owned = [t.own_material for t in turns if t.own_material is not None]
    openers: dict[str, int] = {}
    for turn in turns:
        if turn.opener:
            openers[turn.opener] = openers.get(turn.opener, 0) + 1
    ranked = sorted(openers.items(), key=lambda kv: (-kv[1], kv[0]))

    return LeadFollowSummary(
        turns=total,
        question_end_rate=sum(1 for t in turns if t.ends_with_question) / total,
        avg_questions=sum(t.question_count for t in turns) / total,
        mean_words=sum(words) / total,
        median_words=float(statistics.median(words)),
        anaphoric_opener_rate=(
            sum(1 for t in turns if t.anaphoric_opener) / total
        ),
        mean_opener_echo=(sum(echoes) / len(echoes)) if echoes else 0.0,
        opener_echo_turns=len(echoes),
        mean_own_material=(sum(owned) / len(owned)) if owned else 0.0,
        own_material_turns=len(owned),
        top_openers=tuple(ranked[:5]),
    )


def as_dict(summary: LeadFollowSummary) -> dict[str, object]:
    """JSON-ready view, for ``--json`` and the diagnostics endpoint."""
    return {
        "turns": summary.turns,
        "question_end_rate": round(summary.question_end_rate, 4),
        "avg_questions": round(summary.avg_questions, 4),
        "mean_words": round(summary.mean_words, 2),
        "median_words": round(summary.median_words, 2),
        "anaphoric_opener_rate": round(summary.anaphoric_opener_rate, 4),
        "mean_opener_echo": round(summary.mean_opener_echo, 4),
        "opener_echo_turns": summary.opener_echo_turns,
        "mean_own_material": round(summary.mean_own_material, 4),
        "own_material_turns": summary.own_material_turns,
        "top_openers": [
            {"opener": word, "count": count}
            for word, count in summary.top_openers
        ],
    }


__all__ = [
    "OWN_MATERIAL_MIN_RATIO",
    "OWN_MATERIAL_MIN_WORDS",
    "LeadFollowSummary",
    "TurnMetrics",
    "as_dict",
    "brings_own_material",
    "first_sentence",
    "is_anaphoric_opener",
    "is_measurable",
    "measure_turn",
    "opener_echo",
    "own_material_ratio",
    "own_material_words",
    "summarise",
]

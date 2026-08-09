"""Does a reply open on a sentence that cannot stand without his? (K88/K90)

A leaf module with no first-party imports, and that is the whole reason
it exists. Two callers need this detector and they already depend on
each other in the other direction: the K90 report
(:mod:`app.core.persona.lead_follow_metrics`) reuses the style tracker's
feature extractor, and the K88 band inside
:mod:`app.core.persona.aiko_style_tracker` needs the detector. Putting
it in either one makes a cycle; putting it here makes both imports
trivial.

Sharing it matters beyond the import graph. If the tracker had its own
copy, the cue Aiko sees and the number the report diffs against the
baseline would drift apart, and we would end up "fixing" a rate nothing
was actually reacting to.
"""
from __future__ import annotations

import re

# Sentence splitter: end-of-sentence punctuation runs.
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_WORD_RE = re.compile(r"[a-z0-9']+")

# Tokens that can open a sentence without committing to anything --
# interjections, acknowledgements, and the connectives that hitch her
# sentence to his. Stripped before we look at what the clause is
# actually *about*: "Oh, I finally finished the book" leads despite the
# "oh", while "Oh, that's rough" does not. A sentence made of nothing
# but these is pure response.
PARTICLES: frozenset[str] = frozenset({
    "oh", "ah", "aw", "aww", "ha", "haha", "hah", "heh", "hm", "hmm",
    "mm", "mmm", "huh", "wow", "oof", "ouch", "god", "okay", "ok",
    "alright", "well", "yeah", "yea", "yep", "yup", "yes", "no", "nope",
    "nah", "sure", "right", "true", "exactly", "absolutely",
    "definitely", "totally", "agreed", "fair", "same", "honestly",
    "actually", "so", "then", "but", "and", "plus", "also", "still",
    "though", "anyway", "besides", "because", "hence", "therefore",
})

# Pro-forms that, as the subject of her first clause, point back at his
# sentence rather than at anything she has said.
#
# ``it`` and ``they`` are deliberately absent. Expletive "it" ("it's
# been raining all afternoon") is a dummy subject introducing her own
# observation, and counting it would inflate the rate with exactly the
# turns we want to reward. Demonstratives carry no such ambiguity.
ANAPHORIC_SUBJECTS: frozenset[str] = frozenset({
    "that", "this", "those", "these", "there", "which",
})

# Whole-phrase openers that are pure echo: her clause mirrors the shape
# of his and has no content of its own. Matched as a prefix so "So am I,
# honestly" still counts.
ECHO_OPENERS: tuple[str, ...] = (
    "so am i", "so do i", "so did i", "so have i", "so would i",
    "so is it", "so was i", "as do i", "as am i",
    "neither do i", "neither did i", "neither am i", "nor do i",
    "me too", "me neither", "same here", "same to you",
    "you're right", "youre right", "you are right", "you were right",
    "you're not wrong", "youre not wrong", "you have a point",
    "fair point", "good point", "fair enough",
)


def sentences(text: str) -> list[str]:
    """Split into non-empty sentences."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    return [s.strip() for s in SENT_SPLIT_RE.split(cleaned) if s.strip()]


def first_sentence(text: str) -> str:
    """The opening sentence of ``text``, or ``""`` when there is none."""
    parts = sentences(text)
    return parts[0] if parts else ""


def is_anaphoric_opener(text: str) -> bool:
    """Does her opening clause depend on his to make sense?

    Three shapes count, and one deliberately does not:

    1. A whole-phrase echo -- "So am I", "You're right", "Fair enough".
    2. A reply that is *nothing but* acknowledgement once the particles
       are stripped -- "Exactly.", "Oh, right.", "Yeah."
    3. A clause whose subject is a demonstrative pointing at his
       sentence -- "That makes sense", "Then those are yours."

    What does not count: particles in front of her own subject. "But I
    finished the book" opens on a conjunction and still leads, because
    the content after the hinge is hers. This is a rate detector for a
    grammatical habit, not a ban on connectives -- the occasional "Then
    those pokes are reserved for you" is warm, and a hard prohibition
    would cost the warmth that makes her worth talking to.

    Leading interjections are skipped across sentence boundaries too,
    not just commas. "Mm. I will. Sleep well" and "Mm, I will" are the
    same move, and a detector that called one of them following because
    of the punctuation would spend its firing budget telling her to stop
    making warm noises.
    """
    acknowledged = False
    for sentence in sentences(text):
        tokens = _WORD_RE.findall(sentence.lower())
        if not tokens:
            continue

        joined = " ".join(tokens)
        for phrase in ECHO_OPENERS:
            if joined == phrase or joined.startswith(phrase + " "):
                return True

        index = 0
        while index < len(tokens) and tokens[index] in PARTICLES:
            index += 1
        rest = tokens[index:]
        if not rest:
            # Pure acknowledgement. Keep looking: if she went on to say
            # something of her own, that is the clause to judge.
            acknowledged = True
            continue
        # Split off a contraction clitic so "that's settled" is
        # recognised as the same subject as "that was settled".
        return rest[0].split("'", 1)[0] in ANAPHORIC_SUBJECTS

    # Nothing but acknowledgement: a reply that is only "Exactly." is
    # following by definition. Text with no words at all ("...") is not
    # -- there is no clause to be dependent.
    return acknowledged


__all__ = [
    "ANAPHORIC_SUBJECTS",
    "ECHO_OPENERS",
    "PARTICLES",
    "SENT_SPLIT_RE",
    "first_sentence",
    "is_anaphoric_opener",
    "sentences",
]

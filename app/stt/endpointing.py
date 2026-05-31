"""Tiered voice-endpointing helpers.

The live mic loop can be naively endpointed on a single silence threshold,
but English-as-a-second-language users (and anyone else who pauses while
searching for the next word) get half-thoughts committed to the LLM. The
helpers in this module let the capture loop ask three questions at every
silence boundary:

1. Has the user been silent long enough that we *might* be done?
2. Does the partial transcript look like a *finished* sentence (close fast)?
3. Does it look like a *hesitation* — trailing "um", "and", "you know"?
   (extend capture; reset the silence counter so the user has the full
   ``turn_silence_seconds`` window to find the next word)

The decision is then passed back to :func:`app.audio.mic_capture._capture_loop`
which either breaks out (commit) or keeps reading chunks (wait/extend).

Pure-Python, no STT/audio dependencies — easy to unit test and cheap to
call: a single regex match per silence boundary, never per chunk.
"""
from __future__ import annotations

import re
from typing import Iterable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.infra.settings import EndpointingSettings


__all__ = [
    "EndpointDecision",
    "is_hesitation_marker",
    "is_sentence_final",
    "decide",
]


EndpointDecision = Literal["wait", "extend", "commit"]
"""Outcomes returned to the capture loop:

- ``"wait"`` — keep capturing chunks; do **not** reset the silence
  counter. The loop's own ``silence_chunks_to_stop`` (hard cap) fires
  naturally at the turn boundary.
- ``"extend"`` — keep capturing **and** reset the silence counter. Used
  when a hesitation marker is detected at the phrase boundary so the
  user gets a fresh window to find the next word.
- ``"commit"`` — break out of the capture loop now. Used for the fast
  close (sentence-final partial at ``fast_close_silence_seconds``) and
  for the hard turn boundary (``turn_silence_seconds``).
"""


# Trailing words / particles that strongly suggest the user is mid-thought
# and just searching for the next word. Anchored at end-of-string with
# optional trailing punctuation/whitespace.
_HESITATION_PATTERN = re.compile(
    r"\b("
    r"um+|uh+|hmm+|er+|ah+|eh+|"
    r"and|so|but|or|because|cause|"
    r"like|maybe|perhaps|"
    r"i\s+mean|i\s+think|i\s+guess|"
    r"you\s+know|let\s+me\s+think|"
    r"kind\s+of|sort\s+of|"
    r"how\s+can\s+i\s+say|how\s+do\s+i\s+say|"
    r"what(?:'s|\s+is)\s+the\s+word"
    r")\b\s*[.,…\-]?\s*$",
    re.IGNORECASE,
)

# Patterns that suggest the user is clearly *done* — closer phrases or a
# sentence-final punctuation mark. Triggers fast-close at the short tier.
_SENTENCE_FINAL_PUNCT = re.compile(r"[.?!](?:['\"]+)?\s*$")
_SENTENCE_FINAL_CLOSER = re.compile(
    r"\b("
    r"thanks|thank\s+you|"
    r"right|"
    r"okay|ok|"
    r"got\s+it|"
    r"that(?:'s|\s+is)\s+(?:all|it|everything)|"
    r"that\s+would\s+be\s+all"
    r")\.?\s*$",
    re.IGNORECASE,
)


def _last_segment(text: str) -> str:
    """Return the last clause-ish segment of ``text`` for matching.

    We match against roughly the last ~80 chars so a long preamble doesn't
    accidentally hit "and" early in the sentence. The regexes are anchored
    to end-of-string so the trailing tokens are what actually matters.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > 80:
        cleaned = cleaned[-80:]
    return cleaned


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            # Bad user-supplied pattern — ignore rather than crash the loop.
            continue
    return False


def is_hesitation_marker(text: str, *, extra_patterns: Iterable[str] = ()) -> bool:
    """Does ``text`` look like the user is mid-thought / searching for a word?

    True when the trailing tokens match a hesitation marker (``"um"``,
    ``"and..."``, ``"you know"``…). ``extra_patterns`` is consulted before
    the built-in list so users can extend it via config.
    """
    segment = _last_segment(text)
    if not segment:
        return False
    if extra_patterns and _matches_any(segment, extra_patterns):
        return True
    return bool(_HESITATION_PATTERN.search(segment))


def is_sentence_final(text: str, *, extra_patterns: Iterable[str] = ()) -> bool:
    """Does ``text`` look like a finished sentence we can commit fast?

    True when the trailing tokens match a sentence-final marker (``"."``,
    ``"thanks."``, ``"okay"``…). ``extra_patterns`` is consulted before
    the built-in list.
    """
    segment = _last_segment(text)
    if not segment:
        return False
    if extra_patterns and _matches_any(segment, extra_patterns):
        return True
    if _SENTENCE_FINAL_PUNCT.search(segment):
        return True
    return bool(_SENTENCE_FINAL_CLOSER.search(segment))


def decide(
    silence_seconds: float,
    partial: str,
    settings: "EndpointingSettings",
) -> EndpointDecision:
    """Tiered endpointing decision based on silence elapsed + partial text.

    Implements the table from the plan:

    +-------------------------+------------------+-----------------+
    | silence elapsed         | partial signal   | decision        |
    +-------------------------+------------------+-----------------+
    | < phrase_silence        | --               | wait            |
    | >= fast_close (and < phrase) | sentence-final | commit       |
    | >= phrase_silence       | sentence-final   | commit          |
    | >= phrase_silence       | hesitation       | extend          |
    | >= phrase_silence       | ambiguous        | wait            |
    | >= turn_silence         | --               | commit          |
    +-------------------------+------------------+-----------------+

    All thresholds come from ``settings`` so the loop has a single source
    of truth. When ``settings.enabled`` is ``False`` we always return
    ``"wait"`` so the caller's normal ``silence_chunks_to_stop`` is the
    only gate (legacy behaviour).
    """
    if not getattr(settings, "enabled", True):
        return "wait"

    sil = float(silence_seconds)
    fast = float(getattr(settings, "fast_close_silence_seconds", 0.6))
    phrase = float(getattr(settings, "phrase_silence_seconds", 1.0))
    turn = float(getattr(settings, "turn_silence_seconds", 3.0))
    use_partial = bool(getattr(settings, "use_partial_transcript", True))
    extend_on_hesitation = bool(getattr(settings, "hesitation_extend_to_turn", True))
    extra_hes = tuple(getattr(settings, "hesitation_markers", ()) or ())
    extra_fin = tuple(getattr(settings, "sentence_final_markers", ()) or ())

    # Hard cap: turn boundary always commits, no matter what.
    if sil >= turn:
        return "commit"

    # Fast close: only when we have a partial that *clearly* looks done.
    # Active anywhere between fast_close and turn.
    if use_partial and sil >= fast and is_sentence_final(partial, extra_patterns=extra_fin):
        return "commit"

    # Phrase boundary: the interesting tier.
    if sil >= phrase:
        if not use_partial:
            # Two-tier without lexical: ambiguous case waits until the
            # hard cap, mirroring the lexical "ambiguous" branch below.
            return "wait"
        if extend_on_hesitation and is_hesitation_marker(
            partial, extra_patterns=extra_hes
        ):
            return "extend"
        # Ambiguous partial → fall through; the loop's hard cap at
        # turn_silence_seconds will fire eventually.
        return "wait"

    return "wait"

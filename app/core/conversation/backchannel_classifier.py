"""Regex-only classifier for STT-partial backchannel cues.

Runs on the hot path during ``stt_partial`` events (every ~200ms while the
user is speaking). The output drives a small avatar reaction so Aiko looks
like she's actively listening — a nod, a tilted head, a smile — instead
of staring blankly until the user finishes.

ZERO LLM calls — pure regex. Cost target: <1ms per call. The classifier
is intentionally conservative: it returns ``None`` for ambiguous text
rather than guessing, because a wrong reaction (smiling at "I'm sad") is
worse than no reaction.

Adding a new hint type:
  1. Append to :data:`BackchannelHint`.
  2. Add a (compiled regex, hint) pair to ``_PATTERNS`` below.
  3. Order matters: the first match wins, so put high-confidence narrow
     patterns first (e.g. "haha" before generic "good").
"""
from __future__ import annotations

import re
from typing import Literal


BackchannelHint = Literal[
    "agreement",
    "disagreement",
    "surprise",
    "amusement",
    "concern",
    "confused",
    "thinking",
]


# Ordered list of (pattern, hint). The classifier returns the hint for the
# first pattern that matches the *most-recent* fragment of the partial.
# Patterns use word boundaries so "agreement" doesn't fire on "magnetic".
_PATTERNS: tuple[tuple[re.Pattern[str], BackchannelHint], ...] = (
    # Amusement (very high confidence — laughter is unambiguous).
    (re.compile(r"\b(?:ha){2,}\b", re.IGNORECASE), "amusement"),
    (re.compile(r"\b(?:he){2,}\b", re.IGNORECASE), "amusement"),
    (re.compile(r"\b(?:lol|lmao|rofl)\b", re.IGNORECASE), "amusement"),
    (re.compile(r"\b(?:that'?s funny|hilarious)\b", re.IGNORECASE), "amusement"),
    # Disagreement BEFORE surprise: "not really" must beat "really".
    (re.compile(r"\b(?:i don'?t (?:think|agree|believe))\b", re.IGNORECASE), "disagreement"),
    (re.compile(r"\b(?:not really|nope+|nah|that'?s wrong|incorrect)\b", re.IGNORECASE), "disagreement"),
    # Surprise (interjections + clear lexical markers).
    (re.compile(r"\b(?:wow|whoa+|woah|oh my|holy cow|no way)\b", re.IGNORECASE), "surprise"),
    (re.compile(r"\b(?:really\??|are you serious|seriously\??)\b", re.IGNORECASE), "surprise"),
    # Agreement.
    (re.compile(r"\b(?:exactly|precisely|absolutely|for sure|totally)\b", re.IGNORECASE), "agreement"),
    (re.compile(r"\b(?:yeah|yep+|yup|right|that'?s right|true|correct)\b", re.IGNORECASE), "agreement"),
    (re.compile(r"\b(?:i agree|i think so too|same)\b", re.IGNORECASE), "agreement"),
    # Concern.
    (re.compile(r"\b(?:worried|stressed|tired|exhausted|sad|upset|frustrated|annoyed)\b", re.IGNORECASE), "concern"),
    (re.compile(r"\b(?:that'?s (?:terrible|awful|bad)|i'?m sorry)\b", re.IGNORECASE), "concern"),
    # Confused.
    (re.compile(r"\b(?:wait,? what|i don'?t (?:understand|get it)|confused|huh\??)\b", re.IGNORECASE), "confused"),
    (re.compile(r"\b(?:what do you mean|how come|why)\b", re.IGNORECASE), "confused"),
    # Thinking (explicit verbal stalls — rare but useful).
    (re.compile(r"\b(?:hmm+|let me think|let'?s see|thinking)\b", re.IGNORECASE), "thinking"),
    (re.compile(r"\b(?:um+|uh+|er+|ah+)\b", re.IGNORECASE), "thinking"),
)


# How many trailing characters of the partial we look at. Anything more is
# probably already-classified text and risks re-firing a hint when the user
# pauses but hasn't moved past the keyword.
_RECENT_TAIL_CHARS = 60


def classify(partial: str) -> BackchannelHint | None:
    """Return a hint for the most recent fragment of ``partial``, or ``None``.

    Examines only the trailing ~60 chars so a long partial doesn't trigger
    a hint based on something said 5 seconds ago. Returns ``None`` for any
    fragment that doesn't match a known pattern; the caller treats ``None``
    as "no avatar reaction needed".
    """
    if not partial:
        return None
    text = partial[-_RECENT_TAIL_CHARS:].strip()
    if not text:
        return None
    for pattern, hint in _PATTERNS:
        if pattern.search(text):
            return hint
    return None


class BackchannelGate:
    """Stateful wrapper that rate-limits hints emitted from a session.

    The avatar overlay picks up every ``backchannel`` WS event, so we want
    to avoid spamming the same hint while the user is mid-word. The gate
    tracks the last hint and a min-interval; only emit a new event when:
      * the hint differs from the previous one, OR
      * the same hint hasn't been emitted in ``min_repeat_seconds``.
    """

    def __init__(self, *, min_repeat_seconds: float = 1.5) -> None:
        self._min_repeat = max(0.1, float(min_repeat_seconds))
        self._last_hint: BackchannelHint | None = None
        self._last_hint_at: float = 0.0

    def consider(self, partial: str, *, now: float) -> BackchannelHint | None:
        hint = classify(partial)
        if hint is None:
            return None
        if hint == self._last_hint and (now - self._last_hint_at) < self._min_repeat:
            return None
        self._last_hint = hint
        self._last_hint_at = now
        return hint

    def reset(self) -> None:
        self._last_hint = None
        self._last_hint_at = 0.0

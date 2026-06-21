"""Quality guard for proactive lines before they are spoken / written.

Proactive lines drawn from inner-life memories (callbacks, open
questions, reflections, promises) start life as **third-person memory
text** — ``"Notices that he warms up after coffee"``, ``"Jacob
promised: ..."``. The :class:`NarrativeWeaver` normally rewrites these
into Aiko's first-person voice, but when the weave LLM is unavailable /
raises / returns empty the old template fallback substituted the raw
narration verbatim, leaking it into a spoken line ("I was just sitting
with Notices that he warms up after coffee").

:func:`validate_proactive_line` is the shared, dependency-free backstop:
the weaver rejects bad weave / fallback output before it's stored, and
:class:`ProactiveDirector` re-checks at speak time and degrades to its
safe LLM turn on a reject. The rules are deliberately **conservative** —
they pass normal first-person weave output and only reject text that
clearly reads as raw narration or carries an internal marker.
"""
from __future__ import annotations

import re

# Hard ceiling — a proactive line is one or two sentences. Anything
# longer is either a leaked paragraph or a runaway generation.
_MAX_LEN = 280

# Internal markers / phrasings that should never reach a spoken line.
# ``the user`` is included because a real proactive line addresses the
# user by name (or "you"), never the third-person research label.
_BANNED_SUBSTRINGS: tuple[str, ...] = (
    "[[",
    "]]",
    "promised:",
    "source content",
    "source kind",
    "the user",
)

# Third-person narration verbs that a genuine first-person opener never
# starts with. Memory observations routinely lead with these
# ("Wonders if ...", "Notices that ..."), so a line starting here is a
# verbatim-leak tell.
_NARRATION_OPENERS: frozenset[str] = frozenset({
    "wonders",
    "wondering",
    "notices",
    "noticing",
    "thinks",
    "thinking",
    "realizes",
    "realizing",
    "considers",
    "considering",
    "recalls",
    "recalling",
    "remembers",
    "remembering",
    "mentions",
    "mentioned",
    "seems",
    "appears",
})

# Third-person verbs that, when they directly follow the user's display
# name at the start of a line, mark a leaked third-person sentence
# ("Jacob is ...", "Jacob wants ..."). A vocative ("Jacob, ...") is fine
# and handled separately by the trailing-comma check.
_THIRD_PERSON_VERBS: frozenset[str] = frozenset({
    "is",
    "was",
    "has",
    "had",
    "wants",
    "wanted",
    "plans",
    "planned",
    "needs",
    "needed",
    "hopes",
    "hoped",
    "loves",
    "likes",
    "said",
    "says",
    "mentioned",
    "wonders",
    "notices",
    "thinks",
    "feels",
    "seems",
})

_LEADING_PUNCT_RE = re.compile(r"^[\"'`\s\-–—:;,.]+")
_FIRST_WORD_RE = re.compile(r"[A-Za-z']+")


def _first_two_words(text: str) -> tuple[str, str]:
    """Return the first two alphabetic words (lowercased) of ``text``."""
    stripped = _LEADING_PUNCT_RE.sub("", text)
    words = _FIRST_WORD_RE.findall(stripped)
    first = words[0].lower() if words else ""
    second = words[1].lower() if len(words) > 1 else ""
    return first, second


def validate_proactive_line(
    text: str, *, user_display_name: str = "the user",
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a candidate proactive line.

    ``ok`` is ``True`` when the line is safe to speak. On a reject,
    ``reason`` is a short greppable token (``empty`` / ``too_long`` /
    ``multiline`` / ``banned:<substr>`` / ``narration_opener:<word>`` /
    ``third_person_subject``) so the caller can log *why* without
    re-deriving it.
    """
    raw = (text or "").strip()
    if not raw:
        return False, "empty"
    if "\n" in raw:
        return False, "multiline"
    if len(raw) > _MAX_LEN:
        return False, "too_long"

    low = raw.lower()
    for banned in _BANNED_SUBSTRINGS:
        if banned in low:
            return False, f"banned:{banned}"

    first, second = _first_two_words(raw)
    if first in _NARRATION_OPENERS:
        return False, f"narration_opener:{first}"

    name = (user_display_name or "").strip().lower()
    if name and first == name:
        # "Jacob, ..." (vocative) is fine; the leak shape is
        # "Jacob <3rd-person-verb> ..." with no comma after the name.
        after_name = _LEADING_PUNCT_RE.sub("", raw)[len(name):].lstrip()
        if not after_name.startswith(",") and second in _THIRD_PERSON_VERBS:
            return False, "third_person_subject"

    return True, "ok"


__all__ = ["validate_proactive_line"]

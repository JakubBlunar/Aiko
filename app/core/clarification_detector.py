"""Clarification-repair detection (K17).

Per-turn regex classifier that fires when the user signals Aiko
*missed the point* on her previous reply. The signal is one-shot:
the post-turn flow stashes a :class:`ClarificationResult` and the
inner-life provider consumes it on the very next turn so Aiko
re-reads, acknowledges plainly that she missed it, and moves on
without an apology spiral.

Two bands, ordered by strength:

* ``strong`` — explicit "no that's not what I meant" / "you
  misunderstood" / "I meant X not Y" / "wait no". The user is
  visibly correcting; Aiko should re-read the last two messages
  and own it.
* ``mild`` — softer confusion ("huh?", "wait what", "I don't follow",
  "what do you mean"). Could also fire on a curt one-word "no"
  immediately after Aiko's reply when the dialogue act suggests
  contradiction. Aiko should pause once and check before charging
  ahead.

Design notes:

* Regex hot path only. No LLM cold path here -- the false-positive
  cost is low (one extra "wait, did I miss something?" beat) and
  the latency budget on the post-turn flow is tight. If we ever
  see drift, we can add an LLM upgrade the same way
  :mod:`dialogue_act_tagger` does.
* Order matters: ``strong`` patterns are checked before ``mild`` so
  a message like "no, I meant the *other* one" reads as a strong
  correction, not a curt no.
* The detector is stateless. The post-turn flow handles the one-shot
  consumption contract by stashing the result on the
  ``SessionController`` and clearing it after the next turn renders.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass


log = logging.getLogger("app.clarification_detector")


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClarificationResult:
    """One per-turn clarification-repair signal."""

    band: str  # "strong" | "mild"
    evidence: str  # the matched phrase, trimmed; passed to the LLM cue


# ── regex patterns ───────────────────────────────────────────────────────


# Strong: explicit corrections / repudiations of Aiko's last reply.
# These read as "you got it wrong" -- the user is visibly steering.
_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "no that's not what I meant" / "that's not what I meant"
    re.compile(
        r"\b(?:no[, ]+)?(?:that'?s|thats)\s+not\s+what\s+i\s+meant\b",
        re.IGNORECASE,
    ),
    # "you misunderstood" / "you're misunderstanding"
    re.compile(
        r"\byou(?:'?re)?\s+(?:mis(?:understanding|understood|reading|read))\b",
        re.IGNORECASE,
    ),
    # "you got the wrong" / "you're getting the wrong"
    re.compile(
        r"\byou(?:'?re)?\s+(?:getting|got)\s+(?:the|this)\s+wrong\b",
        re.IGNORECASE,
    ),
    # "I meant X not Y" / "I meant X, not Y"
    re.compile(
        r"\bi\s+meant\b[^.?!\n]{1,80}?\bnot\b",
        re.IGNORECASE,
    ),
    # "no I'm asking about X" / "no, I'm asking..."
    re.compile(
        r"\bno[, ]+i'?m\s+asking\b",
        re.IGNORECASE,
    ),
    # "wait no" / "no wait" -- explicit course-correction
    re.compile(
        r"\b(?:wait[, ]+no|no[, ]+wait)\b",
        re.IGNORECASE,
    ),
    # "that's not it" / "that isn't it"
    re.compile(
        r"\b(?:that'?s|thats|that)\s+(?:not|isn'?t)\s+it\b",
        re.IGNORECASE,
    ),
    # "you're missing the point" / "missing my point"
    re.compile(
        r"\b(?:you(?:'?re)?\s+)?missing\s+(?:the|my)\s+point\b",
        re.IGNORECASE,
    ),
)


# Mild: softer confusion / "I don't follow" -- not an explicit
# correction, just a checkpoint that Aiko should pause and re-read.
_MILD_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "huh?" / "huh??" -- isolated, not "uh huh"
    re.compile(
        r"(?:^|[^a-z])huh\?+",
        re.IGNORECASE,
    ),
    # "wait what" / "wait, what?"
    re.compile(
        r"\bwait[, ]+what\b",
        re.IGNORECASE,
    ),
    # "what do you mean" / "what do you mean by"
    re.compile(
        r"\bwhat\s+do\s+you\s+mean\b",
        re.IGNORECASE,
    ),
    # "I don't follow" / "I dont follow"
    re.compile(
        r"\bi\s+do(?:n'?t|nt)\s+follow\b",
        re.IGNORECASE,
    ),
    # "I'm confused" / "im confused" (when the user is asking, not
    # when they're describing being confused about a third-party
    # situation -- the false-positive cost here is a soft re-read,
    # which is acceptable).
    re.compile(
        r"\bi'?m\s+confused\b",
        re.IGNORECASE,
    ),
    # "that doesn't make sense" / "this doesn't make sense"
    re.compile(
        r"\b(?:that|this|it)\s+do(?:esn'?t|esnt)\s+make\s+sense\b",
        re.IGNORECASE,
    ),
)


# Cap on how much surrounding text we capture as "evidence" for the
# inner-life cue. Long enough to disambiguate, short enough that it
# doesn't blow up the system prompt.
_EVIDENCE_MAX_CHARS: int = 80


# ── Public API ───────────────────────────────────────────────────────────


def detect(user_text: str) -> ClarificationResult | None:
    """Classify ``user_text`` and return a :class:`ClarificationResult`
    when it reads as a clarification-repair beat, or ``None``
    otherwise.

    Empty / whitespace-only input always returns ``None``. Strong
    patterns win over mild patterns when both match (e.g. "no
    that's not what I meant, I'm confused" reads as strong, not
    mild).
    """
    text = (user_text or "").strip()
    if not text:
        return None

    for pattern in _STRONG_PATTERNS:
        m = pattern.search(text)
        if m is not None:
            return ClarificationResult(
                band="strong",
                evidence=_format_evidence(text, m),
            )

    for pattern in _MILD_PATTERNS:
        m = pattern.search(text)
        if m is not None:
            return ClarificationResult(
                band="mild",
                evidence=_format_evidence(text, m),
            )

    return None


def render_inner_life_block(
    result: ClarificationResult,
    *,
    user_display_name: str = "the user",
) -> str:
    """Render ``result`` into a system-prompt-ready block.

    Two bands map to two voicings -- the strong one is direct
    ("you missed it, re-read"), the mild one is softer ("they're
    confused, slow down once before continuing"). Both end with a
    "don't apologise repeatedly" rail to head off the standard
    LLM failure mode.
    """
    if result.band == "strong":
        head = (
            f"Heads-up: {user_display_name} just signalled you missed his "
            "last point"
        )
    else:
        head = (
            f"Heads-up: {user_display_name} just sounded confused or "
            "off-balance after your last reply"
        )
    evidence = (result.evidence or "").strip()
    if evidence:
        head = f'{head} ("{evidence}")'
    if result.band == "strong":
        body = (
            "Re-read his last two messages, acknowledge once that you "
            "missed it (no apology spiral), then answer what he "
            "actually asked. Don't restate your prior reply."
        )
    else:
        body = (
            "Pause once, check what part landed wrong before charging "
            "ahead. A single soft \"wait -- did I lose you?\" or a "
            "quick rephrase is enough; don't apologise repeatedly."
        )
    return f"{head}.\n{body}"


# ── helpers ──────────────────────────────────────────────────────────────


def _format_evidence(text: str, match: re.Match[str]) -> str:
    """Return a trimmed slice of ``text`` centred on ``match``.

    Used as a one-line "what was the trigger" hint for the LLM cue.
    Trimmed to :data:`_EVIDENCE_MAX_CHARS` so a long user turn
    doesn't bloat the system prompt.
    """
    matched = (match.group(0) or "").strip()
    if not matched:
        return text[:_EVIDENCE_MAX_CHARS].strip()
    if len(matched) <= _EVIDENCE_MAX_CHARS:
        return matched
    return matched[:_EVIDENCE_MAX_CHARS].rstrip() + "…"

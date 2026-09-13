"""Lexical gate for Live micro-utterances.

A failed validator is ``noop``/rejection. It never escalates to the
main model and never retries in a loop.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.services.response_text_service import strip_all_meta_tags

MICRO_INTENTS = frozenset({
    "acknowledge_user",
    "react_affectively",
    "backchannel_user",
})
MICRO_DELIVERY = "micro_utterance"
MAX_WORDS = 8
MAX_CHARS = 80

_URL_RE = re.compile(r"(https?://|www\.|\S+@\S+)", re.I)
_DIGIT_RE = re.compile(r"\d")
_BANNED = (
    "you should",
    "you need",
    "you must",
    "you feel",
    "you're feeling",
    "you think",
    "i remember",
    "let me",
    "look up",
    "search for",
    "don't forget",
    "do not forget",
)


def proposed_micro_text(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    delivery = str(arguments.get("delivery") or "").strip()
    if delivery != MICRO_DELIVERY:
        return ""
    return str(arguments.get("text") or "")


def validate_micro_utterance(raw: Any) -> str | None:
    """Return cleaned spoken text, or ``None`` if the line is illegal."""
    text = strip_all_meta_tags(str(raw or ""))
    text = " ".join(text.replace("\n", " ").split())
    if not text or len(text) > MAX_CHARS:
        return None
    if "?" in text or _URL_RE.search(text) or _DIGIT_RE.search(text):
        return None
    lowered = text.lower()
    if any(token in lowered for token in _BANNED):
        return None
    words = text.split()
    if not words or len(words) > MAX_WORDS:
        return None
    return text


__all__ = [
    "MAX_CHARS",
    "MAX_WORDS",
    "MICRO_DELIVERY",
    "MICRO_INTENTS",
    "proposed_micro_text",
    "validate_micro_utterance",
]

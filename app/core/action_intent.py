"""Lightweight action-intent detection for orchestration and goal inference."""
from __future__ import annotations

import re


def has_action_intent(user_text: str) -> bool:
    """Return True if user_text appears to request a UI/desktop action (click, open, minimize, etc.)."""
    normalized = (user_text or "").strip().lower()
    if not normalized:
        return False
    triggers = (
        "click",
        "press",
        "tap",
        "type",
        "write",
        "fill",
        "open",
        "launch",
        "start",
        "select",
        "choose",
        "submit",
        "send",
        "scroll",
        "focus",
        "activate",
        "switch to",
        "switch",
        "bring",
        "move",
        "drag",
        "drop",
        "minimize",
        "minimise",
        "maximize",
        "restore",
        "close",
        "show",
        "hide",
        "resize",
        "arrange",
        "make active",
        "bring to front",
        "read on screen",
        "read the article",
    )
    if any(token in normalized for token in triggers):
        return True

    return bool(
        re.search(
            r"\b(can you|could you|please|would you)\b.*\b(minimi[sz]e|maximi[sz]e|restore|focus|switch|open|close|activate|show|hide|resize|arrange)\b",
            normalized,
        )
        or re.search(
            r"\b(make|set)\b.*\b(active|foreground|front)\b",
            normalized,
        )
    )

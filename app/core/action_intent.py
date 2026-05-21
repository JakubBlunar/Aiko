"""Lightweight action-intent detection for orchestration and goal inference."""
from __future__ import annotations

import re

# Concrete, unambiguous indicators that a user turn requires the agent to
# actually call a tool. Matches are conservative on purpose: we want zero
# false positives on conversational filler ("sure", "ok", "yeah"). Goal
# inference uses the broader ``has_action_intent`` below.
_EXPLICIT_TOOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(search|google|look\s+up|find\s+(online|on the web))\b"),
    re.compile(r"\b(open|navigate\s+to|go\s+to|browse\s+to)\s+(http|https|www\.|\S+\.(com|org|net|io|dev|gg|app|ai))\b"),
    re.compile(r"\b(play|watch|listen\s+to)\s+.+\b(on\s+youtube|on\s+spotify|on\s+twitch|on\s+netflix)\b"),
    re.compile(r"\b(click|tap|fill\s+in|type\s+(in|into)|press)\b.*\b(button|field|link|page|input)\b"),
    re.compile(r"\b(remember\s+(that|when|how|to)|save\s+(this|that|the\s+\w+)|note\s+that)\b"),
    re.compile(r"\b(take\s+a\s+screenshot|capture\s+the\s+(screen|page))\b"),
    re.compile(r"\b(read\s+(the\s+)?(article|page|screen|tab))\b"),
    re.compile(r"\b(scroll\s+(down|up|to))\b"),
    re.compile(r"\b(what(\s+do\s+you|'s)\s+(remember|recall)|search\s+(my\s+)?(history|memory|archive))\b"),
)


def has_explicit_tool_request(user_text: str) -> bool:
    """Return True only when the user clearly asked the agent to use a tool.

    Used by the agent controller to decide whether to expose tools at all on
    this turn. Designed to be conservative: short agreement words ("sure",
    "ok", "yeah"), reactions, opinions, questions, and small talk must NOT
    match. False negatives are acceptable (the judge LLM is the safety net);
    false positives are not.
    """
    normalized = (user_text or "").strip().lower()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _EXPLICIT_TOOL_PATTERNS)


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

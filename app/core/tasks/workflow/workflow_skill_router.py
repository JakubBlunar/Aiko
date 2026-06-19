"""Worker-lane skill router — narrows the planner's menu by goal group.

The [`GoalWorkflowHandler`] hands the [`workflow_planner`] the full skill
catalogue every iteration (`describe_for_planner()`). As the catalogue
grows (more built-ins, several MCP servers each advertising tools), that
menu bloats and the planner's "which skill?" decision degrades — exactly
the brain-lane problem the P14 gate solves, one lane over.

This module is the worker-lane analogue of
[`tool_pass_gate.select_active_tool_names`]: a pure, embedding-free
keyword classifier that maps the workflow GOAL text to the relevant skill
*group(s)* so the handler can pass only those to the planner.

Conservative by construction (same contract as the brain gate): the
narrowing is an optimization, never a correctness gate. The planner's
``missing_capability`` outcome is the canary for over-narrowing, so any
ambiguity widens to the full menu:

* **zero** group matched -> ``None`` (full menu — let the planner see all),
* **one** group matched (and present) -> narrow to that single group,
* **multiple** groups matched -> ``None`` (the goal spans groups; don't
  guess which to drop).

Built-in groups (``files`` / ``web`` / ``vision``) are keyword-classified.
Per-server MCP groups (``mcp:<server_id>``) match only when the goal names
the server id token — otherwise an MCP-needing goal that we can't classify
correctly falls into the "zero match -> full menu" safe path.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger("app.tasks.workflow.skill_router")


def _compile(words: Iterable[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", flags=re.IGNORECASE)


# Per-built-in-group keyword patterns (generous; a match only narrows to
# a strictly-larger-or-equal-quality plan, never hides the terminal skill).
_GROUP_PATTERNS: dict[str, re.Pattern[str]] = {
    "files": _compile([
        r"files?", r"folders?", r"director(?:y|ies)", r"documents?",
        r"notes?", r"\.md", r"\.txt", r"\.pdf", r"\.docx?", r"\.csv",
        r"spreadsheets?", r"path", r"drive", r"downloads", r"desktop",
        r"read", r"write", r"save", r"append", r"edit",
    ]),
    "web": _compile([
        r"search", r"google", r"web", r"online", r"internet", r"news",
        r"headlines?", r"weather", r"forecast", r"latest", r"price",
        r"prices", r"stock", r"score", r"release date", r"look up",
        r"url", r"website", r"browse",
    ]),
    "vision": _compile([
        r"images?", r"pictures?", r"photos?", r"screenshots?",
        r"\.png", r"\.jpe?g", r"\.gif", r"\.webp", r"diagrams?",
        r"what'?s in (?:this|the|that) (?:image|picture|photo|screenshot)",
        r"describe (?:this|the|that) (?:image|picture|photo|screenshot)",
    ]),
}


def select_skill_groups(
    goal_text: str,
    available_groups: Iterable[str],
) -> "set[str] | None":
    """Pick the skill group(s) to expose for ``goal_text``, or ``None``.

    Returns ``None`` (= send the full menu) on zero or multiple matches;
    a single-element set when exactly one available group matches. Only
    groups present in ``available_groups`` are considered, so a built-in
    group with no registered skills (e.g. ``vision`` when vision is
    disabled) can never be selected.
    """
    text = (goal_text or "").strip()
    available = {str(g) for g in available_groups if str(g)}
    if not text or not available:
        return None

    matched: set[str] = set()
    # Built-in keyword groups.
    for group, pattern in _GROUP_PATTERNS.items():
        if group in available and pattern.search(text):
            matched.add(group)
    # Per-server MCP groups: match only when the server id token appears.
    for group in available:
        if not group.startswith("mcp:"):
            continue
        server_id = group[len("mcp:"):]
        if server_id and re.search(
            r"\b" + re.escape(server_id) + r"\b", text, flags=re.IGNORECASE
        ):
            matched.add(group)

    if len(matched) == 1:
        chosen = set(matched)
        log.debug(
            "workflow skill-router: narrowed groups=%s (goal=%.60s)",
            sorted(chosen),
            text,
        )
        return chosen
    # Zero or multiple -> widen to the full menu (conservative).
    log.debug(
        "workflow skill-router: full menu (matched=%s available=%s)",
        sorted(matched),
        sorted(available),
    )
    return None


__all__ = ["select_skill_groups"]

"""P14 — heuristic gate in front of the forced tool-decision pass.

``TurnRunner._maybe_run_tool_pass`` costs a full non-streaming
``chat_with_tools`` round-trip on *every* turn when any tools are
registered, even though the most common outcome on banter turns is the
model picking the ``respond_directly`` escape tool — i.e. we paid an
LLM call to learn there was nothing to do. This module decides, with a
pure embedding-free heuristic, whether the pass needs to run at all.

Design contract (conservative by construction):

* A *false positive* (gate says run, pass picks ``respond_directly``)
  costs exactly the status quo — nothing regresses.
* A *false negative* (gate says skip, the user actually wanted a tool)
  is the real risk, so every ambiguity resolves toward **run**:
  continuity signals (finished-task block, active tasks, the previous
  turn dispatched a tool), an armed force flag, or any registered tool
  whose name we don't have a pattern family for, all bypass the text
  heuristic entirely.
* Signal patterns are *per tool family* and only consulted for families
  that actually have registered tools this turn — disabling
  ``tools.world`` in config deactivates the room/garden patterns.

The gate is intentionally generous with its regexes: matching a chatty
sentence that merely *sounds* tool-shaped only re-runs today's
behaviour. The win comes from the large share of turns with no
tool-shaped token at all.

Escalation ladder (documented in ``docs/personality-backlog/shipped.md``):
if real-world tool recall regresses, the next levers are option D
(route the decision pass to ``routes.worker_default``) and option B
(speculative parallel stream) — NOT loosening this gate further.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger("app.core.session.tool_pass_gate")


@dataclass(frozen=True, slots=True)
class GateContext:
    """Per-turn continuity signals the gate must respect.

    ``finished_task_block`` — the assembled system prompt carries a
    finished-task cue (the pass must run so today's ``tool_choice="auto"``
    relaxation path stays intact).
    ``last_turn_dispatched_tool`` — the previous turn dispatched at least
    one real tool; follow-ups like "and the other folder?" carry no
    tool-shaped token of their own.
    ``tasks_active`` — at least one task is ``running`` /
    ``awaiting_input`` / ``paused``; the user's message may be the answer
    a pending ``answer_file_task`` is waiting for.
    ``force`` — one-shot MCP bypass (``force_tool_pass``).
    """

    finished_task_block: bool = False
    last_turn_dispatched_tool: bool = False
    tasks_active: bool = False
    force: bool = False


@dataclass(frozen=True, slots=True)
class GateDecision:
    run: bool
    reason: str
    matched: tuple[str, ...] = ()

    def as_event(self) -> str:
        """Compact ``run:<reason>`` / ``skip:<reason>`` string for the
        ``turn done:`` log line and MCP telemetry."""
        return f"{'run' if self.run else 'skip'}:{self.reason}"


# ── tool-name → family mapping ────────────────────────────────────────
# Every live tool must appear here. A registered tool whose name is NOT
# in this map makes the gate always run (``reason="unknown_tool"``) so a
# future tool added without a pattern family degrades to the status quo
# instead of silently never being callable.
_TOOL_FAMILY: dict[str, str] = {
    # builtins
    "get_time": "time",
    "web_search": "web",
    "recall": "recall",
    # file tasks
    "list_file_roots": "files",
    "start_file_read": "files",
    "start_file_search": "files",
    "cancel_file_task": "files",
    "answer_file_task": "files",
    # world / room / garden
    "look_around": "world",
    "move_to": "world",
    "change_posture": "world",
    "inspect_item": "world",
    "consume_item": "world",
    "water_plant": "world",
    "plant_seed": "world",
    "harvest_plant": "world",
    # goals
    "add_goal": "goals",
    "update_goal_progress": "goals",
    "archive_goal": "goals",
    "list_goals": "goals",
    # workflows / brain tasks
    "start_workflow": "tasks",
    "check_my_work": "tasks",
    "cancel_work": "tasks",
}


def _compile(words: Iterable[str]) -> re.Pattern[str]:
    return re.compile(
        r"\b(?:" + "|".join(words) + r")\b",
        flags=re.IGNORECASE,
    )


# ── per-family signal patterns ────────────────────────────────────────
# Generous on purpose: a match only costs the status quo. Each entry is
# one compiled alternation; ``matched`` on the decision records which
# families fired for observability.
_FAMILY_PATTERNS: dict[str, re.Pattern[str]] = {
    "time": _compile([
        r"what time", r"the time", r"time is it", r"current time",
        r"what day", r"which day", r"what date", r"the date",
        r"today'?s date", r"what month", r"what year", r"o'?clock",
        r"timezone", r"time zone", r"clock",
    ]),
    "web": _compile([
        r"search", r"look (?:it |that |this )?up", r"google", r"web",
        r"news", r"headlines?", r"weather", r"forecast", r"latest",
        r"price", r"prices", r"stock", r"score", r"release date",
        r"who (?:is|was|are)", r"what (?:is|are|was) (?:the|a|an)\b",
        r"how (?:much|many) (?:is|are|does|do)\b", r"happening",
        r"look up",
    ]),
    "recall": _compile([
        r"remember", r"recall", r"memor(?:y|ies)", r"forg[eo]t(?:ten)?",
        r"did i (?:tell|mention|say)", r"have i (?:told|mentioned|said)",
        r"what did i (?:say|tell)", r"we talked about",
        r"last (?:time|week|month) (?:i|we)\b",
    ]),
    "files": _compile([
        r"files?", r"folders?", r"director(?:y|ies)", r"documents?",
        r"notes?", r"\.md", r"\.txt", r"\.pdf", r"read (?:the|that|my|this)",
        r"open (?:the|that|my|this)", r"look (?:in|inside|at) (?:the|my)",
        r"path", r"drive", r"downloads", r"desktop",
    ]),
    "world": _compile([
        r"room", r"move", r"couch", r"bed", r"desk", r"window",
        r"bookshelf", r"kitchenette", r"beanbag", r"mirror",
        r"sit", r"lie down", r"lay down", r"posture", r"stand",
        r"eat", r"drink", r"cookies?", r"tea", r"snacks?",
        r"look around", r"garden", r"plants?", r"water", r"seeds?",
        r"harvest", r"sprout",
    ]),
    "goals": _compile([
        r"goals?", r"objectives?", r"milestones?", r"progress",
        r"working (?:on|towards?)", r"archive",
    ]),
    "tasks": _compile([
        r"tasks?", r"workflows?", r"cancel", r"status", r"running",
        r"done yet", r"finished?", r"check (?:my|your|the|on)",
        r"work on", r"start(?:ed)? (?:the|a|that)",
    ]),
}

# Generic action-request shapes that should run the pass whenever ANY
# tool is registered — imperatives that usually precede a tool-shaped
# request even when the object noun is unusual ("fetch the thing we
# saved", "show me what you've got").
_GENERIC_PATTERN = _compile([
    r"can you (?:check|find|look|get|list|show|read|search|fetch)",
    r"could you (?:check|find|look|get|list|show|read|search|fetch)",
    r"show me", r"list (?:the|all|every|your)", r"find (?:the|me|out)",
    r"fetch", r"bring up", r"pull up", r"check (?:the|if|whether|what)",
])


def families_for_tools(tool_names: Iterable[str]) -> tuple[set[str], set[str]]:
    """Map registered tool names to (active pattern families, unknown names)."""
    families: set[str] = set()
    unknown: set[str] = set()
    for name in tool_names:
        family = _TOOL_FAMILY.get(name)
        if family is None:
            unknown.add(name)
        else:
            families.add(family)
    return families, unknown


# ── brain-lane progressive disclosure (the "SkillRouter") ─────────────
# The P14 gate already classifies the user's text into tool families.
# When the skill router is enabled we reuse that classification to pick
# the *subset* of tools to expose this turn, instead of always shipping
# the whole registry. The brain families ARE the brain skill-groups.

# Families always exposed when the router narrows, so trivial asks and
# Aiko's spontaneous room actions (sip tea, shift posture on a turn whose
# text named no item) never miss a tool. ``world`` is deliberately in the
# core: it is cheap and the most immersion-relevant lane.
BRAIN_CORE_FAMILIES: frozenset[str] = frozenset({"time", "recall", "world"})


def select_active_tool_names(
    decision: GateDecision,
    registered_tool_names: Iterable[str],
    *,
    core_families: Iterable[str] = BRAIN_CORE_FAMILIES,
    router_enabled: bool = False,
) -> set[str] | None:
    """Pick the brain tool subset to expose this turn, or ``None`` to send
    every registered tool (no narrowing).

    ``None`` is returned whenever the router is disabled, the pass isn't
    running, or the gate fired for any reason other than a specific
    ``signal_<family>`` text match. Every continuity / fallback case
    (``force`` / ``finished_task`` / ``tasks_active`` / ``last_turn_tool``
    / ``unknown_tool`` / ``gate_error`` / ``disabled`` / ``generic_request``)
    therefore widens to the full toolset — preserving the gate's
    conservative contract that a false negative is the only real
    regression.

    When the gate matched specific families, the returned set is every
    registered tool whose family is in ``decision.matched ∪ core_families``
    (core always included).
    """
    if not router_enabled or not decision.run:
        return None
    if not decision.reason.startswith("signal_"):
        return None
    active_families = set(decision.matched) | set(core_families)
    allow: set[str] = set()
    for name in registered_tool_names:
        family = _TOOL_FAMILY.get(name)
        if family is not None and family in active_families:
            allow.add(name)
    return allow


def should_run_tool_pass(
    user_text: str,
    registered_tool_names: Iterable[str],
    *,
    context: GateContext,
) -> GateDecision:
    """Decide whether the forced tool-decision pass needs to run.

    Continuity signals win over the text heuristic; the text heuristic
    only consults pattern families that have registered tools.
    """
    # ── continuity / bypass rules (always run) ────────────────────────
    if context.force:
        decision = GateDecision(run=True, reason="force")
    elif context.finished_task_block:
        decision = GateDecision(run=True, reason="finished_task")
    elif context.tasks_active:
        decision = GateDecision(run=True, reason="tasks_active")
    elif context.last_turn_dispatched_tool:
        decision = GateDecision(run=True, reason="last_turn_tool")
    else:
        families, unknown = families_for_tools(registered_tool_names)
        if unknown:
            # Future tool with no pattern family: degrade to status quo.
            decision = GateDecision(
                run=True,
                reason="unknown_tool",
                matched=tuple(sorted(unknown)),
            )
        else:
            text = (user_text or "").strip()
            if not text:
                decision = GateDecision(run=False, reason="empty_text")
            else:
                matched = tuple(sorted(
                    family for family in families
                    if _FAMILY_PATTERNS[family].search(text)
                ))
                if matched:
                    decision = GateDecision(
                        run=True,
                        reason="signal_" + "+".join(matched),
                        matched=matched,
                    )
                elif families and _GENERIC_PATTERN.search(text):
                    decision = GateDecision(
                        run=True, reason="generic_request",
                    )
                else:
                    decision = GateDecision(run=False, reason="no_signal")

    log.info(
        "tool-gate: run=%s reason=%s matched=%s",
        "1" if decision.run else "0",
        decision.reason,
        ",".join(decision.matched) if decision.matched else "-",
    )
    return decision

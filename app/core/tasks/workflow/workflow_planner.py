"""Workflow planner — the plan→act→observe brain of a goal workflow.

The :class:`GoalWorkflowHandler` (next chunk) runs the loop; this module
owns the one decision per iteration: *given the goal, the skill
catalogue, and everything observed so far, what should I do next?* The
answer is a :class:`PlannerDecision` — either spawn a skill, finish with
findings, or declare a missing capability.

Design constraints:

* **Worker LLM, not the chat model.** The planner runs on
  ``worker_client`` (the gated background model), so it never competes
  with the conversational reply for the chat model's slot.
* **Budgeted blackboard.** The step history is rendered most-recent-
  first and truncated to ``history_budget_chars`` so a long workflow
  can't blow the worker model's context. Observations are individually
  capped before the global budget so one giant file-read result can't
  starve the rest of the history.
* **Strict validation + safe fallback.** The LLM returns JSON; we parse
  defensively. Any malformed / unparseable / unknown-action response
  degrades to a ``finish`` decision (the workflow stops and reports what
  it has) rather than raising — a planner that can't be understood must
  not wedge the loop.
* **Missing-capability is a first-class outcome.** When the goal needs
  something no registered skill provides (e.g. "open this URL and click
  the login button" with no browser skill), the planner returns
  ``action="missing_capability"`` naming what it needs. The handler logs
  it as a capability gap and finishes gracefully — Aiko then tells the
  user "I don't know how to do that yet" and names the missing piece.

This module is pure: no orchestrator, no settings, no I/O beyond the
single ``worker_client.chat_json`` call. The handler passes everything
in via :class:`PlannerInput`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any


log = logging.getLogger("app.tasks.workflow.planner")


# ── action kinds ──────────────────────────────────────────────────────
ACTION_SKILL = "skill"
ACTION_FINISH = "finish"
ACTION_MISSING_CAPABILITY = "missing_capability"

# Outcome labels the planner may stamp on a finish.
OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"
OUTCOME_NOTHING_FOUND = "nothing_found"
_VALID_OUTCOMES = frozenset(
    (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_NOTHING_FOUND)
)

# Per-observation char cap applied BEFORE the global history budget.
_OBSERVATION_CAP = 600
# Greedy match of the first {...} object in a possibly-noisy response.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class PlannerStep:
    """One completed iteration of the loop, for the blackboard render.

    ``skill`` is the skill that ran (or the action kind for non-spawn
    steps), ``args`` what it was called with, ``status`` the child's
    terminal status (``done`` / ``failed`` / ``cancelled`` /
    ``rejected``), and ``observation`` a short text summary of the
    result the handler folded back.
    """

    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    observation: str = ""


@dataclass(frozen=True, slots=True)
class PlannerInput:
    """Everything the planner needs to pick the next action."""

    goal: str
    skills: list[dict[str, Any]]  # registry.describe_for_planner()
    steps: list[PlannerStep] = field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 6
    history_budget_chars: int = 4000
    user_name: str = "the user"
    # Optional per-skill-group operational playbook (e.g. the browser
    # snapshot-first workflow), injected only when the relevant group's
    # skills are in the menu. Empty = no GUIDANCE block.
    guidance: str = ""


@dataclass(frozen=True, slots=True)
class PlannerDecision:
    """The planner's verdict for one iteration."""

    action: str  # ACTION_SKILL | ACTION_FINISH | ACTION_MISSING_CAPABILITY
    skill: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    findings: str = ""
    outcome: str = ""
    missing_capability: str = ""

    @property
    def is_finish(self) -> bool:
        return self.action == ACTION_FINISH

    @property
    def is_missing_capability(self) -> bool:
        return self.action == ACTION_MISSING_CAPABILITY


# ── blackboard render ─────────────────────────────────────────────────


def _render_skill_catalogue(skills: list[dict[str, Any]]) -> str:
    """Render the skill catalogue as a compact, planner-readable block."""
    lines: list[str] = []
    for skill in skills:
        name = skill.get("name", "")
        desc = skill.get("description", "")
        args = skill.get("args", {}) or {}
        arg_bits: list[str] = []
        for arg_name, spec in args.items():
            required = (
                "required" if isinstance(spec, dict) and spec.get("required")
                else "optional"
            )
            arg_bits.append(f"{arg_name} ({required})")
        arg_str = ", ".join(arg_bits) if arg_bits else "no args"
        lines.append(f"- {name}: {desc} [args: {arg_str}]")
    return "\n".join(lines)


def _render_history(steps: list[PlannerStep], budget_chars: int) -> str:
    """Render the step history most-recent-first within ``budget_chars``.

    Each observation is capped at :data:`_OBSERVATION_CAP` first, then
    steps are added newest-to-oldest until the budget is hit. Returns a
    chronological (oldest-first) block so the planner reads the arc in
    order, even though truncation drops the OLDEST steps.
    """
    if not steps:
        return "(no steps yet — this is the first decision)"
    rendered: list[str] = []
    used = 0
    # Walk newest-first so truncation keeps recent context.
    for idx in range(len(steps) - 1, -1, -1):
        step = steps[idx]
        obs = (step.observation or "").strip()
        if len(obs) > _OBSERVATION_CAP:
            obs = obs[:_OBSERVATION_CAP].rstrip() + "…"
        args_str = ""
        if step.args:
            try:
                args_str = json.dumps(step.args, ensure_ascii=False)
            except Exception:
                args_str = str(step.args)
        block = (
            f"Step {idx + 1}: {step.skill}"
            + (f" {args_str}" if args_str else "")
            + f" -> [{step.status or 'unknown'}] {obs}"
        )
        if used + len(block) > budget_chars and rendered:
            rendered.append("… (older steps truncated)")
            break
        rendered.append(block)
        used += len(block)
    rendered.reverse()
    return "\n".join(rendered)


_SYSTEM_PROMPT = (
    "You are the background task planner for Aiko, an AI companion. You "
    "are given a GOAL and a set of SKILLS you can use to accomplish it. "
    "You work step by step: each turn you pick ONE next action, see its "
    "result, then decide again. Be efficient — use the fewest steps that "
    "actually answer the goal.\n\n"
    "Respond with ONE JSON object and nothing else:\n"
    '{\n'
    '  "action": "<skill name> | finish | missing_capability",\n'
    '  "args": { ... },            // arguments for the chosen skill\n'
    '  "reason": "<one short sentence on why>",\n'
    '  "findings": "<summary, only when action=finish>",\n'
    '  "outcome": "success | partial | nothing_found (only when finish)",\n'
    '  "missing_capability": "<what you need, only when action=missing_capability>"\n'
    "}\n\n"
    "Rules:\n"
    "- Pick a skill from the catalogue by its exact name to make progress.\n"
    "- Pick \"finish\" when you have enough to answer the goal, OR when no "
    "further step would help. Put a concise summary in \"findings\".\n"
    "- Pick \"missing_capability\" ONLY when the goal genuinely requires "
    "something none of the skills can do; name the missing capability "
    "plainly (e.g. \"open and interact with a web page\"). Do NOT use it "
    "just because a step failed.\n"
    "- Do NOT repeat an action that already ran with the same args.\n"
    "- Do NOT invent skills or args that aren't in the catalogue.\n"
    "- Be HONEST about failure. If steps failed and you could not actually "
    "complete the goal, finish with outcome \"partial\" or \"nothing_found\" "
    "and say plainly what went wrong in \"findings\". NEVER claim an action "
    "succeeded (e.g. \"copied the file\") when its step status was failed."
)


def render_planner_messages(ctx: PlannerInput) -> list[dict[str, str]]:
    """Build the ``messages`` list for the planner's ``chat_json`` call."""
    catalogue = _render_skill_catalogue(ctx.skills)
    history = _render_history(ctx.steps, ctx.history_budget_chars)
    guidance_block = (
        f"GUIDANCE:\n{ctx.guidance.strip()}\n\n" if ctx.guidance.strip() else ""
    )
    user_block = (
        f"GOAL (for {ctx.user_name}): {ctx.goal.strip()}\n\n"
        f"SKILLS:\n{catalogue}\n\n"
        f"{guidance_block}"
        f"STEPS SO FAR (iteration {ctx.iteration + 1} of "
        f"{ctx.max_iterations}):\n{history}\n\n"
        "Decide the next action. Respond with one JSON object."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


# ── decision parsing ──────────────────────────────────────────────────


def parse_planner_response(
    raw: str, *, valid_skill_names: set[str]
) -> PlannerDecision:
    """Parse + validate the planner's raw JSON into a decision.

    Defensive: on any parse failure or unknown action, returns a
    ``finish`` decision with ``outcome=partial`` so the loop stops
    cleanly instead of wedging. ``valid_skill_names`` is the set of
    spawnable skill names PLUS ``"finish"`` (the terminal skill) — the
    handler passes ``registry.names()``.
    """
    text = (raw or "").strip()
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        log.warning("planner: no JSON object in response (chars=%d)", len(text))
        return PlannerDecision(
            action=ACTION_FINISH,
            outcome=OUTCOME_PARTIAL,
            reason="planner response was not parseable",
        )
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("planner: JSON decode failed")
        return PlannerDecision(
            action=ACTION_FINISH,
            outcome=OUTCOME_PARTIAL,
            reason="planner response was not valid JSON",
        )
    if not isinstance(parsed, dict):
        return PlannerDecision(
            action=ACTION_FINISH,
            outcome=OUTCOME_PARTIAL,
            reason="planner response was not an object",
        )
    action_raw = str(parsed.get("action", "") or "").strip()
    args = parsed.get("args")
    if not isinstance(args, dict):
        args = {}
    reason = str(parsed.get("reason", "") or "").strip()[:300]

    # missing_capability branch.
    if action_raw == ACTION_MISSING_CAPABILITY:
        gap = str(parsed.get("missing_capability", "") or "").strip()[:300]
        if not gap:
            # Treat an empty gap as a finish — nothing actionable.
            return PlannerDecision(
                action=ACTION_FINISH,
                outcome=OUTCOME_PARTIAL,
                reason=reason or "missing_capability without detail",
            )
        return PlannerDecision(
            action=ACTION_MISSING_CAPABILITY,
            reason=reason,
            missing_capability=gap,
        )

    # finish branch (explicit "finish" action).
    if action_raw == "finish" or action_raw == ACTION_FINISH:
        outcome = str(parsed.get("outcome", "") or "").strip().lower()
        if outcome not in _VALID_OUTCOMES:
            outcome = OUTCOME_SUCCESS
        return PlannerDecision(
            action=ACTION_FINISH,
            args=args,
            reason=reason,
            findings=str(parsed.get("findings", "") or "").strip(),
            outcome=outcome,
        )

    # skill branch — must name a known, spawnable skill.
    if action_raw and action_raw in valid_skill_names and action_raw != "finish":
        return PlannerDecision(
            action=ACTION_SKILL,
            skill=action_raw,
            args=args,
            reason=reason,
        )

    # Unknown / empty action: stop cleanly.
    log.warning(
        "planner: unknown action=%r (valid=%s) -> finishing",
        action_raw,
        sorted(valid_skill_names),
    )
    return PlannerDecision(
        action=ACTION_FINISH,
        outcome=OUTCOME_PARTIAL,
        reason=f"planner picked an unknown action ({action_raw!r})",
    )


def decide_next_action(
    worker_client: Any,
    ctx: PlannerInput,
    *,
    valid_skill_names: set[str],
    model: str | None = None,
    max_tokens: int = 512,
    timeout_seconds: float | None = None,
) -> PlannerDecision:
    """Run one planner decision via ``worker_client.chat_json``.

    Returns a validated :class:`PlannerDecision`. Any transport / LLM
    failure degrades to a ``finish`` decision (the workflow stops and
    reports what it has) — the planner must never raise back into the
    handler loop.
    """
    messages = render_planner_messages(ctx)
    options: dict[str, object] = {"num_predict": int(max_tokens)}
    try:
        raw, _usage = worker_client.chat_json(
            messages,
            model=model,
            options=options,
            timeout_seconds=timeout_seconds,
            format_json=True,
            surface="workflow_planner",
        )
    except Exception:
        log.warning("planner: chat_json failed -> finishing", exc_info=True)
        return PlannerDecision(
            action=ACTION_FINISH,
            outcome=OUTCOME_PARTIAL,
            reason="planner LLM call failed",
        )
    decision = parse_planner_response(raw, valid_skill_names=valid_skill_names)
    log.info(
        "planner decision: iter=%d action=%s skill=%s reason=%s",
        ctx.iteration,
        decision.action,
        decision.skill or "-",
        (decision.reason or "")[:80],
    )
    return decision


__all__ = [
    "ACTION_SKILL",
    "ACTION_FINISH",
    "ACTION_MISSING_CAPABILITY",
    "OUTCOME_SUCCESS",
    "OUTCOME_PARTIAL",
    "OUTCOME_NOTHING_FOUND",
    "PlannerStep",
    "PlannerInput",
    "PlannerDecision",
    "render_planner_messages",
    "parse_planner_response",
    "decide_next_action",
]

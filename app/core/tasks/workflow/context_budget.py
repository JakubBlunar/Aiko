"""Task-owned context accounting. Durable goals are never shortened to fit a prompt."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re

from app.llm.token_utils import estimate_tokens


class TaskContextOverflow(ValueError):
    """The immutable request cannot fit safely; ask for scope instead of forgetting it."""


def tokens(text: str, scale=1.0) -> int:
    # The shared estimator learns from normal use; the byte floor also covers
    # source/Unicode-heavy cold starts. Headroom is still an estimate, not a tokenizer.
    non_ascii = sum(ord(char) > 127 for char in text)
    conservative = (len(text) - non_ascii) / 3 + non_ascii * 2
    return math.ceil(max(estimate_tokens(text), conservative) * max(1, scale))


def message_tokens(messages, scale=1.0):
    return sum(tokens(message["content"], scale) + 8 for message in messages) + 8


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class TaskBudget:
    window: int
    input_limit: int
    target: int
    answer: int
    reasoning: int
    margin: int
    scale: float = 1.0

    @classmethod
    def resolve(cls, window=None, *, answer=768, routine=False, scale=1.0):
        window = int(window or 4096)
        window = max(256, window)
        margin = max(128, min(8192, math.ceil(window * 0.15)))
        output = min(answer, 384 if routine else 768, max(64, window // 12))
        reasoning = 0 if routine else min(512, window // 24)
        hard = max(0, window - margin - output - reasoning)
        target = min(hard, max(2048, min(12288, window // 4)))
        return cls(window, hard, target, output, reasoning, margin, max(1, float(scale)))

    def report(self, messages, **extra):
        return {
            "context_window": self.window,
            "input_limit": self.input_limit,
            "input_target": self.target,
            "estimated_input_tokens": message_tokens(messages, self.scale),
            "answer_reserved": self.answer,
            "reasoning_reserved": self.reasoning,
            "safety_reserved": self.margin,
            "estimate_scale": self.scale,
            **extra,
        }


def fit_text(text, allowance, *, scale=1.0, focus=""):
    """Fit complete relevant lines first, without inventing or breaking a reference."""
    if tokens(text, scale) <= allowance:
        return text, False
    words = set(re.findall(r"\w{3,}", focus.casefold()))
    lines = text.splitlines()
    order = sorted(
        range(len(lines)),
        key=lambda n: (n != 0, -len(words & set(re.findall(r"\w{3,}", lines[n].casefold()))), n),
    )
    selected, used = [], 0
    marker = "\n[Partial excerpt; inspect the source result for omitted text.]"
    allowance -= tokens(marker, scale)
    for index in order:
        cost = tokens(lines[index] + "\n", scale)
        if cost + used <= allowance:
            selected.append(index)
            used += cost
    if selected:
        return "\n".join(lines[n] for n in sorted(selected)) + marker, True
    # Prose can be one enormous line; keep a marked excerpt rather than dropping
    # all evidence. Structured tool schemas never pass through this helper.
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if tokens(text[:mid], scale) <= max(0, allowance):
            low = mid
        else:
            high = mid - 1
    return text[:low] + marker, True


def pack_planner(ctx, budget):
    from dataclasses import replace
    from app.core.tasks.workflow.workflow_planner import render_planner_messages

    source = deepcopy(ctx.work or {})
    # These values carry user authority and temporal/subject identity. They are
    # fixed context, never replaced by generated notes or a model summary.
    work = {
        key: source[key]
        for key in (
            "contract",
            "profile",
            "profile_instructions",
            "user_guidance",
            "remaining_steps",
        )
        if key in source
    }
    state = replace(
        ctx,
        skills=[],
        steps=[],
        guidance="",
        already_tried=[],
        work=work if ctx.work is not None else None,
        compact=True,
    )

    def render():
        return render_planner_messages(state)

    fixed = message_tokens(render(), budget.scale)
    if fixed + 64 > budget.input_limit:
        raise TaskContextOverflow(
            "The original goal, criteria and constraints exceed safe context."
        )
    target = min(budget.input_limit, max(budget.target, fixed + 1024))
    observation = source.get("current_observation")
    if isinstance(observation, dict):
        observation = deepcopy(observation)
        content = str(observation.pop("content", ""))
        if content:
            # Current evidence gets space before optional history. Grow toward
            # the hard headroom boundary when a small model needs explicit state.
            desired = min(5000, tokens(content, budget.scale))
            target = min(budget.input_limit, max(target, fixed + desired + 1024))
            allowance = min(max(128, (target - fixed) // 2), 5000)
            observation["content"], partial = fit_text(
                content, allowance, scale=budget.scale, focus=ctx.goal
            )
            observation["partial"] = observation.get("partial", False) or partial
        work["current_observation"] = observation

    def fits():
        return message_tokens(render(), budget.scale) <= target

    if not fits():
        work.pop("current_observation", None)
        if observation:
            work["current_observation"] = {
                "source_task_id": observation.get("source_task_id"),
                "status": "omitted",
                "instruction": "Inspect this owned result or request a narrower observation.",
            }

    # Whole valid schemas, with descriptions bounded independently. The selected
    # names are returned to admission; omitted tools cannot be called this turn.
    for row in ctx.skills:
        if len(state.skills) >= 6:
            break
        row = dict(row)
        trial = [*state.skills, row]
        old = state.skills
        state = replace(state, skills=trial)
        if not fits():
            state = replace(state, skills=old)

    # Operational rules are selected by the caller for these capabilities, and
    # are fixed as a whole; never cut a safety sentence in half to fit it.
    if ctx.guidance:
        state = replace(state, guidance=ctx.guidance)
        if not fits():
            # Drop menus/history first to retain the selected playbook in full.
            while state.skills and not fits():
                state = replace(state, skills=state.skills[:-1])
            if not fits():
                raise TaskContextOverflow("The selected capability guidance cannot fit safely.")

    candidates = [row for row in ctx.skills if not row.get("terminal")]
    if candidates and not any(not row.get("terminal") for row in state.skills):
        for row in candidates:
            state = replace(state, skills=[row])
            needed = message_tokens(render(), budget.scale)
            if needed <= budget.input_limit:
                target = max(target, needed)
                break
        else:
            raise TaskContextOverflow("A complete operation schema cannot fit with the request.")

    def add(key, value):
        work[key] = value
        if not fits():
            work.pop(key, None)
            return False
        return True

    for key in ("inspected_results", "plan", "notes", "capability_groups", "profiles"):
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            kept = []
            for item in reversed(value):
                if add(key, [item, *kept]):
                    kept.insert(0, item)
                else:
                    if kept:
                        work[key] = kept
                    break
        else:
            add(key, value)
    # Newest observations first in selection, chronological order in the prompt.
    for step in reversed(ctx.steps):
        previous = state.steps
        state = replace(state, steps=[step, *previous])
        if not fits():
            state = replace(state, steps=previous)
            break
    messages = render()
    report = budget.report(
        messages,
        input_target=target,
        selected_tools=len(state.skills),
        omitted_tools=len(ctx.skills) - len(state.skills),
        retained_steps=len(state.steps),
        omitted_steps=len(ctx.steps) - len(state.steps),
    )
    if report["estimated_input_tokens"] > budget.input_limit:
        raise TaskContextOverflow("The task prompt cannot fit safely.")
    return messages, state.skills, report


def pack_review(system, frame, evidence, budget):
    """Retain the complete contract/findings; fit evidence records with explicit partiality."""
    frame = deepcopy(frame)
    frame["evidence"] = []

    def render():
        return [{"role": "system", "content": system}, {"role": "user", "content": dumps(frame)}]

    fixed = message_tokens(render(), budget.scale)
    if fixed + 64 > budget.input_limit:
        raise TaskContextOverflow("The review contract exceeds safe context.")
    target = min(budget.input_limit, max(budget.target, fixed + 1024))
    for index, source in enumerate(evidence):
        record = deepcopy(source)
        key = "content" if "content" in record else "result"
        text = str(record.pop(key, ""))
        available = (
            target
            - message_tokens(render(), budget.scale)
            - tokens(dumps(record), budget.scale)
            - 64
        )
        if available < 64:
            break
        # Spread room over remaining sources, but allow explicit inspected excerpts
        # first so a relevant tail can survive a giant returned file prefix.
        allowance = max(64, available // max(1, min(3, len(evidence) - index)))
        record[key], partial = fit_text(
            text, allowance, scale=budget.scale, focus=dumps(frame.get("contract", {}))
        )
        record["partial"] = record.get("partial", False) or partial
        frame["evidence"].append(record)
        if message_tokens(render(), budget.scale) > target:
            frame["evidence"].pop()
            break
    frame["omitted_evidence_records"] = len(evidence) - len(frame["evidence"])
    messages = render()
    if message_tokens(messages, budget.scale) > budget.input_limit:
        raise TaskContextOverflow("The review prompt cannot fit safely.")
    return messages, budget.report(
        messages,
        retained_evidence=len(frame["evidence"]),
        evidence_ids=[row["task_id"] for row in frame["evidence"]],
        omitted_evidence=frame["omitted_evidence_records"],
    )

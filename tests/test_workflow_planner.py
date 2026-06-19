"""Tests for the workflow planner: render, parse, and decide."""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.core.tasks.workflow.workflow_planner import (
    ACTION_FINISH,
    ACTION_MISSING_CAPABILITY,
    ACTION_SKILL,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    PlannerInput,
    PlannerStep,
    decide_next_action,
    parse_planner_response,
    render_planner_messages,
)


_SKILLS = [
    {
        "name": "search_files",
        "description": "Search files.",
        "args": {"query": {"required": False}, "only_new": {"required": False}},
        "terminal": False,
    },
    {
        "name": "read_file",
        "description": "Read a file.",
        "args": {"path": {"required": True}},
        "terminal": False,
    },
    {"name": "finish", "description": "Stop.", "args": {}, "terminal": True},
]
_VALID = {"search_files", "read_file", "finish"}


class RenderTests(unittest.TestCase):
    def test_messages_contain_goal_and_skills(self) -> None:
        ctx = PlannerInput(goal="find new notes", skills=_SKILLS, user_name="Jacob")
        msgs = render_planner_messages(ctx)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        user = msgs[1]["content"]
        self.assertIn("find new notes", user)
        self.assertIn("search_files", user)
        self.assertIn("read_file", user)
        self.assertIn("Jacob", user)

    def test_empty_history_note(self) -> None:
        ctx = PlannerInput(goal="g", skills=_SKILLS)
        user = render_planner_messages(ctx)[1]["content"]
        self.assertIn("no steps yet", user)

    def test_guidance_block_rendered_when_present(self) -> None:
        ctx = PlannerInput(
            goal="g",
            skills=_SKILLS,
            guidance="Snapshot FIRST. Refs go stale after navigation.",
        )
        user = render_planner_messages(ctx)[1]["content"]
        self.assertIn("GUIDANCE:", user)
        self.assertIn("Snapshot FIRST", user)
        # GUIDANCE sits between SKILLS and STEPS SO FAR.
        self.assertLess(user.index("SKILLS:"), user.index("GUIDANCE:"))
        self.assertLess(user.index("GUIDANCE:"), user.index("STEPS SO FAR"))

    def test_no_guidance_block_when_empty(self) -> None:
        ctx = PlannerInput(goal="g", skills=_SKILLS, guidance="   ")
        user = render_planner_messages(ctx)[1]["content"]
        self.assertNotIn("GUIDANCE:", user)

    def test_history_budget_truncates_oldest(self) -> None:
        steps = [
            PlannerStep(
                skill="search_files",
                args={"query": f"q{i}"},
                status="done",
                observation="X" * 300,
            )
            for i in range(20)
        ]
        ctx = PlannerInput(
            goal="g", skills=_SKILLS, steps=steps, history_budget_chars=500
        )
        user = render_planner_messages(ctx)[1]["content"]
        self.assertIn("truncated", user)
        # Most recent step (Step 20) must survive.
        self.assertIn("Step 20", user)

    def test_observation_capped(self) -> None:
        steps = [
            PlannerStep(skill="read_file", status="done", observation="Y" * 5000)
        ]
        ctx = PlannerInput(
            goal="g", skills=_SKILLS, steps=steps, history_budget_chars=4000
        )
        user = render_planner_messages(ctx)[1]["content"]
        self.assertIn("…", user)
        # The 5000-char observation must have been capped well under 5000.
        self.assertLess(user.count("Y"), 1000)


class ParseTests(unittest.TestCase):
    def test_skill_action(self) -> None:
        raw = json.dumps(
            {"action": "search_files", "args": {"only_new": True}, "reason": "go"}
        )
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertEqual(d.action, ACTION_SKILL)
        self.assertEqual(d.skill, "search_files")
        self.assertTrue(d.args["only_new"])

    def test_finish_action(self) -> None:
        raw = json.dumps(
            {"action": "finish", "findings": "found 3", "outcome": "success"}
        )
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertTrue(d.is_finish)
        self.assertEqual(d.findings, "found 3")
        self.assertEqual(d.outcome, OUTCOME_SUCCESS)

    def test_finish_invalid_outcome_defaults_success(self) -> None:
        raw = json.dumps({"action": "finish", "outcome": "weird"})
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertEqual(d.outcome, OUTCOME_SUCCESS)

    def test_missing_capability(self) -> None:
        raw = json.dumps(
            {
                "action": "missing_capability",
                "missing_capability": "open a web page",
                "reason": "no browser skill",
            }
        )
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertTrue(d.is_missing_capability)
        self.assertEqual(d.missing_capability, "open a web page")

    def test_missing_capability_empty_degrades_to_finish(self) -> None:
        raw = json.dumps({"action": "missing_capability", "missing_capability": ""})
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertTrue(d.is_finish)

    def test_unknown_skill_degrades_to_finish(self) -> None:
        raw = json.dumps({"action": "teleport", "args": {}})
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertTrue(d.is_finish)
        self.assertEqual(d.outcome, OUTCOME_PARTIAL)

    def test_non_json_degrades_to_finish(self) -> None:
        d = parse_planner_response("I think we should search", valid_skill_names=_VALID)
        self.assertTrue(d.is_finish)
        self.assertEqual(d.outcome, OUTCOME_PARTIAL)

    def test_json_embedded_in_prose(self) -> None:
        raw = 'Sure! {"action": "read_file", "args": {"path": "a.md"}} done.'
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertEqual(d.action, ACTION_SKILL)
        self.assertEqual(d.skill, "read_file")

    def test_args_not_dict_coerced_empty(self) -> None:
        raw = json.dumps({"action": "search_files", "args": "nope"})
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertEqual(d.args, {})

    def test_finish_named_skill_not_treated_as_skill_action(self) -> None:
        # "finish" is in valid names but must route to the finish branch.
        raw = json.dumps({"action": "finish"})
        d = parse_planner_response(raw, valid_skill_names=_VALID)
        self.assertTrue(d.is_finish)
        self.assertEqual(d.action, ACTION_FINISH)


class _FakeClient:
    def __init__(self, raw: str | None = None, *, raises: bool = False) -> None:
        self._raw = raw
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self._raises:
            raise RuntimeError("boom")
        return self._raw, None


class DecideTests(unittest.TestCase):
    def test_decide_skill(self) -> None:
        client = _FakeClient(json.dumps({"action": "read_file", "args": {"path": "x"}}))
        ctx = PlannerInput(goal="g", skills=_SKILLS)
        d = decide_next_action(client, ctx, valid_skill_names=_VALID)
        self.assertEqual(d.action, ACTION_SKILL)
        self.assertEqual(d.skill, "read_file")
        self.assertEqual(client.calls[0]["kwargs"]["surface"], "workflow_planner")

    def test_decide_llm_failure_finishes(self) -> None:
        client = _FakeClient(raises=True)
        ctx = PlannerInput(goal="g", skills=_SKILLS)
        d = decide_next_action(client, ctx, valid_skill_names=_VALID)
        self.assertTrue(d.is_finish)
        self.assertEqual(d.outcome, OUTCOME_PARTIAL)

    def test_decide_passes_max_tokens(self) -> None:
        client = _FakeClient(json.dumps({"action": "finish"}))
        ctx = PlannerInput(goal="g", skills=_SKILLS)
        decide_next_action(client, ctx, valid_skill_names=_VALID, max_tokens=256)
        self.assertEqual(client.calls[0]["kwargs"]["options"]["num_predict"], 256)


if __name__ == "__main__":
    unittest.main()

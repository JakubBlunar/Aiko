"""Tests for the worker-lane skill router
(:mod:`app.core.tasks.workflow.workflow_skill_router`) and the
group-filtered ``describe_for_planner``.

Contracts pinned here:

1. **Single match narrows** — a goal that clearly names one capability
   group returns that single group.
2. **Conservative fallback** — zero matches and multi-group matches both
   return ``None`` (full menu), so the planner never has a needed skill
   hidden by an over-eager router.
3. **Availability gating** — only groups present in the registry are
   considered.
4. **describe_for_planner(groups=)** — filters by group but ALWAYS keeps
   the terminal ``finish`` skill and any uncategorised skill.
"""
from __future__ import annotations

import unittest

from app.core.tasks.workflow import (
    WORKFLOW_SKILL_FINISH,
    WorkflowSkill,
    WorkflowSkillRegistry,
    build_builtin_skill_registry,
)
from app.core.tasks.workflow.workflow_skill_router import select_skill_groups


_BUILTIN_GROUPS = {"files", "web"}  # vision/file_write off by default


class SelectSkillGroupsTests(unittest.TestCase):
    def test_single_files_match(self) -> None:
        groups = select_skill_groups(
            "find my Q4 budget file and read it", _BUILTIN_GROUPS,
        )
        self.assertEqual(groups, {"files"})

    def test_single_web_match(self) -> None:
        groups = select_skill_groups(
            "search the web for the latest python release", _BUILTIN_GROUPS,
        )
        self.assertEqual(groups, {"web"})

    def test_multi_group_returns_none(self) -> None:
        # Spans files + web -> ambiguous, widen to full menu.
        groups = select_skill_groups(
            "search online and save the results to a file", _BUILTIN_GROUPS,
        )
        self.assertIsNone(groups)

    def test_zero_match_returns_none(self) -> None:
        groups = select_skill_groups(
            "think about what we talked about yesterday", _BUILTIN_GROUPS,
        )
        self.assertIsNone(groups)

    def test_empty_goal_returns_none(self) -> None:
        self.assertIsNone(select_skill_groups("   ", _BUILTIN_GROUPS))

    def test_no_available_groups_returns_none(self) -> None:
        self.assertIsNone(select_skill_groups("read a file", set()))

    def test_unavailable_group_not_selected(self) -> None:
        # Vision keyword, but vision group isn't registered -> no match.
        groups = select_skill_groups(
            "describe this screenshot", _BUILTIN_GROUPS,
        )
        self.assertIsNone(groups)

    def test_vision_match_when_available(self) -> None:
        groups = select_skill_groups(
            "what's in this screenshot?", {"files", "vision"},
        )
        self.assertEqual(groups, {"vision"})

    def test_mcp_group_matched_by_server_id_token(self) -> None:
        groups = select_skill_groups(
            "use playwright to open the page", {"mcp:playwright"},
        )
        self.assertEqual(groups, {"mcp:playwright"})

    def test_mcp_group_not_matched_without_token(self) -> None:
        # No server-id token in the goal -> safe full-menu fallback.
        groups = select_skill_groups(
            "open the page and click login", {"mcp:playwright"},
        )
        self.assertIsNone(groups)


class DescribeForPlannerGroupsTests(unittest.TestCase):
    def _registry(self) -> WorkflowSkillRegistry:
        # files (search/read) + web + finish by default.
        return build_builtin_skill_registry(web_search_enabled=True)

    def test_full_menu_when_groups_none(self) -> None:
        reg = self._registry()
        names = {s["name"] for s in reg.describe_for_planner()}
        self.assertIn("search_files", names)
        self.assertIn("web_search", names)
        self.assertIn(WORKFLOW_SKILL_FINISH, names)

    def test_files_group_filter_keeps_finish_drops_web(self) -> None:
        reg = self._registry()
        described = reg.describe_for_planner(groups={"files"})
        names = {s["name"] for s in described}
        self.assertIn("search_files", names)
        self.assertIn("read_file", names)
        # finish is terminal -> always present.
        self.assertIn(WORKFLOW_SKILL_FINISH, names)
        # web is a different group -> dropped.
        self.assertNotIn("web_search", names)

    def test_uncategorised_skill_never_hidden(self) -> None:
        reg = WorkflowSkillRegistry()
        reg.register(WorkflowSkill(name="misc", description="d", spawn=lambda a, c: 1))
        reg.register(
            WorkflowSkill(
                name="search_files", description="d",
                spawn=lambda a, c: 1, group="files",
            )
        )
        names = {
            s["name"] for s in reg.describe_for_planner(groups={"web"})
        }
        # "misc" has no group -> always included; files dropped (not web).
        self.assertIn("misc", names)
        self.assertNotIn("search_files", names)

    def test_groups_helper_lists_nonempty_groups(self) -> None:
        reg = self._registry()
        self.assertEqual(reg.groups(), {"files", "web"})

    def test_describe_includes_group_field(self) -> None:
        reg = self._registry()
        by_name = {s["name"]: s for s in reg.describe_for_planner()}
        self.assertEqual(by_name["search_files"]["group"], "files")
        self.assertEqual(by_name["web_search"]["group"], "web")


if __name__ == "__main__":
    unittest.main()

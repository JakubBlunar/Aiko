"""Tests for per-skill-group planner guidance (skill_guidance).

Guidance now comes exclusively from plugin ``SKILL.md`` / runtime-captured
server instructions (``group_guidance`` keyed by ``mcp:<server_id>``). There
are no hardcoded browser / filesystem playbooks in the core anymore.
"""
from __future__ import annotations

import unittest

from app.core.tasks.workflow.skill_guidance import guidance_for_skills


class GuidanceForSkillsTests(unittest.TestCase):
    def test_group_guidance_for_present_mcp_group(self) -> None:
        skills = [{"name": "browser__browser_snapshot", "group": "mcp:browser"}]
        out = guidance_for_skills(
            skills,
            group_guidance={"mcp:browser": "BROWSER GUIDANCE"},
        )
        self.assertEqual(out, "BROWSER GUIDANCE")

    def test_multiple_groups_compose_sorted(self) -> None:
        skills = [
            {"name": "browser__browser_snapshot", "group": "mcp:browser"},
            {"name": "filesystem__move_file", "group": "mcp:filesystem"},
        ]
        out = guidance_for_skills(
            skills,
            group_guidance={
                "mcp:browser": "BROWSER GUIDANCE",
                "mcp:filesystem": "FS GUIDANCE",
            },
        )
        # Sorted by group name: mcp:browser before mcp:filesystem.
        self.assertEqual(out, "BROWSER GUIDANCE\n\nFS GUIDANCE")

    def test_empty_when_no_group_guidance(self) -> None:
        skills = [{"name": "browser__browser_snapshot", "group": "mcp:browser"}]
        self.assertEqual(guidance_for_skills(skills), "")

    def test_guidance_ignored_when_group_absent(self) -> None:
        skills = [{"name": "web_search", "group": "web"}]
        out = guidance_for_skills(
            skills,
            group_guidance={"mcp:github": "GITHUB GUIDANCE"},
        )
        self.assertEqual(out, "")

    def test_only_mcp_groups_considered(self) -> None:
        # A non-mcp group (built-in) never pulls guidance even if a same-name
        # key somehow exists.
        skills = [{"name": "web_search", "group": "web"}]
        out = guidance_for_skills(
            skills,
            group_guidance={"web": "SHOULD NOT APPEAR"},
        )
        self.assertEqual(out, "")

    def test_arbitrary_plugin_group_included(self) -> None:
        skills = [{"name": "github__create_issue", "group": "mcp:github"}]
        out = guidance_for_skills(
            skills,
            group_guidance={"mcp:github": "GITHUB GUIDANCE"},
        )
        self.assertIn("GITHUB GUIDANCE", out)

    def test_tolerates_empty_and_none_groups(self) -> None:
        skills = [
            {"name": "x", "group": ""},
            {"name": "browser__browser_snapshot", "group": "mcp:browser"},
        ]
        out = guidance_for_skills(
            skills,
            group_guidance={"mcp:browser": "BROWSER GUIDANCE"},
        )
        self.assertEqual(out, "BROWSER GUIDANCE")

    def test_blank_guidance_entry_skipped(self) -> None:
        skills = [{"name": "browser__browser_snapshot", "group": "mcp:browser"}]
        out = guidance_for_skills(
            skills,
            group_guidance={"mcp:browser": "   "},
        )
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()

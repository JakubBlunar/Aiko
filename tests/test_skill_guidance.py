"""Tests for per-skill-group planner playbooks (skill_guidance)."""
from __future__ import annotations

import unittest

from app.core.tasks.workflow.skill_guidance import (
    BROWSER_PLAYBOOK,
    FILESYSTEM_PLAYBOOK,
    filesystem_group_for_skills,
    filesystem_playbook,
    guidance_for_groups,
    guidance_for_skills,
)


class GuidanceForGroupsTests(unittest.TestCase):
    def test_browser_playbook_when_group_present(self) -> None:
        out = guidance_for_groups(
            {"files", "mcp:browser"}, browser_group="mcp:browser"
        )
        self.assertEqual(out, BROWSER_PLAYBOOK)
        self.assertIn("Snapshot FIRST", out)
        self.assertIn("STALE", out)

    def test_empty_when_browser_group_absent(self) -> None:
        out = guidance_for_groups({"files", "web"}, browser_group="mcp:browser")
        self.assertEqual(out, "")

    def test_empty_when_no_browser_group_configured(self) -> None:
        out = guidance_for_groups({"mcp:browser"}, browser_group="")
        self.assertEqual(out, "")

    def test_respects_custom_server_id_group(self) -> None:
        # A swapped browser server registers under its own mcp:<id> group.
        out = guidance_for_groups(
            {"mcp:chrome"}, browser_group="mcp:chrome"
        )
        self.assertEqual(out, BROWSER_PLAYBOOK)
        # ...and the default group no longer matches.
        self.assertEqual(
            guidance_for_groups({"mcp:chrome"}, browser_group="mcp:browser"),
            "",
        )

    def test_tolerates_empty_and_none_groups(self) -> None:
        out = guidance_for_groups(
            {"", "mcp:browser"}, browser_group="mcp:browser"
        )
        self.assertEqual(out, BROWSER_PLAYBOOK)

    def test_playbook_mentions_key_safety_rules(self) -> None:
        # The condensed playbook must keep the upstream guardrails.
        self.assertIn("close tabs", BROWSER_PLAYBOOK.lower())
        self.assertIn("navigate away", BROWSER_PLAYBOOK.lower())


class FilesystemDetectionTests(unittest.TestCase):
    def test_detects_group_by_marker_tool(self) -> None:
        skills = [
            {"name": "filesystem__write_file", "group": "mcp:filesystem"},
            {"name": "filesystem__list_allowed_directories", "group": "mcp:filesystem"},
        ]
        self.assertEqual(
            filesystem_group_for_skills(skills), "mcp:filesystem"
        )

    def test_custom_server_id_still_detected(self) -> None:
        skills = [{"name": "fsbox__move_file", "group": "mcp:fsbox"}]
        self.assertEqual(filesystem_group_for_skills(skills), "mcp:fsbox")

    def test_no_match_without_marker_tools(self) -> None:
        skills = [
            {"name": "browser__browser_snapshot", "group": "mcp:browser"},
            {"name": "search_files", "group": "files"},
        ]
        self.assertEqual(filesystem_group_for_skills(skills), "")

    def test_builtin_file_group_is_not_filesystem_mcp(self) -> None:
        # Built-in file skills live in group "files" (no mcp: prefix) and
        # must NOT trigger the MCP filesystem playbook.
        skills = [{"name": "read_file", "group": "files"}]
        self.assertEqual(filesystem_group_for_skills(skills), "")


class GuidanceForSkillsTests(unittest.TestCase):
    def test_filesystem_playbook_when_fs_skill_present(self) -> None:
        skills = [
            {"name": "filesystem__list_allowed_directories", "group": "mcp:filesystem"},
        ]
        out = guidance_for_skills(skills, browser_group="mcp:browser")
        self.assertIn(FILESYSTEM_PLAYBOOK, out)
        self.assertNotIn(BROWSER_PLAYBOOK, out)

    def test_both_playbooks_compose(self) -> None:
        skills = [
            {"name": "browser__browser_snapshot", "group": "mcp:browser"},
            {"name": "filesystem__move_file", "group": "mcp:filesystem"},
        ]
        out = guidance_for_skills(skills, browser_group="mcp:browser")
        self.assertIn(BROWSER_PLAYBOOK, out)
        self.assertIn(FILESYSTEM_PLAYBOOK, out)

    def test_empty_when_no_special_groups(self) -> None:
        skills = [{"name": "search_files", "group": "files"}]
        self.assertEqual(guidance_for_skills(skills, browser_group="mcp:browser"), "")

    def test_filesystem_playbook_warns_off_label_paths(self) -> None:
        # The exact failure mode the user hit must be addressed in copy.
        self.assertIn("outside allowed directories", FILESYSTEM_PLAYBOOK)
        self.assertIn("Documents", FILESYSTEM_PLAYBOOK)
        self.assertIn("absolute", FILESYSTEM_PLAYBOOK.lower())


class FilesystemRootInliningTests(unittest.TestCase):
    def test_playbook_inlines_explicit_root(self) -> None:
        out = filesystem_playbook(["F:\\AikosFiles"])
        self.assertIn("F:\\AikosFiles", out)
        self.assertIn("EXACTLY", out)
        self.assertIn("Do NOT invent", out)

    def test_playbook_without_roots_is_base(self) -> None:
        self.assertEqual(filesystem_playbook([]), FILESYSTEM_PLAYBOOK)
        self.assertEqual(filesystem_playbook(None), FILESYSTEM_PLAYBOOK)

    def test_guidance_uses_root_lookup_for_fs_server(self) -> None:
        skills = [
            {"name": "filesystem__write_file", "group": "mcp:filesystem"},
            {"name": "filesystem__list_allowed_directories", "group": "mcp:filesystem"},
        ]
        captured: list[str] = []

        def _lookup(server_id: str) -> list[str]:
            captured.append(server_id)
            return ["F:\\AikosFiles"]

        out = guidance_for_skills(skills, root_lookup=_lookup)
        # Looked up by the bare server id (group minus the mcp: prefix).
        self.assertEqual(captured, ["filesystem"])
        self.assertIn("F:\\AikosFiles", out)

    def test_guidance_root_lookup_failure_falls_back(self) -> None:
        skills = [{"name": "fs__move_file", "group": "mcp:fs"}]

        def _boom(server_id: str) -> list[str]:
            raise RuntimeError("nope")

        out = guidance_for_skills(skills, root_lookup=_boom)
        # Still includes the base playbook, just without the inlined root.
        self.assertIn("outside allowed directories", out)


if __name__ == "__main__":
    unittest.main()

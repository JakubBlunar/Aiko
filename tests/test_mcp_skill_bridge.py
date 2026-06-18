"""Tests for the MCP → WorkflowSkill bridge (``register_mcp_skills``)."""
from __future__ import annotations

import unittest

from app.core.tasks.handler_names import HANDLER_MCP_TOOL
from app.core.tasks.task_handler import INITIATED_BY_BACKGROUND
from app.core.tasks.workflow.mcp_skills import (
    _arg_schema_from_input,
    register_mcp_skills,
)
from app.core.tasks.workflow.skill_registry import (
    SpawnContext,
    WorkflowSkillRegistry,
)
from app.mcp.client.manager import McpToolDescriptor


class _FakeManager:
    def __init__(self, descriptors) -> None:
        self._descriptors = descriptors

    def list_available_tools(self):
        return list(self._descriptors)


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self._next_id = 100

    def start_task(self, **kwargs):
        self.spawned.append(kwargs)
        self._next_id += 1
        return self._next_id


class ArgSchemaConversionTests(unittest.TestCase):
    def test_projects_properties_and_required(self) -> None:
        input_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "the path"},
                "depth": {"type": "integer", "description": "how deep"},
            },
            "required": ["path"],
        }
        out = _arg_schema_from_input(input_schema)
        self.assertEqual(out["path"]["type"], "string")
        self.assertTrue(out["path"]["required"])
        self.assertEqual(out["depth"]["type"], "integer")
        self.assertFalse(out["depth"]["required"])

    def test_non_dict_returns_empty(self) -> None:
        self.assertEqual(_arg_schema_from_input(None), {})
        self.assertEqual(_arg_schema_from_input([]), {})


class RegisterMcpSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = WorkflowSkillRegistry()
        self.descriptors = [
            McpToolDescriptor(
                server_id="filesystem",
                name="read_text_file",
                description="Read a file's text.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            McpToolDescriptor(
                server_id="browser",
                name="read_text_file",  # same tool name, different server
                description="Read page text.",
                input_schema={},
            ),
        ]
        self.manager = _FakeManager(self.descriptors)

    def test_registers_namespaced_skills(self) -> None:
        names = register_mcp_skills(self.registry, self.manager)
        self.assertEqual(
            sorted(names),
            ["browser__read_text_file", "filesystem__read_text_file"],
        )
        # No collision: both servers' read_text_file coexist.
        self.assertIsNotNone(self.registry.get("filesystem__read_text_file"))
        self.assertIsNotNone(self.registry.get("browser__read_text_file"))

    def test_skill_description_and_args(self) -> None:
        register_mcp_skills(self.registry, self.manager)
        skill = self.registry.get("filesystem__read_text_file")
        self.assertIn("filesystem", skill.description)
        self.assertIn("Read a file's text", skill.description)
        self.assertTrue(skill.arg_schema["path"]["required"])
        self.assertTrue(skill.spawnable)

    def test_spawn_starts_mcp_tool_child(self) -> None:
        register_mcp_skills(self.registry, self.manager)
        orch = _FakeOrchestrator()
        ctx = SpawnContext(orchestrator=orch, user_id="u1", parent_task_id=42)
        child_id = self.registry.spawn_child(
            "filesystem__read_text_file", {"path": "notes.md"}, ctx
        )
        self.assertEqual(child_id, 101)
        self.assertEqual(len(orch.spawned), 1)
        spawned = orch.spawned[0]
        self.assertEqual(spawned["handler_name"], HANDLER_MCP_TOOL)
        self.assertEqual(spawned["initiated_by"], INITIATED_BY_BACKGROUND)
        self.assertFalse(spawned["notify_aiko"])
        self.assertEqual(spawned["parent_task_id"], 42)
        self.assertEqual(spawned["args"]["server_id"], "filesystem")
        self.assertEqual(spawned["args"]["tool_name"], "read_text_file")
        self.assertEqual(spawned["args"]["tool_args"], {"path": "notes.md"})

    def test_idempotent_reregister(self) -> None:
        register_mcp_skills(self.registry, self.manager)
        # Calling again (e.g. on reconnect) overwrites cleanly.
        register_mcp_skills(self.registry, self.manager)
        self.assertEqual(
            len(
                [
                    n
                    for n in self.registry.names()
                    if n.endswith("read_text_file")
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()

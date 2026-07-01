"""Tests for the WorkflowSkillRegistry + built-in skill spawn functions."""
from __future__ import annotations

import unittest
from typing import Any

from app.core.tasks.handler_names import (
    HANDLER_WEB_SEARCH,
)
from app.core.tasks.workflow import (
    WORKFLOW_SKILL_FINISH,
    SpawnContext,
    WorkflowSkill,
    WorkflowSkillRegistry,
    build_builtin_skill_registry,
)


class _FakeOrchestrator:
    """Records ``start_task`` calls and returns sequential ids."""

    def __init__(self, *, reject: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next = 100
        self._reject = reject

    def start_task(self, **kwargs: Any) -> int | None:
        self.calls.append(kwargs)
        if self._reject:
            return None
        self._next += 1
        return self._next


def _ctx(orch: Any, *, parent: int = 7, user: str = "u1") -> SpawnContext:
    return SpawnContext(orchestrator=orch, user_id=user, parent_task_id=parent)


class RegistryBasicsTests(unittest.TestCase):
    def test_register_and_get(self) -> None:
        reg = WorkflowSkillRegistry()
        skill = WorkflowSkill(name="x", description="d", spawn=lambda a, c: 1)
        reg.register(skill)
        self.assertIs(reg.get("x"), skill)
        self.assertIsNone(reg.get("missing"))

    def test_register_empty_name_raises(self) -> None:
        reg = WorkflowSkillRegistry()
        with self.assertRaises(ValueError):
            reg.register(WorkflowSkill(name="  ", description="d"))

    def test_reregister_overwrites(self) -> None:
        reg = WorkflowSkillRegistry()
        reg.register(WorkflowSkill(name="x", description="first"))
        reg.register(WorkflowSkill(name="x", description="second"))
        self.assertEqual(reg.get("x").description, "second")
        self.assertEqual(reg.names(), ["x"])

    def test_spawnable_vs_terminal(self) -> None:
        spawn_skill = WorkflowSkill(name="s", description="d", spawn=lambda a, c: 1)
        term_skill = WorkflowSkill(name="t", description="d", terminal=True)
        self.assertTrue(spawn_skill.spawnable)
        self.assertFalse(term_skill.spawnable)
        # A skill with a spawn fn but terminal=True is not spawnable.
        weird = WorkflowSkill(
            name="w", description="d", spawn=lambda a, c: 1, terminal=True
        )
        self.assertFalse(weird.spawnable)


class BuiltinRegistryTests(unittest.TestCase):
    def test_default_skills(self) -> None:
        # File skills are no longer built in — they come from the filesystem
        # MCP plugin. The core registry ships web search + terminal finish.
        reg = build_builtin_skill_registry()
        self.assertEqual(reg.names(), ["finish", "web_search"])
        self.assertEqual(reg.spawnable_names(), ["web_search"])

    def test_web_search_can_be_disabled(self) -> None:
        reg = build_builtin_skill_registry(web_search_enabled=False)
        self.assertNotIn("web_search", reg.names())
        # finish is always present.
        self.assertIn(WORKFLOW_SKILL_FINISH, reg.names())

    def test_describe_for_planner_shape(self) -> None:
        reg = build_builtin_skill_registry()
        desc = reg.describe_for_planner()
        names = {d["name"] for d in desc}
        self.assertEqual(names, set(reg.names()))
        for entry in desc:
            self.assertIn("description", entry)
            self.assertIn("args", entry)
            self.assertIn("terminal", entry)
        finish = next(d for d in desc if d["name"] == "finish")
        self.assertTrue(finish["terminal"])


class SpawnChildTests(unittest.TestCase):
    def test_web_search_spawn(self) -> None:
        reg = build_builtin_skill_registry()
        orch = _FakeOrchestrator()
        tid = reg.spawn_child(
            "web_search", {"query": "weather", "max_results": 3}, _ctx(orch)
        )
        self.assertEqual(tid, 101)
        call = orch.calls[0]
        self.assertEqual(call["handler_name"], HANDLER_WEB_SEARCH)
        self.assertEqual(call["args"]["query"], "weather")
        self.assertEqual(call["args"]["max_results"], 3)

    def test_web_search_clamps_max_results(self) -> None:
        reg = build_builtin_skill_registry()
        orch = _FakeOrchestrator()
        reg.spawn_child("web_search", {"query": "x", "max_results": 999}, _ctx(orch))
        self.assertEqual(orch.calls[0]["args"]["max_results"], 10)

    def test_spawn_unknown_skill_returns_none(self) -> None:
        reg = build_builtin_skill_registry()
        orch = _FakeOrchestrator()
        self.assertIsNone(reg.spawn_child("nope", {}, _ctx(orch)))
        self.assertEqual(orch.calls, [])

    def test_spawn_terminal_skill_returns_none(self) -> None:
        reg = build_builtin_skill_registry()
        orch = _FakeOrchestrator()
        self.assertIsNone(reg.spawn_child("finish", {}, _ctx(orch)))
        self.assertEqual(orch.calls, [])

    def test_spawn_rejected_by_orchestrator(self) -> None:
        reg = build_builtin_skill_registry()
        orch = _FakeOrchestrator(reject=True)
        tid = reg.spawn_child("web_search", {"query": "x"}, _ctx(orch))
        self.assertIsNone(tid)

    def test_spawn_fn_exception_downgraded_to_none(self) -> None:
        reg = WorkflowSkillRegistry()

        def _boom(args: dict[str, Any], ctx: SpawnContext) -> int | None:
            raise RuntimeError("kaboom")

        reg.register(WorkflowSkill(name="x", description="d", spawn=_boom))
        orch = _FakeOrchestrator()
        self.assertIsNone(reg.spawn_child("x", {}, _ctx(orch)))


if __name__ == "__main__":
    unittest.main()

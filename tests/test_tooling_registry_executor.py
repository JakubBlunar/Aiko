from __future__ import annotations

import unittest

from app.core.tooling.config_loader import ToolPolicyConfig, ToolingConfig
from app.core.tooling.executor import ToolExecutor
from app.core.tooling.registry import ToolRegistry
from app.core.tooling.types import ToolContext, ToolResult, ToolSpec


class _DummyTool:
    def __init__(self, *, name: str, is_mutating: bool = False) -> None:
        self.spec = ToolSpec(
            name=name,
            description="dummy",
            is_mutating=is_mutating,
            input_schema={"required": ["value"], "properties": {"value": "int"}},
        )

    def run(self, context: ToolContext, args: dict, cancel_token=None) -> ToolResult:
        return ToolResult(success=True, data={"echo": args.get("value")})


class ToolingRegistryExecutorTests(unittest.TestCase):
    def test_registry_filters(self) -> None:
        registry = ToolRegistry()
        registry.register(_DummyTool(name="a"))
        registry.register(_DummyTool(name="b"))

        specs = registry.filtered_specs(enabled=["a"], disabled=[])
        self.assertEqual([item.name for item in specs], ["a"])

    def test_executor_schema_validation(self) -> None:
        registry = ToolRegistry()
        registry.register(_DummyTool(name="schema.tool"))
        executor = ToolExecutor(registry, ToolingConfig())

        missing = executor.invoke("schema.tool", args={})
        self.assertFalse(missing.success)
        self.assertEqual(missing.error.code, "missing_required_arg")

        bad_type = executor.invoke("schema.tool", args={"value": "wrong"})
        self.assertFalse(bad_type.success)
        self.assertEqual(bad_type.error.code, "invalid_arg_type")

        ok = executor.invoke("schema.tool", args={"value": 3})
        self.assertTrue(ok.success)
        self.assertEqual(ok.data.get("echo"), 3)

    def test_executor_confirmation_for_mutating_tool(self) -> None:
        registry = ToolRegistry()
        registry.register(_DummyTool(name="mut.tool", is_mutating=True))
        cfg = ToolingConfig(policies=ToolPolicyConfig(full_auto=False, mutating_requires_confirmation=True))
        executor = ToolExecutor(registry, cfg)

        result = executor.invoke("mut.tool", args={"value": 1})
        self.assertFalse(result.success)
        self.assertTrue(result.requires_confirmation)
        self.assertIsNotNone(result.confirmation)


if __name__ == "__main__":
    unittest.main()

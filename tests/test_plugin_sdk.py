"""Tests for the ToolPlugin SDK (PluginApi + middleware + gating helpers)."""
from __future__ import annotations

import unittest
from pathlib import Path

from app.plugins.sdk import (
    MiddlewareResult,
    PluginApi,
    PluginGatedError,
    ToolResultMiddleware,
)


def _api(config: dict | None = None) -> PluginApi:
    return PluginApi(
        plugin_id="demo", plugin_root=Path("/tmp/demo"), config=config or {}
    )


class _Mw:
    def claims(self, server_id: str, tool_name: str) -> bool:
        return tool_name == "snap"

    def transform(self, server_id, tool_name, raw_text, tool_args=None):
        return MiddlewareResult(content="C", summary="S", element_count=1)


class RegisterMcpServerTests(unittest.TestCase):
    def test_spec_captured_with_plugin_id(self) -> None:
        api = _api()
        api.register_mcp_server(command="npx", args=["-y", "srv"], timeout_seconds=15)
        spec = api.server_spec
        self.assertIsNotNone(spec)
        self.assertEqual(spec["id"], "demo")
        self.assertEqual(spec["command"], "npx")
        self.assertEqual(spec["args"], ["-y", "srv"])
        self.assertEqual(spec["timeout_seconds"], 15)

    def test_second_call_overwrites(self) -> None:
        api = _api()
        api.register_mcp_server(command="a")
        api.register_mcp_server(command="b")
        self.assertEqual(api.server_spec["command"], "b")


class RegisterSkillsTests(unittest.TestCase):
    def test_default_dir(self) -> None:
        api = _api()
        api.register_skills()
        self.assertEqual(api.skill_dirs, ["skills"])

    def test_named_dirs_dedup(self) -> None:
        api = _api()
        api.register_skills("a", "b", "a")
        self.assertEqual(api.skill_dirs, ["a", "b"])

    def test_inline_skill(self) -> None:
        api = _api()
        api.register_skill("n", "d", "body")
        self.assertEqual(len(api.inline_skills), 1)
        self.assertEqual(api.inline_skills[0].body, "body")


class MiddlewareTests(unittest.TestCase):
    def test_register_plain_middleware(self) -> None:
        api = _api()
        api.register_tool_result_middleware(_Mw())
        self.assertEqual(len(api.middlewares), 1)
        self.assertTrue(api.middlewares[0].claims("x", "snap"))

    def test_server_id_filter_wraps(self) -> None:
        api = _api()
        api.register_tool_result_middleware(_Mw(), server_id="browser")
        mw = api.middlewares[0]
        # AND-ed: right server + target's own claim.
        self.assertTrue(mw.claims("browser", "snap"))
        self.assertFalse(mw.claims("other", "snap"))
        self.assertEqual(mw.server_id, "browser")

    def test_tool_names_filter(self) -> None:
        api = _api()
        api.register_tool_result_middleware(_Mw(), tool_names=["only"])
        mw = api.middlewares[0]
        # tool "snap" passes target's claim but not the tool_names gate.
        self.assertFalse(mw.claims("x", "snap"))

    def test_protocol_isinstance(self) -> None:
        self.assertIsInstance(_Mw(), ToolResultMiddleware)


class RegisterFastToolTests(unittest.TestCase):
    def test_spec_captured(self) -> None:
        api = _api()
        api.register_fast_tool(
            name="calculate",
            description="do math",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "42",
            family="math",
            gate_patterns=["calculate", r"\d+\s*\+\s*\d+"],
        )
        tools = api.fast_tools
        self.assertEqual(len(tools), 1)
        spec = tools[0]
        self.assertEqual(spec.name, "calculate")
        self.assertEqual(spec.family, "math")
        self.assertEqual(spec.gate_patterns, ("calculate", r"\d+\s*\+\s*\d+"))
        self.assertEqual(spec.handler({}), "42")

    def test_multiple_tools_per_plugin(self) -> None:
        api = _api()
        api.register_fast_tool(
            name="a", description="", parameters={}, handler=lambda a: "a"
        )
        api.register_fast_tool(
            name="b", description="", parameters={}, handler=lambda a: "b"
        )
        self.assertEqual([t.name for t in api.fast_tools], ["a", "b"])

    def test_empty_name_ignored(self) -> None:
        api = _api()
        api.register_fast_tool(
            name="  ", description="", parameters={}, handler=lambda a: "x"
        )
        self.assertEqual(api.fast_tools, [])

    def test_non_callable_handler_ignored(self) -> None:
        api = _api()
        api.register_fast_tool(
            name="t", description="", parameters={}, handler="not callable",
        )
        self.assertEqual(api.fast_tools, [])

    def test_family_without_patterns_kept(self) -> None:
        # A family with no patterns is allowed; the wiring layer decides it
        # can't gate on it (degrades to always-run).
        api = _api()
        api.register_fast_tool(
            name="t", description="", parameters={}, handler=lambda a: "x",
            family="custom",
        )
        self.assertEqual(api.fast_tools[0].family, "custom")
        self.assertEqual(api.fast_tools[0].gate_patterns, ())


class GatingHelperTests(unittest.TestCase):
    def test_require_config_missing_raises(self) -> None:
        api = _api({})
        with self.assertRaises(PluginGatedError) as cm:
            api.require_config("root")
        self.assertIn("root", cm.exception.reason)

    def test_require_config_returns_value(self) -> None:
        api = _api({"root": "/x"})
        self.assertEqual(api.require_config("root"), "/x")

    def test_require_config_blank_string_raises(self) -> None:
        api = _api({"root": "   "})
        with self.assertRaises(PluginGatedError):
            api.require_config("root")

    def test_require_binary_missing_raises(self) -> None:
        api = _api()
        with self.assertRaises(PluginGatedError):
            api.require_binary("definitely-not-a-real-binary-xyz")

    def test_require_env(self) -> None:
        import os

        api = _api()
        with self.assertRaises(PluginGatedError):
            api.require_env("AIKO_TEST_ENV_UNSET_XYZ")
        os.environ["AIKO_TEST_ENV_SET"] = "v"
        try:
            self.assertEqual(api.require_env("AIKO_TEST_ENV_SET"), "v")
        finally:
            del os.environ["AIKO_TEST_ENV_SET"]


if __name__ == "__main__":
    unittest.main()

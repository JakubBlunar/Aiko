"""Tests for the bundled ``calculator`` plugin + the fast-tool adapter.

Covers three layers end-to-end:

* ``aiko_calc.safe_eval`` — the plugin-local AST-whitelisted evaluator
  (moved out of app core).
* The plugin ``entry.py`` -> SDK ``register_fast_tool`` -> ``_FastToolSpec``
  read-back path (loaded exactly as the runtime does).
* ``PluginFastTool`` wrapping that spec into the brain ``Tool`` protocol and
  dispatching through a real ``ToolRegistry``.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path

from app.llm.tools.base import ToolRegistry
from app.llm.tools.plugin_tool import PluginFastTool
from app.plugins.sdk import PluginApi


_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "calculator"
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from aiko_calc import CalcError, safe_eval  # noqa: E402  (needs sys.path)


def _load_calculate_spec():
    """Load the plugin entry, run define_plugin, return the fast-tool spec."""
    spec = importlib.util.spec_from_file_location(
        "calculator_entry_test", _PLUGIN_ROOT / "entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = PluginApi(plugin_id="calculator", plugin_root=_PLUGIN_ROOT)
    module.define_plugin(api)
    return api.fast_tools[0]


class SafeEvalTests(unittest.TestCase):
    def test_basic_arithmetic(self) -> None:
        self.assertEqual(safe_eval("1 + 2 * 3"), 7)
        self.assertEqual(safe_eval("(1 + 2) * 3"), 9)
        self.assertEqual(safe_eval("2 ** 10"), 1024)
        self.assertEqual(safe_eval("17 % 5"), 2)
        self.assertEqual(safe_eval("17 // 5"), 3)

    def test_float_and_functions(self) -> None:
        self.assertAlmostEqual(safe_eval("0.185 * 2340"), 432.9)
        self.assertAlmostEqual(safe_eval("sqrt(144)"), 12.0)
        self.assertAlmostEqual(safe_eval("log(1000, 10)"), 3.0)
        self.assertEqual(safe_eval("factorial(5)"), 120)
        self.assertAlmostEqual(safe_eval("pi"), math.pi)

    def test_rejects_names_and_calls(self) -> None:
        for expr in ("x + 1", "(1).__class__", "__import__('os')", "eval('1')"):
            with self.assertRaises(CalcError):
                safe_eval(expr)

    def test_rejects_strings_collections_empty(self) -> None:
        for expr in ("'a' * 3", "[1, 2, 3]", "", "   "):
            with self.assertRaises(CalcError):
                safe_eval(expr)

    def test_math_errors(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("1 / 0")
        with self.assertRaises(CalcError):
            safe_eval("2 ** 100000000")
        with self.assertRaises(CalcError):
            safe_eval("1 +")


class CalculatePluginToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = _load_calculate_spec()
        self.tool = PluginFastTool(self.spec)

    def test_spec_metadata(self) -> None:
        self.assertEqual(self.spec.name, "calculate")
        self.assertEqual(self.spec.family, "math")
        self.assertTrue(self.spec.gate_patterns)

    def test_adapter_schema(self) -> None:
        schema = self.tool.schema()
        self.assertEqual(schema.name, "calculate")
        self.assertIn("expression", schema.parameters["properties"])

    def test_adapter_run_returns_result(self) -> None:
        out = json.loads(self.tool.run({"expression": "0.185 * 2340"}))
        self.assertEqual(out["expression"], "0.185 * 2340")
        self.assertAlmostEqual(out["result"], 432.9)

    def test_dispatch_through_registry(self) -> None:
        registry = ToolRegistry()
        registry.register(self.tool)
        self.assertIn("calculate", registry.names())
        result = registry.dispatch("calculate", {"expression": "6 * 7"})
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content)["result"], 42)

    def test_dispatch_invalid_expression_is_error(self) -> None:
        registry = ToolRegistry()
        registry.register(self.tool)
        result = registry.dispatch("calculate", {"expression": "import os"})
        self.assertFalse(result.ok)
        self.assertIn("calculate", result.content)

    def test_dispatch_missing_expression_is_error(self) -> None:
        registry = ToolRegistry()
        registry.register(self.tool)
        result = registry.dispatch("calculate", {})
        self.assertFalse(result.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

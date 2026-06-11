"""Tests for the AST-whitelisted arithmetic evaluator + calculate tool."""
from __future__ import annotations

import json
import math
import unittest

from app.core.calc import CalcError, safe_eval
from app.llm.tools.base import ToolError
from app.llm.tools.calc import CalculateTool


class SafeEvalTests(unittest.TestCase):
    def test_basic_arithmetic(self) -> None:
        self.assertEqual(safe_eval("1 + 2 * 3"), 7)
        self.assertEqual(safe_eval("(1 + 2) * 3"), 9)
        self.assertEqual(safe_eval("10 - 4 - 3"), 3)
        self.assertEqual(safe_eval("2 ** 10"), 1024)
        self.assertEqual(safe_eval("17 % 5"), 2)
        self.assertEqual(safe_eval("17 // 5"), 3)

    def test_float_and_percentage(self) -> None:
        self.assertAlmostEqual(safe_eval("0.185 * 2340"), 432.9)
        self.assertAlmostEqual(safe_eval("3 / 2"), 1.5)

    def test_unary(self) -> None:
        self.assertEqual(safe_eval("-5 + 3"), -2)
        self.assertEqual(safe_eval("--5"), 5)

    def test_functions_and_constants(self) -> None:
        self.assertAlmostEqual(safe_eval("sqrt(144)"), 12.0)
        self.assertAlmostEqual(safe_eval("log(1000, 10)"), 3.0)
        self.assertAlmostEqual(safe_eval("abs(-7)"), 7)
        self.assertEqual(safe_eval("max(1, 9, 4)"), 9)
        self.assertEqual(safe_eval("factorial(5)"), 120)
        self.assertAlmostEqual(safe_eval("pi"), math.pi)
        self.assertAlmostEqual(safe_eval("sin(0)"), 0.0)

    def test_rejects_names(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("x + 1")

    def test_rejects_attribute_access(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("(1).__class__")

    def test_rejects_disallowed_call(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("__import__('os')")
        with self.assertRaises(CalcError):
            safe_eval("eval('1')")

    def test_rejects_strings_and_collections(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("'a' * 3")
        with self.assertRaises(CalcError):
            safe_eval("[1, 2, 3]")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("")
        with self.assertRaises(CalcError):
            safe_eval("   ")

    def test_division_by_zero(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("1 / 0")

    def test_huge_power_rejected(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("2 ** 100000000")

    def test_keyword_args_rejected(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("round(1.23456, ndigits=2)")

    def test_syntax_error(self) -> None:
        with self.assertRaises(CalcError):
            safe_eval("1 +")


class CalculateToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = CalculateTool()

    def test_schema_name(self) -> None:
        self.assertEqual(self.tool.schema().name, "calculate")

    def test_run_returns_result(self) -> None:
        out = json.loads(self.tool.run({"expression": "0.185 * 2340"}))
        self.assertEqual(out["expression"], "0.185 * 2340")
        self.assertAlmostEqual(out["result"], 432.9)

    def test_run_missing_expression(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({})

    def test_run_invalid_expression_raises_toolerror(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"expression": "import os"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

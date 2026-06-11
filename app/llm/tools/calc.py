"""``calculate`` — synchronous exact-arithmetic agent tool.

Unlike the file / workflow tools (which spawn background tasks and
report a turn or two later), ``calculate`` is a fast, in-turn tool: it
evaluates the expression and returns the exact result in the same
reply. This is the cure for "what's 18.5% of 2,340?" being a
hallucinated number — Aiko calls the tool and reads back the real
value.

The heavy lifting is the AST-whitelisted :func:`app.core.calc.safe_eval`
(no ``eval``); this module is just the thin LLM-facing wrapper.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.calc import CalcError, safe_eval
from app.llm.tools.base import ToolError, ToolSchema


log = logging.getLogger("app.tools.calc")


class CalculateTool:
    """Evaluate an arithmetic expression and return the exact result."""

    name = "calculate"

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="calculate",
            description=(
                "Evaluate an arithmetic expression and return the EXACT "
                "result. ALWAYS use this for any non-trivial arithmetic "
                "instead of computing it in your head — percentages, "
                "multi-step sums, unit conversions, powers, roots. "
                "SYNCHRONOUS: the result comes back in the same turn, so "
                "use it in your reply directly. Supports + - * / // % **, "
                "parentheses, and functions like sqrt, sin, cos, log, "
                "log10, exp, floor, ceil, abs, round, min, max, factorial, "
                "plus the constants pi, e, tau. Examples: '0.185 * 2340', "
                "'(1+2)**10', 'sqrt(2) * 100', 'log(1000, 10)'. Returns "
                "JSON: {expression, result}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "The arithmetic expression to evaluate, e.g. "
                            "'0.185 * 2340' or 'sqrt(144) + 7'. Numbers, "
                            "operators, parentheses, and allow-listed math "
                            "functions only — no variables or code."
                        ),
                    },
                },
                "required": ["expression"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        expression = str((arguments or {}).get("expression", "") or "").strip()
        if not expression:
            raise ToolError("calculate: 'expression' is required")
        try:
            result = safe_eval(expression)
        except CalcError as exc:
            raise ToolError(f"calculate: {exc}") from exc
        log.info(
            "calculate: expr=%r result=%s", expression[:120], result
        )
        return json.dumps(
            {"expression": expression, "result": result},
            ensure_ascii=False,
        )


__all__ = ["CalculateTool"]

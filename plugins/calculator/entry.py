"""Calculator ToolPlugin -- a synchronous exact-arithmetic fast tool.

Registers one brain-lane fast tool (``calculate``) via the ToolPlugin SDK.
Unlike the background workflow/MCP tools (which spawn a task and report a
turn or two later), a fast tool returns inline in the same reply -- this is
the cure for "what's 18.5% of 2,340?" being a hallucinated number.

The evaluator is the plugin-local, AST-whitelisted ``aiko_calc.safe_eval``
(no ``eval``); the runtime puts this plugin's root on ``sys.path`` so the
``aiko_calc`` package imports cleanly and stays decoupled from app core.
"""
from __future__ import annotations

import json
import logging


log = logging.getLogger("app.plugins.calculator")

_DESCRIPTION = (
    "Evaluate an arithmetic expression and return the EXACT result. "
    "ALWAYS use this for any non-trivial arithmetic instead of computing "
    "it in your head -- percentages, multi-step sums, unit conversions, "
    "powers, roots. SYNCHRONOUS: the result comes back in the same turn, "
    "so use it in your reply directly. Supports + - * / // % **, "
    "parentheses, and functions like sqrt, sin, cos, log, log10, exp, "
    "floor, ceil, abs, round, min, max, factorial, plus the constants "
    "pi, e, tau. Examples: '0.185 * 2340', '(1+2)**10', 'sqrt(2) * 100', "
    "'log(1000, 10)'. Returns JSON: {expression, result}."
)

_PARAMETERS = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": (
                "The arithmetic expression to evaluate, e.g. "
                "'0.185 * 2340' or 'sqrt(144) + 7'. Numbers, operators, "
                "parentheses, and allow-listed math functions only -- no "
                "variables or code."
            ),
        },
    },
    "required": ["expression"],
}

# P14 gate patterns for the ``math`` family: number-crunching turns route
# to ``calculate`` so Aiko never guesses a sum. Generous by design (a false
# positive only costs the status-quo pass); the numeric-operator
# alternation catches "2340 * 0.185"-shaped asks that carry no keyword.
_GATE_PATTERNS = [
    r"calculate", r"comput(?:e|ing|ation)", r"arithmetic",
    r"square root", r"to the power", r"percent", r"percentage",
    r"multipl(?:y|ied|ication)", r"divide[d]?", r"divided by",
    r"how much is", r"\d+\s*[-+*/x^]\s*\d+",
]


def _calculate(arguments: dict) -> str:
    from aiko_calc import CalcError, safe_eval

    expression = str((arguments or {}).get("expression", "") or "").strip()
    if not expression:
        raise ValueError("calculate: 'expression' is required")
    try:
        result = safe_eval(expression)
    except CalcError as exc:
        raise ValueError(f"calculate: {exc}") from exc
    log.info("calculate: expr=%r result=%s", expression[:120], result)
    return json.dumps(
        {"expression": expression, "result": result},
        ensure_ascii=False,
    )


def define_plugin(api) -> None:
    api.register_fast_tool(
        name="calculate",
        description=_DESCRIPTION,
        parameters=_PARAMETERS,
        handler=_calculate,
        family="math",
        gate_patterns=_GATE_PATTERNS,
    )

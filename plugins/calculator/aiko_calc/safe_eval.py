"""AST-whitelisted arithmetic evaluator — no ``eval``, no names leak.

The ``calculate`` fast tool needs to give the LLM an *exact* arithmetic
result instead of letting it guess ("what's 18.5% of 2340?" should not
be a hallucinated number). The obvious-but-dangerous way is
``eval(expr)``; this module is the safe replacement.

It parses the expression with :func:`ast.parse` (``mode="eval"``) and
walks the tree, allowing ONLY:

* numeric literals (int / float, and complex is rejected);
* the binary operators ``+ - * / // % **``;
* unary ``+`` / ``-``;
* parentheses (implicit in the AST);
* a small allow-list of ``math`` functions (``sqrt``, ``sin``,
  ``log``, ``floor``, …) plus ``abs`` / ``round`` / ``min`` / ``max``;
* the constants ``pi`` / ``e`` / ``tau``.

Anything else — attribute access (``(1).__class__``), arbitrary names,
calls to non-allowlisted functions, comprehensions, lambdas, string
literals — raises :class:`CalcError`. Exponentiation is bounded so
``2 ** 10**9`` can't hang the process.

Ships inside the ``calculator`` plugin bundle (the runtime puts the
plugin root on ``sys.path`` so ``entry.py`` can ``import aiko_calc``);
it does not depend on app core.
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable


class CalcError(Exception):
    """Raised for any unsupported / invalid arithmetic expression."""


# Binary + unary operator dispatch tables.
_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Allow-listed callables. Everything is pure + numeric.
_FUNCS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1.0 / 3.0), x),
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "exp": math.exp,
    "log": math.log,  # log(x) or log(x, base)
    "log2": math.log2,
    "log10": math.log10,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "fabs": math.fabs,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "degrees": math.degrees,
    "radians": math.radians,
    "hypot": math.hypot,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": lambda *args: sum(args),
    "pow": pow,
}

# Allow-listed bare names (constants).
_NAMES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

# Bound on exponentiation to keep ``a ** b`` from blowing up. We reject
# when the (numeric) exponent's magnitude exceeds this and the base is
# not in [-1, 1].
_MAX_POW_EXPONENT = 1000


def safe_eval(expression: str) -> float | int:
    """Evaluate an arithmetic ``expression`` safely.

    Returns an ``int`` or ``float``. Raises :class:`CalcError` for any
    syntax error, unsupported construct, or math error (division by
    zero, domain error, overflow).
    """
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("expression is empty")
    if len(expression) > 1000:
        raise CalcError("expression is too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"could not parse expression: {exc.msg}") from exc
    try:
        result = _eval(tree.body)
    except CalcError:
        raise
    except ZeroDivisionError as exc:
        raise CalcError("division by zero") from exc
    except (ValueError, OverflowError) as exc:
        raise CalcError(f"math error: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive catch-all
        raise CalcError(f"could not evaluate expression: {exc}") from exc
    if isinstance(result, bool):  # bool is an int subclass; reject it
        raise CalcError("boolean results are not supported")
    if not isinstance(result, (int, float)):
        raise CalcError("expression did not evaluate to a number")
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise CalcError("result is not a finite number")
    return result


def _eval(node: ast.AST) -> Any:
    """Recursively evaluate a whitelisted AST node."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
            node.value, (int, float)
        ):
            raise CalcError("only numeric literals are allowed")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise CalcError(
                f"operator not allowed: {type(node.op).__name__}"
            )
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Pow):
            _guard_pow(left, right)
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise CalcError(
                f"unary operator not allowed: {type(node.op).__name__}"
            )
        return op(_eval(node.operand))
    if isinstance(node, ast.Call):
        return _eval_call(node)
    if isinstance(node, ast.Name):
        if node.id in _NAMES:
            return _NAMES[node.id]
        raise CalcError(f"unknown name: {node.id!r}")
    raise CalcError(f"unsupported expression element: {type(node).__name__}")


def _eval_call(node: ast.Call) -> Any:
    """Evaluate a whitelisted function call."""
    if not isinstance(node.func, ast.Name):
        raise CalcError("only direct function calls are allowed")
    fn = _FUNCS.get(node.func.id)
    if fn is None:
        raise CalcError(f"function not allowed: {node.func.id!r}")
    if node.keywords:
        raise CalcError("keyword arguments are not allowed")
    args = [_eval(arg) for arg in node.args]
    return fn(*args)


def _guard_pow(base: Any, exponent: Any) -> None:
    """Reject pathological exponentiation before it runs."""
    try:
        if abs(base) > 1 and abs(exponent) > _MAX_POW_EXPONENT:
            raise CalcError(
                "exponent too large (refusing to compute a huge power)"
            )
    except TypeError:  # pragma: no cover - non-numeric handled upstream
        return


__all__ = ["CalcError", "safe_eval"]

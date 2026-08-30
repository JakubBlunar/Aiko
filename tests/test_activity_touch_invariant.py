"""Invariant: ``_touch_user_activity`` is chat-turn only.

C6 perception is supposed to run while the user is coding and not
chatting. If ``user_activity`` WS frames ever reset the idle gate, the
whole pipeline silently stops. This file fails the run if a new caller
appears outside the chat-turn mixin.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app"

_ALLOWED = {
    (APP / "core" / "session" / "lifecycle_mixin.py").resolve(): {"_touch_user_activity"},
    (APP / "core" / "session" / "chat_turn_mixin.py").resolve(): {"chat_once_streaming"},
}


class TouchUserActivityCallersTests(unittest.TestCase):
    def test_only_chat_turn_calls_touch_user_activity(self) -> None:
        callers: list[str] = []
        for path in APP.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            allowed_funcs = _ALLOWED.get(path.resolve(), set())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = None
                if isinstance(func, ast.Attribute) and func.attr == "_touch_user_activity":
                    name = func.attr
                elif isinstance(func, ast.Name) and func.id == "_touch_user_activity":
                    name = func.id
                if name is None:
                    continue
                owner = _enclosing_function(tree, node)
                if owner in allowed_funcs:
                    continue
                callers.append(f"{path.relative_to(REPO)}:{node.lineno} in {owner}")
        self.assertEqual(
            callers,
            [],
            "_touch_user_activity gained a caller outside chat_turn_mixin; "
            "user_activity frames must not reset the idle gate",
        )


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    parent_fn = "<module>"
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _contains(node, target):
            parent_fn = node.name
    return parent_fn


def _contains(owner: ast.AST, target: ast.AST) -> bool:
    return any(child is target for child in ast.walk(owner))


if __name__ == "__main__":
    unittest.main()

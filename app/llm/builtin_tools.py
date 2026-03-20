"""Lightweight built-in tools for the LangChain agent.

Each tool is deterministic — the model only decides WHEN to call them.
"""
from __future__ import annotations

import ast
import operator
import platform
from datetime import datetime
from pathlib import Path
from typing import Any


def _make_tools(chat_db: Any = None, db_path: Path | None = None) -> list[Any]:
    """Return a list of LangChain @tool instances."""
    try:
        from langchain_core.tools import tool
    except ImportError:
        return []

    tools: list[Any] = []

    # ── Time / Date ──

    @tool
    def get_current_datetime() -> str:
        """Get the current local date, time, day of week, and timezone.
        Use this whenever the user asks what time or date it is."""
        now = datetime.now().astimezone()
        return (
            f"Date: {now.strftime('%A, %B %d, %Y')}\n"
            f"Time: {now.strftime('%I:%M %p')}\n"
            f"Timezone: {now.strftime('%Z (UTC%z)')}"
        )

    tools.append(get_current_datetime)

    # ── Calculator ──

    _SAFE_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _safe_eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp):
            op = _SAFE_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            left = _safe_eval(node.left)
            right = _safe_eval(node.right)
            if isinstance(node.op, ast.Pow) and right > 1000:
                raise ValueError("Exponent too large")
            return op(left, right)
        if isinstance(node, ast.UnaryOp):
            op = _SAFE_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported unary: {type(node.op).__name__}")
            return op(_safe_eval(node.operand))
        raise ValueError(f"Unsupported expression: {ast.dump(node)}")

    @tool
    def calculate(expression: str) -> str:
        """Evaluate a math expression and return the result.
        Supports: + - * / // % ** and parentheses.
        Example: calculate('(12 + 8) * 3.5')"""
        try:
            tree = ast.parse(expression.strip(), mode="eval")
            result = _safe_eval(tree)
            if result == int(result):
                return str(int(result))
            return f"{result:.10g}"
        except Exception as exc:
            return f"Error: {exc}"

    tools.append(calculate)

    # ── System Info ──

    @tool
    def get_system_info() -> str:
        """Get basic system information: OS, hostname, CPU architecture, and Python version."""
        import os
        try:
            import psutil
            mem = psutil.virtual_memory()
            ram = f"{mem.total / (1024**3):.1f} GB total, {mem.percent}% used"
        except ImportError:
            ram = "unknown (psutil not installed)"
        return (
            f"OS: {platform.system()} {platform.release()} ({platform.machine()})\n"
            f"Hostname: {platform.node()}\n"
            f"CPU: {platform.processor() or 'unknown'}\n"
            f"RAM: {ram}\n"
            f"Python: {platform.python_version()}\n"
            f"Working directory: {os.getcwd()}"
        )

    tools.append(get_system_info)

    # ── Notes / Reminders ──

    if chat_db is not None:
        _notes_db = chat_db

        @tool
        def save_note(text: str) -> str:
            """Save a note or reminder. ONLY use when the user explicitly says
            'remember this', 'save this', 'remind me', or 'make a note'.
            Do NOT call this during normal conversation."""
            try:
                conn = _notes_db._get_conn()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS user_notes "
                    "(id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL)",
                )
                from datetime import datetime as _dt
                conn.execute(
                    "INSERT INTO user_notes (content, created_at) VALUES (?, ?)",
                    (text.strip(), _dt.now().isoformat()),
                )
                conn.commit()
                return f"Saved note: {text.strip()[:100]}"
            except Exception as exc:
                return f"Failed to save note: {exc}"

        @tool
        def list_notes() -> str:
            """List all saved notes and reminders."""
            try:
                conn = _notes_db._get_conn()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS user_notes "
                    "(id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL)",
                )
                rows = conn.execute(
                    "SELECT content, created_at FROM user_notes ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
                if not rows:
                    return "No saved notes yet."
                parts = []
                for content, created in rows:
                    parts.append(f"- [{created[:16]}] {content}")
                return "\n".join(parts)
            except Exception as exc:
                return f"Error reading notes: {exc}"

        tools.extend([save_note, list_notes])

    # ── Clipboard ──

    @tool
    def read_clipboard() -> str:
        """Read the current text from the system clipboard.
        Use when the user asks about something they copied."""
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                return "No clipboard available (app not running)."
            text = app.clipboard().text()
            if not text:
                return "Clipboard is empty."
            if len(text) > 2000:
                return f"Clipboard ({len(text)} chars, truncated):\n{text[:2000]}..."
            return f"Clipboard:\n{text}"
        except Exception as exc:
            return f"Could not read clipboard: {exc}"

    tools.append(read_clipboard)

    return tools

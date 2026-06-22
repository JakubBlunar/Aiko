"""One-shot mechanical splitter for app/mcp/server.py (Phase 2b).

create_mcp_server is a flat sequence of @mcp.tool()/@mcp.resource() nested
defs closing over (mcp, session). We move contiguous ranges into
app/mcp/server_tools/<domain>.py each exposing register(mcp, session), and
turn create_mcp_server into a thin orchestrator that calls them in order.
Behavior-preserving: same decorators run on the same mcp object.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path("app/mcp/server.py")
lines = SRC.read_text(encoding="utf-8").splitlines()

# (module_name, start_line, end_line) 1-indexed inclusive; start lines are
# @mcp. decorator lines, ranges are contiguous and cover 33..6011.
GROUPS = [
    ("core_tools", 33, 1036),
    ("memory_worker_tools", 1037, 2109),
    ("self_state_tools", 2110, 3166),
    ("emotion_touch_tools", 3167, 3914),
    ("proactive_task_tools", 3915, 5371),
    ("resource_file_tools", 5372, 6011),
]

# sanity: every group starts on an @mcp. decorator, return mcp follows the last
for _name, a, _b in GROUPS:
    assert lines[a - 1].strip().startswith("@mcp."), (a, lines[a - 1])
assert lines[6011].strip() == "return mcp", lines[6011]


def make_module(seg: list[str]) -> str:
    text = "\n".join(seg)
    need_json = "json" in text
    need_any = re.search(r"\bAny\b", text) is not None
    need_log = re.search(r"\blog\b", text) is not None
    hdr = ["from __future__ import annotations", ""]
    if need_json:
        hdr.append("import json")
    if need_log:
        hdr.append("import logging")
    hdr.append("from typing import TYPE_CHECKING" + (", Any" if need_any else ""))
    hdr += [
        "",
        "if TYPE_CHECKING:",
        "    from app.core.session.session_controller import SessionController",
        "",
    ]
    if need_log:
        hdr += ['', 'log = logging.getLogger("app.mcp.server")']
    hdr += [
        "",
        "",
        'def register(mcp, session: "SessionController") -> None:',
    ]
    body = hdr + seg + [""]
    text_out = "\n".join(body) + "\n"
    ast.parse(text_out)
    return text_out


pkg = Path("app/mcp/server_tools")
pkg.mkdir(exist_ok=True)
(pkg / "__init__.py").write_text(
    '"""Domain-grouped MCP debug tool registrations for create_mcp_server."""\n',
    encoding="utf-8",
)

for name, a, b in GROUPS:
    seg = lines[a - 1 : b]
    (pkg / f"{name}.py").write_text(make_module(seg), encoding="utf-8")
    print(f"{name}.py", b - a + 1, "tool-lines")

# --- rewrite server.py ----------------------------------------------------
new_server = lines[0:32] + [
    "    from app.mcp.server_tools import (",
    "        core_tools,",
    "        memory_worker_tools,",
    "        self_state_tools,",
    "        emotion_touch_tools,",
    "        proactive_task_tools,",
    "        resource_file_tools,",
    "    )",
    "",
    "    core_tools.register(mcp, session)",
    "    memory_worker_tools.register(mcp, session)",
    "    self_state_tools.register(mcp, session)",
    "    emotion_touch_tools.register(mcp, session)",
    "    proactive_task_tools.register(mcp, session)",
    "    resource_file_tools.register(mcp, session)",
    "    return mcp",
    "",
]
text_out = "\n".join(new_server) + "\n"
ast.parse(text_out)
SRC.write_text(text_out, encoding="utf-8")
print("server.py", len(new_server), "lines (was", len(lines), ")")

"""One-shot splitter for app/web/server.py REST routes (Phase 2d).

create_web_app's REST route region (767..3366) closes over only these
factory locals: app, session, hub, _broadcast_context_window, live_session
(verified via compiled co_freevars). All intra-region shared locals
(get_settings<-patch_settings, _hidden_* task state, _MAX_DOCUMENT_UPLOAD_BYTES)
are self-contained within their cluster, so cutting at 1991 and 2639 keeps
each cluster whole. We move the three slices into app/web/rest/*.py each
exposing register(app, session, hub, _broadcast_context_window, live_session)
with verbatim route bodies; create_web_app calls them in order.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path("app/web/server.py")
lines = SRC.read_text(encoding="utf-8").splitlines()

GROUPS = [
    ("sessions_settings_routes", 766, 1990),
    ("memory_world_routes", 1991, 2638),
    ("tasks_files_routes", 2639, 3366),
]

# sanity anchors (M1 starts on the @app decorator, not the def)
assert lines[765].strip() == '@app.get("/api/sessions")', lines[765]
assert lines[1990].lstrip().startswith("def _on_memory_updated"), lines[1990]
assert lines[2638].lstrip().startswith("def _resolve_task_user_id"), lines[2638]
assert lines[3366].strip().startswith("# ") and "Avatar" in lines[3366], lines[3366]


def build_module(name: str, body: list[str]) -> str:
    t = "\n".join(body)
    h: list[str] = ["from __future__ import annotations", ""]
    # stdlib
    if re.search(r"\basyncio\b", t):
        h.append("import asyncio")
    if re.search(r"\bjson\b", t):
        h.append("import json")
    h.append("import logging")
    if re.search(r"\bthreading\b", t):
        h.append("import threading")
    if re.search(r"\btime\.", t) or "monotonic" in t:
        h.append("import time")
    typ = []
    if re.search(r"\bAny\b", t):
        typ.append("Any")
    if typ:
        h.append("from typing import " + ", ".join(typ))
    # fastapi
    fa = []
    if re.search(r"=\s*File\(", t) or re.search(r"\bUploadFile\b", t):
        fa += ["File", "UploadFile"]
    if "HTTPException" in t:
        fa.append("HTTPException")
    if fa:
        h.append("from fastapi import " + ", ".join(sorted(set(fa))))
    resp = []
    if "FileResponse" in t:
        resp.append("FileResponse")
    if "JSONResponse" in t:
        resp.append("JSONResponse")
    if resp:
        h.append("from fastapi.responses import " + ", ".join(resp))
    # app modules
    if "crash_logging" in t:
        h.append("from app.core.infra import crash_logging")
    setn = []
    if "OUTFIT_MODES" in t:
        setn.append("OUTFIT_MODES")
    if "_parse_grounding_line_mode" in t:
        setn.append("_parse_grounding_line_mode")
    if "persist_user_overrides" in t:
        setn.append("persist_user_overrides")
    if setn:
        h.append("from app.core.infra.settings import " + ", ".join(setn))
    # server.py module-level helpers (lazy-safe: rest modules import only
    # happens when create_web_app is *called*, well after server.py loads)
    srv = []
    if "_classify_test_error" in t:
        srv.append("_classify_test_error")
    if "_search_public_snapshot" in t:
        srv.append("_search_public_snapshot")
    if re.search(r"\b_trim\b", t):
        srv.append("_trim")
    if srv:
        h.append("from app.web.server import " + ", ".join(srv))
    h += [
        "",
        "",
        'log = logging.getLogger("app.web.server")',
        "",
        "",
        "def register(app, session, hub, _broadcast_context_window, live_session) -> None:",
        f'    """REST routes: {name.replace("_", " ")}."""',
    ]
    out = "\n".join(h + body + [""]) + "\n"
    ast.parse(out)
    return out


pkg = Path("app/web/rest")
pkg.mkdir(exist_ok=True)
(pkg / "__init__.py").write_text(
    '"""Feature-grouped REST route registrations for create_web_app."""\n',
    encoding="utf-8",
)
for name, a, b in GROUPS:
    body = lines[a - 1 : b]
    (pkg / f"{name}.py").write_text(build_module(name, body), encoding="utf-8")
    print(f"{name}.py", b - a + 1, "route-lines")

# rewrite create_web_app: keep 1-765, inject register calls, keep 3367-end
new = lines[0:765] + [
    "    from app.web.rest import (",
    "        sessions_settings_routes,",
    "        memory_world_routes,",
    "        tasks_files_routes,",
    "    )",
    "",
    "    sessions_settings_routes.register(",
    "        app, session, hub, _broadcast_context_window, live_session",
    "    )",
    "    memory_world_routes.register(",
    "        app, session, hub, _broadcast_context_window, live_session",
    "    )",
    "    tasks_files_routes.register(",
    "        app, session, hub, _broadcast_context_window, live_session",
    "    )",
    "",
] + lines[3366:]
text = "\n".join(new) + "\n"
ast.parse(text)
SRC.write_text(text, encoding="utf-8")
print("server.py", len(new), "lines (was", len(lines), ")")

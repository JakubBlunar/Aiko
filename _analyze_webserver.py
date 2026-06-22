"""Analyze create_web_app to plan a safe route-module split.

Finds create_web_app's top-level statements, classifies them, and computes the
free names used by the REST-route region (so we know exactly what each new
module must import and which factory-locals must be passed in).
"""
from __future__ import annotations

import ast
from pathlib import Path

src = Path("app/web/server.py").read_text(encoding="utf-8")
tree = ast.parse(src)

fn = next(
    n for n in tree.body
    if isinstance(n, ast.FunctionDef) and n.name == "create_web_app"
)

# module-level names defined in server.py (imports + top-level defs)
module_names: set[str] = set()
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        module_names.add(n.name)
    elif isinstance(n, ast.Import):
        for a in n.names:
            module_names.add((a.asname or a.name).split(".")[0])
    elif isinstance(n, ast.ImportFrom):
        for a in n.names:
            module_names.add(a.asname or a.name)
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                module_names.add(t.id)

# top-level statements of create_web_app, with line spans + kind
print("=== create_web_app top-level statements ===")
stmts = []
for st in fn.body:
    kind = type(st).__name__
    label = ""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
        decs = [ast.unparse(d) for d in st.decorator_list]
        label = f"def {st.name}  decs={decs}"
    elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
        label = ast.unparse(st.value)[:70]
    elif isinstance(st, ast.Assign):
        label = ast.unparse(st)[:70]
    elif isinstance(st, ast.Return):
        label = "return " + ast.unparse(st.value)[:40]
    elif isinstance(st, (ast.If, ast.With, ast.Try)):
        label = kind + " " + ast.unparse(st).splitlines()[0][:60]
    stmts.append((st.lineno, st.end_lineno, kind, label))

# --- free-name analysis of the route region [767, 3374] ------------------
REGION_LO, REGION_HI = 767, 3374
region_stmts = [st for st in fn.body if st.lineno >= REGION_LO and st.end_lineno <= REGION_HI]

bound: set[str] = set()
for st in region_stmts:
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bound.add(st.name)
    elif isinstance(st, ast.Assign):
        for t in st.targets:
            for nn in ast.walk(t):
                if isinstance(nn, ast.Name):
                    bound.add(nn.id)

loads: dict[str, int] = {}
for st in region_stmts:
    for nn in ast.walk(st):
        if isinstance(nn, ast.Name) and isinstance(nn.ctx, ast.Load):
            loads[nn.id] = loads.get(nn.id, 0) + 1

import builtins as _b
builtin_names = set(dir(_b))
factory_locals = {"app", "session", "hub", "_broadcast_context_window", "live_session"}

free = {}
for name, cnt in loads.items():
    if name in bound or name in builtin_names:
        continue
    free[name] = cnt

print("\n=== factory-locals referenced in region ===")
for n in sorted(factory_locals):
    if n in free:
        print(f"  {n}: {free[n]}")

print("\n=== module-level names referenced in region (need import) ===")
for n in sorted(free):
    if n in factory_locals:
        continue
    tag = "MODULE" if n in module_names else "??? UNKNOWN"
    print(f"  {n}: {free[n]:4}  [{tag}]")

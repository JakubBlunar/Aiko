"""Precise closure/global analysis via compiled code objects.

co_freevars of a nested route fn = exactly the create_web_app locals it
closes over. co_names = module globals it touches. We union these across all
route fns in [767,3374] to learn the register() signature + import set.
"""
from __future__ import annotations

import ast
from pathlib import Path

src = Path("app/web/server.py").read_text(encoding="utf-8")
code = compile(src, "server.py", "exec")


def find_code(co, name):
    for c in co.co_consts:
        if hasattr(c, "co_name") and c.co_name == name:
            return c
    return None


create = find_code(code, "create_web_app")
assert create is not None

REGION_LO, REGION_HI = 767, 3374

freevars: dict[str, int] = {}
globals_used: dict[str, int] = {}
route_fns = []
for c in create.co_consts:
    if not hasattr(c, "co_name"):
        continue
    ln = c.co_firstlineno
    if not (REGION_LO <= ln <= REGION_HI):
        continue
    route_fns.append((ln, c.co_name))
    for fv in c.co_freevars:
        freevars[fv] = freevars.get(fv, 0) + 1
    for gn in c.co_names:
        globals_used[gn] = globals_used.get(gn, 0) + 1

print("route/closure fns in region:", len(route_fns))
print("\n=== co_freevars (must be register() params) ===")
for n in sorted(freevars):
    print(f"  {n}: {freevars[n]}")

# module-level names defined in server.py
tree = ast.parse(src)
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

import builtins as _b
builtin_names = set(dir(_b))

print("\n=== module globals used by region (need import in new modules) ===")
for n in sorted(globals_used):
    if n in builtin_names:
        continue
    if n in module_names:
        print(f"  {n}: {globals_used[n]:4}  [server.py module-level]")
    else:
        print(f"  {n}: {globals_used[n]:4}  [attr/global - likely fine]")

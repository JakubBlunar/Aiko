"""Static audit: no ``demand()`` probe may mutate.

A probe runs on every scheduler tick for every worker, so a write
inside one is both a correctness bug (it happens whether or not the
worker is admitted) and invisible in normal use. The suite catches it
per worker; this catches it across the registry, including workers
added later.

Run: ``python scripts/audit_demand_probes.py``. Exits non-zero on a hit.
"""
from __future__ import annotations

import ast
import pathlib
import sys

# Method names that write, spend a budget, or call a model. Matched on
# the attribute name alone, so a false positive is possible and a
# deliberate exception belongs in ALLOW below rather than here.
MUTATORS = frozenset({
    "add", "add_goal", "add_message", "allow", "chat", "chat_json",
    "chat_stream", "delete", "enqueue", "kv_set", "pop", "promote_stage",
    "save_thread_note", "set_cluster_label", "update", "upsert", "write",
})

# Deliberate exceptions. A probe-local memo is not a mutation in the
# sense that matters — it touches no store and changes no decision — and
# is the whole reason the common case costs a string compare instead of
# a kv read on every tick.
ALLOW = frozenset({
    ("MoodDriftSampleWorker", "_sampled_date"),
})


def audit(root: pathlib.Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                if fn.name != "demand":
                    continue
                findings.extend(_scan(path, cls, fn))
    return findings


def _scan(
    path: pathlib.Path, cls: ast.ClassDef, fn: ast.FunctionDef,
) -> list[str]:
    out: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in MUTATORS and (cls.name, attr) not in ALLOW:
                out.append(
                    f"{path}:{node.lineno}: {cls.name}.demand calls .{attr}()"
                )
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and (cls.name, target.attr) not in ALLOW
            ):
                out.append(
                    f"{path}:{target.lineno}: "
                    f"{cls.name}.demand assigns self.{target.attr}"
                )
    return out


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "core"
    findings = audit(root)
    probes = sum(
        1
        for path in root.rglob("*.py")
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if isinstance(node, ast.FunctionDef) and node.name == "demand"
    )
    print(f"scanned {probes} demand() probes")
    for line in findings:
        print(line)
    print(f"{len(findings)} suspect write(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

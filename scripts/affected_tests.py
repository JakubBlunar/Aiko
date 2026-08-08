#!/usr/bin/env python
"""Pick the test files affected by a set of changed files.

The suite is ~9,000 tests across 420 files and takes ~11 minutes, which is
too slow to sit in an edit loop. Almost all of that work is irrelevant to
any one change: touching ``app/core/relationship/relationship.py`` can only
break tests that reach it, and that reachability is already written down in
the codebase's own import statements.

So: parse every first-party module with ``ast``, build the *reverse* import
graph (module -> the modules that import it), and walk it outward from the
changed files. Whatever lands in ``tests/`` is the set worth running.

Static analysis, deliberately -- no coverage instrumentation, no recorded
baseline, no plugin. It reads the tree as it is right now, so it can't go
stale or disagree with a rebased branch, and it costs well under a second.
The tradeoff is the other direction: it can only see imports it can read.

**This is the inner-loop tool, not a release gate.** It over-selects
happily (a change to ``settings.py`` reaches nearly everything, which is
the honest answer) but it can under-select when a test reaches code by a
route that isn't an import: a plugin loaded by name, a subprocess, a
fixture that reads a data file. Run the full suite before you call
something done.

Usage::

    python scripts/affected_tests.py                 # vs working tree + HEAD
    python scripts/affected_tests.py --run           # ... and run them
    python scripts/affected_tests.py --run -- -x -q  # ... with pytest args
    python scripts/affected_tests.py --base main     # vs a branch point
    python scripts/affected_tests.py --files a.py b.py
    python scripts/affected_tests.py --explain       # why each was picked
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees whose imports are worth following. ``data/`` and ``web/`` are not
# Python; ``build/`` and ``.venv/`` are copies that would double every edge.
SOURCE_ROOTS = ("app", "tests", "scripts", "plugins", "tools")

TESTS_DIR = "tests"

# A change here invalidates the whole suite rather than any subset:
# conftest fixtures are autouse and session-scoped, so nothing is isolated
# from them.
GLOBAL_TRIGGERS = (
    "tests/conftest.py",
    "pyproject.toml",
    "requirements.lock",
)


# ── module graph ────────────────────────────────────────────────────────


def _module_name(path: Path) -> str | None:
    """Dotted import name for a repo-relative source path.

    ``app/core/foo.py`` -> ``app.core.foo``, ``app/core/__init__.py`` ->
    ``app.core``. Files under ``tests/`` are top-level modules, not a
    package (there is no ``tests/__init__.py``, and pytest puts the
    directory itself on ``sys.path``), so ``tests/test_x.py`` -> ``test_x``
    and a shared helper like ``tests/web_fake_session.py`` ->
    ``web_fake_session``.
    """
    if path.suffix != ".py":
        return None
    parts = list(path.parts)
    if not parts:
        return None
    if parts[0] == TESTS_DIR:
        parts = parts[1:]
        if not parts:
            return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts) if parts else None


def _iter_source_files() -> list[Path]:
    out: list[Path] = []
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out.append(path.relative_to(REPO_ROOT))
    return out


def _imports_of(path: Path) -> set[str]:
    """Dotted names imported by one file.

    ``from a.b import c`` yields both ``a.b`` and ``a.b.c`` -- ``c`` may be
    a submodule or just a name, and the resolver keeps whichever exists.
    Imports under ``if TYPE_CHECKING:`` count: they are real ast nodes, and
    they are exactly how the lazy-loading packages here still declare what
    they depend on.
    """
    try:
        tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()

    package_parts = _package_parts(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import: climb out of the current package.
                base = package_parts[: len(package_parts) - (node.level - 1)]
                prefix = list(base)
                if node.module:
                    prefix += node.module.split(".")
            elif node.module:
                prefix = node.module.split(".")
            else:
                continue
            if prefix:
                found.add(".".join(prefix))
            for alias in node.names:
                if alias.name != "*":
                    found.add(".".join([*prefix, alias.name]))
    return found


def _package_parts(path: Path) -> tuple[str, ...]:
    """The package a file lives in, as dotted parts (for relative imports)."""
    module = _module_name(path)
    if module is None:
        return ()
    parts = module.split(".")
    if path.name == "__init__.py":
        return tuple(parts)
    return tuple(parts[:-1])


def build_reverse_graph(
    files: list[Path] | None = None,
) -> dict[Path, set[Path]]:
    """Map each source file to the files that import it."""
    files = files if files is not None else _iter_source_files()
    by_module: dict[str, Path] = {}
    for path in files:
        module = _module_name(path)
        if module is not None:
            by_module[module] = path

    reverse: dict[Path, set[Path]] = {path: set() for path in files}
    for importer in files:
        for name in _imports_of(importer):
            target = by_module.get(name)
            if target is None:
                # ``from app.core.foo import Bar`` -- trim the trailing
                # attribute and try the module it came from.
                head = name.rsplit(".", 1)[0]
                target = by_module.get(head)
            if target is not None and target != importer:
                reverse[target].add(importer)
    return reverse


# ── selection ───────────────────────────────────────────────────────────


def _is_test_file(path: Path) -> bool:
    return path.parts[:1] == (TESTS_DIR,) and path.name.startswith("test_")


def affected_tests(
    changed: list[Path],
    *,
    reverse: dict[Path, set[Path]] | None = None,
    source_files: list[Path] | None = None,
) -> tuple[list[Path], dict[Path, Path]]:
    """Test files reachable from ``changed``, plus why each was picked.

    The second return value maps a selected test file to the changed file
    that pulled it in (the first one found), for ``--explain``.
    """
    files = source_files if source_files is not None else _iter_source_files()
    graph = reverse if reverse is not None else build_reverse_graph(files)

    origin: dict[Path, Path] = {}
    seen: set[Path] = set()
    queue: deque[tuple[Path, Path]] = deque()
    for path in changed:
        if path in graph or _is_test_file(path):
            queue.append((path, path))

    while queue:
        current, root = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        if _is_test_file(current):
            origin.setdefault(current, root)
        for importer in graph.get(current, ()):  # noqa: SIM118 -- .get default
            if importer not in seen:
                queue.append((importer, root))

    # Non-Python changes have no import edges. Fall back to a text search:
    # a data file's name shows up in the tests that read it, which is how
    # e.g. ``aiko_companion.txt`` finds the persona suites.
    for path in changed:
        if path.suffix == ".py":
            continue
        for test in _tests_mentioning(path, files):
            origin.setdefault(test, path)

    return sorted(origin), origin


def _tests_mentioning(path: Path, files: list[Path]) -> list[Path]:
    needle = path.name
    if not needle:
        return []
    out: list[Path] = []
    for candidate in files:
        if not _is_test_file(candidate):
            continue
        try:
            text = (REPO_ROOT / candidate).read_text(encoding="utf-8")
        except OSError:
            continue
        if needle in text:
            out.append(candidate)
    return out


# ── git ─────────────────────────────────────────────────────────────────


def changed_files(base: str | None = None) -> list[Path]:
    """Changed files: uncommitted work plus, with ``base``, the branch's commits."""
    args = ["diff", "--name-only", "HEAD"] if base is None else [
        "diff", "--name-only", f"{base}...HEAD",
    ]
    out = set(_git(args))
    out.update(_git(["ls-files", "--others", "--exclude-standard"]))
    if base is not None:
        out.update(_git(["diff", "--name-only", "HEAD"]))
    return sorted(Path(p) for p in out if p)


def _git(args: list[str]) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"warning: git {' '.join(args)} failed: {exc}", file=sys.stderr)
        return []
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines()]


# ── cli ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Select the test files affected by changed files.",
    )
    parser.add_argument(
        "--base",
        help="compare against this ref (branch point) as well as the working tree",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        type=Path,
        help="use these changed files instead of asking git",
    )
    parser.add_argument("--run", action="store_true", help="run pytest on the selection")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="show which changed file pulled in each test",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="extra args for pytest (after --)",
    )
    args = parser.parse_args(argv)

    changed = args.files if args.files else changed_files(args.base)
    changed = [Path(str(p).replace("\\", "/")) for p in changed]
    if not changed:
        print("no changed files")
        return 0

    print(f"changed files ({len(changed)}):")
    for path in changed:
        print(f"  {path}")

    triggers = [p for p in changed if str(p) in GLOBAL_TRIGGERS]
    if triggers:
        print()
        print(
            "these affect every test, so the selection would be the whole "
            f"suite: {', '.join(str(p) for p in triggers)}",
        )
        print("run: python -m pytest")
        return 0

    selected, origin = affected_tests(changed)
    print()
    if not selected:
        print("no test files reach these changes")
        print("(that may itself be worth fixing -- or the link isn't an import)")
        return 0

    print(f"affected test files ({len(selected)}):")
    for path in selected:
        if args.explain:
            print(f"  {path}   <- {origin[path]}")
        else:
            print(f"  {path}")

    if not args.run:
        print()
        print("run: python -m pytest " + " ".join(str(p) for p in selected))
        return 0

    extra = [a for a in args.pytest_args if a != "--"]
    cmd = [sys.executable, "-m", "pytest", *[str(p) for p in selected], *extra]
    print()
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())

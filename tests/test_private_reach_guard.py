"""Architectural guard: how far outer layers reach into ``SessionController``.

``SessionController`` wires every subsystem together, and the layers above it
grew by reaching through it rather than by asking it for anything: at the time
this guard was written ``app/web/`` and ``app/mcp/server_tools/`` between them
touched 636 private attributes on it. That is what makes the class hard to
change -- a rename is a silent breakage until some route 404s at runtime, and
the web tests use ``MagicMock`` sessions, which answer *any* attribute name
happily and so cannot notice.

The guard does two separate jobs.

**Correctness.** Every private name reached from outside must actually exist on
the controller. This is the part that turns a rename from a runtime surprise
into a failing test, and it is why the guard is worth having even before the
reach count comes down. It holds with no allowlist today.

**Ratchet.** Each package has a budget of permitted reaches that may only ever
decrease. Going over fails; coming under *also* fails, with the new number to
write down. Without the second half a package silently re-earns headroom every
time an unrelated cleanup removes a reach.

Ownership is computed from ``SessionController`` plus every ``*Mixin`` class in
``app/core/session/`` -- all 22 of its bases live there, so that set is exact.

Known blind spot: a reach is only recognised when the receiver is spelled
``session``. Aliasing the controller to another local would slip past. That is
how every call site is written today, and the correctness half of the guard
would still catch a rename at all the un-aliased sites.
"""
from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SESSION_PKG = ROOT / "app" / "core" / "session"

# Local variable names that hold the live controller at a call site.
RECEIVERS = frozenset({"session"})

# Reflective forms that name the attribute as a string literal; without these
# the ~60 ``getattr(session, "_x", default)`` debug reads would be invisible.
REFLECTIVE = frozenset({"getattr", "setattr", "hasattr", "delattr"})

# Reaches permitted per package. Lower these as conversions land; never raise
# them. ``app/web`` is at 0: routes have a facade to talk to, so they have no
# excuse. ``app/mcp/server_tools`` is a debug surface whose whole job is poking
# at internals, so it gets a budget rather than a ban. It came down from 569
# when the one-shot ``_force_*`` flags moved into ``session.debug_overrides``,
# then to 467 with the ``session.debug_clock`` accessor, then to 466 when
# P45 dropped the idle-seed daily-cap kv read; the rest waits on typed
# handle accessors for the subsystems.
BUDGETS: dict[str, int] = {
    "app/web": 0,
    "app/mcp/server_tools": 466,
}

MAX_REPORTED = 15


class Reach(NamedTuple):
    """A single private access, kept with its location for the failure message."""

    attr: str
    path: Path
    lineno: int

    def where(self) -> str:
        return f"{self.path.relative_to(ROOT).as_posix()}:{self.lineno}"


def _iter_python(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if p.is_file())


def _owned_names() -> set[str]:
    """Every private attribute and method the controller actually has.

    Collected from assignments (``self._x = ...``, annotated and augmented),
    ``setattr(self, "_x", ...)``, and ``def _x`` -- methods matter because a
    good share of what the MCP tools reach for are private helpers, not state.
    """
    names: set[str] = set()
    for path in _iter_python(SESSION_PKG):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            if cls.name != "SessionController" and not cls.name.endswith("Mixin"):
                continue
            for node in ast.walk(cls):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef),
                ) and node.name.startswith("_"):
                    names.add(node.name)
                    continue

                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr.startswith("_")
                    ):
                        names.add(target.attr)

                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "setattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "self"
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    names.add(node.args[1].value)
    return names


def _reaches_in(package: str) -> list[Reach]:
    """Every private access on a ``session`` receiver inside ``package``."""
    found: list[Reach] = []
    for path in _iter_python(ROOT / package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in RECEIVERS
                and node.attr.startswith("_")
            ):
                found.append(Reach(node.attr, path, node.lineno))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in REFLECTIVE
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in RECEIVERS
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value.startswith("_")
            ):
                found.append(Reach(node.args[1].value, path, node.lineno))
    return found


class OwnershipTests(unittest.TestCase):
    """Sanity-check the ownership scan before trusting it to judge reaches."""

    def test_finds_the_controller_and_all_its_mixins(self) -> None:
        owner_classes: set[str] = set()
        for path in _iter_python(SESSION_PKG):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and (
                    node.name == "SessionController" or node.name.endswith("Mixin")
                ):
                    owner_classes.add(node.name)
        self.assertIn("SessionController", owner_classes)
        # 22 bases at the time of writing; the scan must not silently collapse.
        self.assertGreaterEqual(len(owner_classes), 22)

    def test_ownership_includes_state_and_helpers(self) -> None:
        owned = _owned_names()
        # An attribute assigned in __init__ and a private helper method, so a
        # regression in either branch of the collector shows up here rather
        # than as a confusing allowlist failure below.
        self.assertIn("_settings", owned)
        self.assertIn("_notify_message", owned)
        self.assertGreater(len(owned), 500)


class PrivateReachGuardTests(unittest.TestCase):
    def test_every_reached_name_exists_on_the_controller(self) -> None:
        """A reach for a name the controller lacks is a rename already broken.

        Holds with no allowlist, so any failure here is a real find: either a
        controller attribute was renamed and a caller was missed, or a caller
        invented a name that never existed.
        """
        owned = _owned_names()
        orphans = [
            reach
            for package in BUDGETS
            for reach in _reaches_in(package)
            if reach.attr not in owned
        ]
        if orphans:
            listing = "\n".join(
                f"  {reach.attr} -- {reach.where()}"
                for reach in orphans[:MAX_REPORTED]
            )
            extra = (
                f"\n  ... and {len(orphans) - MAX_REPORTED} more"
                if len(orphans) > MAX_REPORTED
                else ""
            )
            self.fail(
                f"{len(orphans)} reach(es) name something SessionController does "
                f"not have. Either the attribute was renamed and these callers "
                f"were missed, or the name is a typo that has been silently "
                f"returning a default:\n{listing}{extra}",
            )

    def test_reach_counts_stay_within_budget(self) -> None:
        for package, budget in sorted(BUDGETS.items()):
            with self.subTest(package=package):
                reaches = _reaches_in(package)
                actual = len(reaches)
                if actual > budget:
                    worst = Counter(r.attr for r in reaches).most_common(5)
                    self.fail(
                        f"{package} now makes {actual} private reaches into "
                        f"SessionController, over its budget of {budget}. Add a "
                        f"public method to the controller instead of reaching "
                        f"through it. Most-reached names: {worst}",
                    )
                self.assertEqual(
                    actual,
                    budget,
                    f"{package} is down to {actual} private reaches (budget "
                    f"{budget}) -- lower BUDGETS['{package}'] to {actual} so the "
                    f"progress is locked in.",
                )

    def test_web_budget_stays_at_zero(self) -> None:
        """``app/web`` is converted; every route goes through the facade.

        Pinned separately so re-opening the door is an obvious edit to a test
        that says not to, rather than a plausible-looking number in a dict.
        """
        self.assertEqual(BUDGETS["app/web"], 0)


if __name__ == "__main__":
    unittest.main()

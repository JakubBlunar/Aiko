"""Tests for ``scripts.affected_tests``.

The selector decides what *not* to run, so its failure mode is silence: an
under-selection doesn't error, it just quietly skips the test that would
have caught the bug. The cases below therefore pin the reachability rules
themselves against a synthetic source tree -- one directional claim per
test, including the two indirect routes that are easy to lose (a relative
import, and an import that only exists under ``if TYPE_CHECKING:``, which
is how the lazy-loading packages here declare their dependencies).
"""
from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import affected_tests as at


class _Tree:
    """A throwaway repo: write files, then resolve against them."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def write(self, rel: str, text: str = "") -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")

    def selected(self, *changed: str) -> set[str]:
        with patch.object(at, "REPO_ROOT", self.root):
            picked, _ = at.affected_tests([Path(c) for c in changed])
        return {p.as_posix() for p in picked}

    def cleanup(self) -> None:
        self._tmp.cleanup()


class ModuleNameTests(unittest.TestCase):
    def test_app_module_is_dotted_from_the_repo_root(self) -> None:
        self.assertEqual(
            at._module_name(Path("app/core/relationship/relationship.py")),
            "app.core.relationship.relationship",
        )

    def test_package_init_names_the_package(self) -> None:
        self.assertEqual(at._module_name(Path("app/core/__init__.py")), "app.core")

    def test_a_test_module_is_top_level(self) -> None:
        # tests/ has no __init__.py and pytest puts it on sys.path, so the
        # importable name drops the directory.
        self.assertEqual(at._module_name(Path("tests/test_x.py")), "test_x")

    def test_a_shared_test_helper_is_top_level_too(self) -> None:
        self.assertEqual(
            at._module_name(Path("tests/web_fake_session.py")), "web_fake_session"
        )

    def test_non_python_has_no_module_name(self) -> None:
        self.assertIsNone(at._module_name(Path("data/persona/aiko.txt")))


class ReachabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = _Tree()
        self.addCleanup(self.tree.cleanup)

    def test_a_direct_importer_is_selected(self) -> None:
        self.tree.write("app/core/leaf.py", "VALUE = 1\n")
        self.tree.write(
            "tests/test_leaf.py", "from app.core.leaf import VALUE\n"
        )
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_leaf.py"}
        )

    def test_an_unrelated_test_is_not_selected(self) -> None:
        self.tree.write("app/core/leaf.py", "VALUE = 1\n")
        self.tree.write("app/core/other.py", "OTHER = 2\n")
        self.tree.write("tests/test_leaf.py", "from app.core.leaf import VALUE\n")
        self.tree.write("tests/test_other.py", "from app.core.other import OTHER\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_leaf.py"}
        )

    def test_reachability_is_transitive(self) -> None:
        # The common shape: a test imports a controller, the controller
        # imports the module that changed.
        self.tree.write("app/core/leaf.py", "VALUE = 1\n")
        self.tree.write("app/core/mid.py", "from app.core.leaf import VALUE\n")
        self.tree.write("tests/test_mid.py", "import app.core.mid\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_mid.py"}
        )

    def test_a_relative_import_still_links(self) -> None:
        self.tree.write("app/core/__init__.py")
        self.tree.write("app/core/leaf.py", "VALUE = 1\n")
        self.tree.write("app/core/mid.py", "from .leaf import VALUE\n")
        self.tree.write("tests/test_mid.py", "from app.core.mid import VALUE\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_mid.py"}
        )

    def test_a_type_checking_import_counts(self) -> None:
        # app/core/session/__init__.py resolves its exports lazily at
        # runtime and declares them under TYPE_CHECKING. If those didn't
        # count, a change to a mixin would reach none of its tests.
        self.tree.write("app/core/session/__init__.py", """
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                from .voice_mixin import VoiceMixin
        """)
        self.tree.write("app/core/session/voice_mixin.py", "class VoiceMixin: ...\n")
        self.tree.write("tests/test_session.py", "import app.core.session\n")
        self.assertEqual(
            self.tree.selected("app/core/session/voice_mixin.py"),
            {"tests/test_session.py"},
        )

    def test_a_shared_test_helper_pulls_in_its_users(self) -> None:
        self.tree.write("tests/web_fake_session.py", "class Fake: ...\n")
        self.tree.write("tests/test_a.py", "from web_fake_session import Fake\n")
        self.tree.write("tests/test_b.py", "from web_fake_session import Fake\n")
        self.tree.write("tests/test_c.py", "VALUE = 1\n")
        self.assertEqual(
            self.tree.selected("tests/web_fake_session.py"),
            {"tests/test_a.py", "tests/test_b.py"},
        )

    def test_a_changed_test_selects_itself(self) -> None:
        self.tree.write("tests/test_solo.py", "VALUE = 1\n")
        self.assertEqual(
            self.tree.selected("tests/test_solo.py"), {"tests/test_solo.py"}
        )

    def test_a_syntax_error_does_not_abort_the_walk(self) -> None:
        self.tree.write("app/core/leaf.py", "VALUE = 1\n")
        self.tree.write("app/core/broken.py", "def (:\n")
        self.tree.write("tests/test_leaf.py", "from app.core.leaf import VALUE\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_leaf.py"}
        )


class NonPythonTests(unittest.TestCase):
    """Data files have no import edges, so the name is the only link."""

    def setUp(self) -> None:
        self.tree = _Tree()
        self.addCleanup(self.tree.cleanup)

    def test_a_data_file_selects_the_tests_that_name_it(self) -> None:
        self.tree.write("data/persona/aiko_companion.txt", "persona text\n")
        self.tree.write(
            "tests/test_persona.py",
            'PATH = "data/persona/aiko_companion.txt"\n',
        )
        self.tree.write("tests/test_unrelated.py", "VALUE = 1\n")
        self.assertEqual(
            self.tree.selected("data/persona/aiko_companion.txt"),
            {"tests/test_persona.py"},
        )

    def test_a_frontend_file_selects_nothing(self) -> None:
        # Vitest owns web/; pytest has nothing to say about it.
        self.tree.write("web/src/lib/time.ts", "export const x = 1;\n")
        self.tree.write("tests/test_unrelated.py", "VALUE = 1\n")
        self.assertEqual(self.tree.selected("web/src/lib/time.ts"), set())


class GlobalTriggerTests(unittest.TestCase):
    def test_conftest_is_a_whole_suite_trigger(self) -> None:
        # Its fixtures are autouse and session-scoped, so no subset is
        # honestly isolated from a change to it.
        self.assertIn("tests/conftest.py", at.GLOBAL_TRIGGERS)


if __name__ == "__main__":
    unittest.main()

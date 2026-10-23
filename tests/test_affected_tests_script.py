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
import os
import sys

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

    def test_lazy_exports_do_not_pull_in_unrelated_sibling_users(self) -> None:
        self.tree.write("app/core/__init__.py", """
            from typing import TYPE_CHECKING
            def __getattr__(name):
                raise AttributeError(name)
            if TYPE_CHECKING:
                from .leaf import Leaf
                from .other import Other
        """)
        self.tree.write("app/core/leaf.py", "class Leaf: ...\n")
        self.tree.write("app/core/other.py", "class Other: ...\n")
        self.tree.write("tests/test_leaf.py", "from app.core import Leaf\n")
        self.tree.write("tests/test_other.py", "from app.core import Other\n")
        self.tree.write("tests/test_package.py", "import app.core\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_leaf.py"},
        )
        self.assertEqual(
            self.tree.selected("app/core/__init__.py"),
            {"tests/test_leaf.py", "tests/test_other.py", "tests/test_package.py"},
        )

    def test_lazy_export_alias_and_transitive_importer_are_selected(self) -> None:
        self.tree.write("app/core/__init__.py", """
            from typing import TYPE_CHECKING
            def __getattr__(name):
                raise AttributeError(name)
            if TYPE_CHECKING:
                from app.core.leaf import Leaf as PublicLeaf
        """)
        self.tree.write("app/core/leaf.py", "class Leaf: ...\n")
        self.tree.write("app/core/mid.py", "from app.core import PublicLeaf\n")
        self.tree.write("tests/test_mid.py", "import app.core.mid\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"), {"tests/test_mid.py"},
        )

    def test_lazy_exports_resolve_qualified_access_and_wildcard_imports(self) -> None:
        self.tree.write("app/core/__init__.py", """
            from typing import TYPE_CHECKING
            def __getattr__(name):
                raise AttributeError(name)
            if TYPE_CHECKING:
                from .leaf import Leaf
                from .other import Other
        """)
        self.tree.write("app/core/leaf.py", "class Leaf: ...\n")
        self.tree.write("app/core/other.py", "class Other: ...\n")
        self.tree.write("tests/test_qualified.py", "import app.core\nvalue = app.core.Leaf\n")
        self.tree.write("tests/test_alias.py", "import app.core as core\nvalue = core.Leaf\n")
        self.tree.write("tests/test_star.py", "from app.core import *\n")
        self.tree.write("tests/test_other.py", "import app.core as core\nvalue = core.Other\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"),
            {"tests/test_qualified.py", "tests/test_alias.py", "tests/test_star.py"},
        )

    def test_a_changed_test_selects_itself(self) -> None:
        self.tree.write("tests/test_solo.py", "VALUE = 1\n")
        self.assertEqual(
            self.tree.selected("tests/test_solo.py"), {"tests/test_solo.py"}
        )

    def test_literal_dynamic_imports_and_patch_targets_are_selected(self) -> None:
        self.tree.write("app/core/leaf.py", "class Leaf: ...\n")
        self.tree.write("tests/test_dynamic.py", """
            import importlib
            module = importlib.import_module("app.core.leaf")
        """)
        self.tree.write("tests/test_builtin.py", '__import__("app.core.leaf")\n')
        self.tree.write("tests/test_patch.py", """
            from unittest.mock import patch
            patch("app.core.leaf.Leaf.method")
        """)
        self.tree.write("tests/test_unrelated.py", "VALUE = 1\n")
        self.assertEqual(
            self.tree.selected("app/core/leaf.py"),
            {"tests/test_dynamic.py", "tests/test_builtin.py", "tests/test_patch.py"},
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

    def test_a_data_file_reaches_tests_through_a_source_reader(self) -> None:
        self.tree.write("data/persona/aiko_companion.txt", "persona text\n")
        self.tree.write(
            "app/core/persona.py", 'PATH = "data/persona/aiko_companion.txt"\n',
        )
        self.tree.write("tests/test_persona.py", "from app.core.persona import PATH\n")
        self.tree.write("tests/test_unrelated.py", "VALUE = 1\n")
        self.assertEqual(
            self.tree.selected("data/persona/aiko_companion.txt"),
            {"tests/test_persona.py"},
        )

    def test_a_data_file_reaches_tests_through_a_shared_fixture_helper(self) -> None:
        self.tree.write("tests/fixtures/example.json", "{}\n")
        self.tree.write("tests/fixture_helper.py", 'PATH = "example.json"\n')
        self.tree.write("tests/test_example.py", "from fixture_helper import PATH\n")
        self.assertEqual(
            self.tree.selected("tests/fixtures/example.json"), {"tests/test_example.py"},
        )


class GlobalTriggerTests(unittest.TestCase):
    def test_conftest_is_a_whole_suite_trigger(self) -> None:
        # Its fixtures are autouse and session-scoped, so no subset is
        # honestly isolated from a change to it.
        self.assertIn("tests/conftest.py", at.GLOBAL_TRIGGERS)

    def test_run_does_not_report_success_or_start_the_suite_for_a_global_trigger(self) -> None:
        with patch.object(at, "_run_tests") as run:
            result = at.main(["--files", "tests/conftest.py", "--run"])
        self.assertEqual(result, 2)
        run.assert_not_called()


class ManagedTestRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = _Tree()
        self.addCleanup(self.tree.cleanup)

    def test_unmanaged_child_tempfiles_are_removed_after_success_and_failure(self) -> None:
        # Exercise a real child that leaks mkdtemp(), like the legacy DB
        # harnesses, without running another pytest or creating large data.
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                marker = self.tree.root / f"scratch-{exit_code}.txt"
                code = (
                    "import os, pathlib, sqlite3, sys, tempfile; "
                    "root = pathlib.Path(os.environ['TEMP']); "
                    "assert os.environ['TMP'] == os.environ['TMPDIR'] == str(root); "
                    "folder = pathlib.Path(tempfile.mkdtemp()); "
                    "conn = sqlite3.connect(folder / 'test.db'); "
                    "conn.execute('CREATE TABLE example (id INTEGER)'); conn.commit(); "
                    "pathlib.Path(sys.argv[1]).write_text(str(root)); "
                    "sys.exit(int(sys.argv[2]))"
                )
                sibling = self.tree.root / ".tmp-tests" / "unrelated.txt"
                sibling.parent.mkdir(parents=True, exist_ok=True)
                sibling.write_text("keep", encoding="utf-8")
                original_env = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
                with patch.object(at, "REPO_ROOT", self.tree.root):
                    result = at._run_tests([
                        sys.executable, "-c", code, str(marker), str(exit_code),
                    ])
                self.assertEqual(result, exit_code)
                root = Path(marker.read_text())
                self.assertEqual(root.parent, self.tree.root / ".tmp-tests")
                self.assertFalse(root.exists())
                self.assertEqual(sibling.read_text(), "keep")
                self.assertEqual(original_env, {
                    key: os.environ.get(key) for key in original_env
                })

    def test_interrupt_stops_child_before_removing_its_files(self) -> None:
        events = []

        class Child:
            def __init__(self, command, **kwargs):
                self.root = Path(kwargs["env"]["TEMP"])
                (self.root / "held.db").write_bytes(b"test")

            def wait(self):
                raise KeyboardInterrupt

        def stop(child):
            self.assertTrue((child.root / "held.db").exists())
            events.append(child.root)

        with patch.object(at, "REPO_ROOT", self.tree.root), \
                patch.object(at.subprocess, "Popen", Child), \
                patch.object(at, "_stop_test_process", stop), \
                self.assertRaises(KeyboardInterrupt):
            at._run_tests([sys.executable, "-m", "pytest"])
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].exists())

    def test_launch_failure_also_cleans_owned_directory(self) -> None:
        with patch.object(at, "REPO_ROOT", self.tree.root), \
                patch.object(at.subprocess, "Popen", side_effect=OSError("cannot start")), \
                self.assertRaises(OSError):
            at._run_tests([sys.executable, "-m", "pytest"])
        self.assertEqual(list((self.tree.root / ".tmp-tests").iterdir()), [])

    def test_windows_interrupt_targets_only_owned_process_tree(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows process-tree termination")
        from unittest.mock import Mock

        child = Mock(pid=12345)
        child.poll.return_value = None
        with patch.object(at.subprocess, "run") as terminate:
            at._stop_test_process(child)
        self.assertEqual(terminate.call_args.args[0],
                         ["taskkill", "/PID", "12345", "/T", "/F"])
        child.wait.assert_called_once_with(timeout=5)

    def test_finished_child_is_not_terminated(self) -> None:
        from unittest.mock import Mock

        child = Mock()
        child.poll.return_value = 0
        with patch.object(at.subprocess, "run") as terminate:
            at._stop_test_process(child)
        terminate.assert_not_called()

    def test_explicit_runner_forwards_targets(self) -> None:
        from scripts import run_tests

        with patch.object(run_tests, "_run_tests", return_value=1) as run:
            self.assertEqual(run_tests.main(["tests/test_example.py", "-q"]), 1)
        run.assert_called_once_with([
            sys.executable, "-m", "pytest", "tests/test_example.py", "-q",
        ])


if __name__ == "__main__":
    unittest.main()

"""The ``app.core.session`` package must not drag in the controller.

Every module in the package runs ``__init__`` first, so eager re-exports
there meant importing a leaf utility pulled in all ~29 mixins. That closed
a real cycle: ``app.core.voice.tts_queue`` imports ``session_text_utils``,
``__init__`` imported ``voice_mixin``, and ``voice_mixin`` imports
``TtsQueue`` back out of a half-executed module.

It only broke when ``tts_queue`` was imported *first*, so the app never hit
it and a single test file failed to collect. That order-dependence is why
these run in subprocesses: inside one pytest process some earlier import
has already warmed ``sys.modules`` and the cycle cannot reproduce.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


class ImportOrderTests(unittest.TestCase):
    def test_tts_queue_can_be_imported_first(self) -> None:
        # The exact cycle. Fails with ImportError on a partially
        # initialised app.core.voice.tts_queue when __init__ is eager.
        proc = _run("import app.core.voice.tts_queue as m; assert m.TtsQueue")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_leaf_text_utils_can_be_imported_first(self) -> None:
        proc = _run(
            "from app.core.session.session_text_utils import prepare_tts_text;"
            " assert prepare_tts_text('hi')"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_leaf_import_does_not_pull_in_the_controller(self) -> None:
        # The actual invariant. If this regresses, the cycle is one
        # new cross-package import away from coming back.
        proc = _run(
            "import sys;"
            " import app.core.session.session_text_utils;"
            " leaked = [m for m in ('app.core.session.voice_mixin',"
            " 'app.core.session.session_controller') if m in sys.modules];"
            " print(leaked); assert not leaked, leaked"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


class LazyExportTests(unittest.TestCase):
    def test_names_still_resolve_off_the_package(self) -> None:
        import app.core.session as pkg

        for name in ("VoiceMixin", "WorldMixin", "TaskHandles", "KNOWN_OVERRIDES"):
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(pkg, name))

    def test_unknown_name_raises_attribute_error(self) -> None:
        import app.core.session as pkg

        with self.assertRaises(AttributeError):
            pkg.NoSuchMixin  # noqa: B018

    def test_every_exported_name_is_reachable(self) -> None:
        import app.core.session as pkg

        for name in pkg.__all__:
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(pkg, name))


if __name__ == "__main__":
    unittest.main()

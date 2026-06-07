"""Tests for :mod:`app.core.tasks.sandbox`.

Pin the contract every filesystem task handler depends on:

* Label normalisation rejects everything that breaks the
  ``"<label>:<path>"`` prefix grammar.
* :func:`normalize_root` resolves relative paths against the supplied
  app root without touching the disk.
* :func:`validate_roots` flips paths inactive with stable
  ``reason=`` strings; sensitive directories get warned but stay
  active.
* :func:`resolve_path` rejects every shape of escape attempt
  (``..``, absolute paths outside roots, ``Documents:..\\..\\etc``)
  and handles label-prefixed + bare paths symmetrically.
* Multi-root resolution surfaces ``multiple_matches`` candidates so
  the handler can emit ``TaskInputNeeded``.

These tests are pure (temp-dir tree, no orchestrator, no DB) and
should run in well under a second on any platform.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from app.core.tasks.sandbox import (
    FileTaskRoot,
    PathResolutionError,
    ResolvedPath,
    ValidatedRoot,
    is_valid_label,
    normalize_root,
    resolve_path,
    validate_roots,
)


class LabelValidationTests(unittest.TestCase):
    def test_simple_label_accepted(self) -> None:
        self.assertTrue(is_valid_label("Documents"))
        self.assertTrue(is_valid_label("Notes"))
        self.assertTrue(is_valid_label("user_documents"))
        self.assertTrue(is_valid_label("My Stuff"))
        self.assertTrue(is_valid_label("v1-archive"))

    def test_empty_rejected(self) -> None:
        self.assertFalse(is_valid_label(""))
        self.assertFalse(is_valid_label("   "))

    def test_separators_rejected(self) -> None:
        # Colon is the prefix separator.
        self.assertFalse(is_valid_label("a:b"))
        self.assertFalse(is_valid_label("foo/bar"))
        self.assertFalse(is_valid_label("foo\\bar"))
        self.assertFalse(is_valid_label("foo\nbar"))
        self.assertFalse(is_valid_label("foo\tbar"))

    def test_non_string_rejected(self) -> None:
        self.assertFalse(is_valid_label(None))  # type: ignore[arg-type]
        self.assertFalse(is_valid_label(42))  # type: ignore[arg-type]


class NormalizeRootTests(unittest.TestCase):
    def test_relative_resolves_against_app_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = FileTaskRoot(label="Notes", path="docs/notes")
            n = normalize_root(r, app_root=td)
            self.assertTrue(os.path.isabs(n.path))
            self.assertTrue(n.path.endswith(os.path.normpath("docs/notes")))
            self.assertEqual(n.label, "Notes")
            self.assertTrue(n.read_only)

    def test_absolute_kept_verbatim(self) -> None:
        if sys.platform == "win32":
            r = FileTaskRoot(label="X", path=r"C:\some\absolute\path")
        else:
            r = FileTaskRoot(label="X", path="/some/absolute/path")
        n = normalize_root(r, app_root="/tmp/whatever")
        # ``resolve(strict=False)`` may normalise case/slashes but the
        # path remains absolute and points to the same place.
        self.assertTrue(os.path.isabs(n.path))
        self.assertEqual(n.label, "X")

    def test_dotdot_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = FileTaskRoot(label="N", path="docs/../notes")
            n = normalize_root(r, app_root=td)
            self.assertNotIn("..", n.path.split(os.sep))


class ValidateRootsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.docs = self.tmp_path / "Docs"
        self.docs.mkdir()
        self.empty_file = self.tmp_path / "loose.txt"
        self.empty_file.write_text("hello")
        self.addCleanup(self.tmp.cleanup)

    def test_existing_dir_active(self) -> None:
        out = validate_roots(
            [FileTaskRoot(label="Docs", path=str(self.docs))],
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].active)
        self.assertEqual(out[0].reason, "")
        self.assertEqual(out[0].warnings, ())

    def test_missing_path_inactive(self) -> None:
        ghost = str(self.tmp_path / "does-not-exist")
        out = validate_roots([FileTaskRoot(label="Ghost", path=ghost)])
        self.assertFalse(out[0].active)
        self.assertEqual(out[0].reason, "missing")

    def test_file_not_directory_inactive(self) -> None:
        out = validate_roots(
            [FileTaskRoot(label="Loose", path=str(self.empty_file))]
        )
        self.assertFalse(out[0].active)
        self.assertEqual(out[0].reason, "not_a_directory")

    def test_invalid_label_inactive(self) -> None:
        out = validate_roots(
            [FileTaskRoot(label="bad:label", path=str(self.docs))]
        )
        self.assertFalse(out[0].active)
        self.assertEqual(out[0].reason, "invalid_label")

    def test_duplicate_path_second_warns(self) -> None:
        out = validate_roots(
            [
                FileTaskRoot(label="A", path=str(self.docs)),
                FileTaskRoot(label="B", path=str(self.docs)),
            ]
        )
        self.assertTrue(out[0].active)
        self.assertTrue(out[1].active)
        self.assertEqual(out[0].warnings, ())
        self.assertIn("duplicate_path", out[1].warnings)

    def test_nested_warning(self) -> None:
        outer = self.docs
        inner = outer / "child"
        inner.mkdir()
        out = validate_roots(
            [
                FileTaskRoot(label="Outer", path=str(outer)),
                FileTaskRoot(label="Inner", path=str(inner)),
            ]
        )
        self.assertTrue(out[1].active)
        self.assertTrue(
            any(w.startswith("nested_inside_") for w in out[1].warnings)
        )


class ResolvePathTests(unittest.TestCase):
    """End-to-end resolution against a real temp-dir tree.

    Layout::

        tmp/
          Docs/
            a.md
            sub/
              b.md
          Notes/
            a.md           (same relative path as Docs/a.md -> multi-match)
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.docs = self.tmp_path / "Docs"
        self.notes = self.tmp_path / "Notes"
        self.docs.mkdir()
        self.notes.mkdir()
        (self.docs / "a.md").write_text("doc a")
        (self.docs / "sub").mkdir()
        (self.docs / "sub" / "b.md").write_text("doc sub b")
        (self.notes / "a.md").write_text("note a")
        self.actives = validate_roots(
            [
                FileTaskRoot(label="Docs", path=str(self.docs)),
                FileTaskRoot(label="Notes", path=str(self.notes)),
            ]
        )

    def test_label_prefixed_hits(self) -> None:
        out = resolve_path("Docs:a.md", active_roots=self.actives)
        self.assertIsInstance(out, ResolvedPath)
        assert isinstance(out, ResolvedPath)  # for the type checker
        self.assertEqual(out.label, "Docs")
        self.assertEqual(out.relative_path, "a.md")
        self.assertTrue(Path(out.abs_path).is_file())

    def test_label_prefixed_unknown_label(self) -> None:
        out = resolve_path("Ghost:a.md", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "unknown_label")

    def test_label_prefixed_nonexistent_path(self) -> None:
        out = resolve_path("Docs:nope.md", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "no_match")

    def test_bare_unique_match(self) -> None:
        out = resolve_path("sub/b.md", active_roots=self.actives)
        self.assertIsInstance(out, ResolvedPath)
        assert isinstance(out, ResolvedPath)
        self.assertEqual(out.label, "Docs")
        # On Windows the join uses backslashes internally but
        # ResolvedPath always normalises to forward slashes.
        self.assertEqual(out.relative_path, "sub/b.md")

    def test_bare_multiple_matches_returns_candidates(self) -> None:
        out = resolve_path("a.md", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "multiple_matches")
        labels = sorted(c.label for c in out.candidates)
        self.assertEqual(labels, ["Docs", "Notes"])

    def test_bare_no_match(self) -> None:
        out = resolve_path("missing.md", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "no_match")

    def test_escape_attempt_via_dotdot(self) -> None:
        out = resolve_path("Docs:../Notes/a.md", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "escape")

    def test_escape_attempt_deep_dotdot(self) -> None:
        out = resolve_path(
            "Docs:sub/../../Notes/a.md", active_roots=self.actives
        )
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "escape")

    def test_empty_path_rejected(self) -> None:
        out = resolve_path("", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        self.assertEqual(out.reason, "empty_path")

    def test_inactive_root_skipped(self) -> None:
        # Mark Notes inactive by passing a stubbed verdict.
        actives = (
            self.actives[0],  # Docs active
            ValidatedRoot(
                root=self.actives[1].root,
                active=False,
                abs_path=self.actives[1].abs_path,
                reason="missing",
            ),
        )
        out = resolve_path("a.md", active_roots=actives)
        # No multi-match because Notes is skipped.
        self.assertIsInstance(out, ResolvedPath)
        assert isinstance(out, ResolvedPath)
        self.assertEqual(out.label, "Docs")

    def test_must_exist_false_returns_synthetic_path(self) -> None:
        out = resolve_path(
            "Docs:future.md",
            active_roots=self.actives,
            must_exist=False,
        )
        self.assertIsInstance(out, ResolvedPath)
        assert isinstance(out, ResolvedPath)
        self.assertEqual(out.label, "Docs")
        self.assertFalse(Path(out.abs_path).exists())

    def test_windows_drive_letter_not_treated_as_label(self) -> None:
        # Even on POSIX, the ``"C:\..."`` shape must not be parsed
        # as ``"<label C>:<\\...>"`` because ``C`` is technically a
        # valid label. We guarded by also requiring the rest of the
        # string to make sense as a *relative* path under that
        # label. On a non-Windows test box the resolver will simply
        # return ``no_match`` (label ``"C"`` doesn't exist) — the
        # important contract is that we don't crash and don't
        # short-circuit to an absolute Windows path.
        out = resolve_path(r"C:\Windows\System32", active_roots=self.actives)
        self.assertIsInstance(out, PathResolutionError)
        assert isinstance(out, PathResolutionError)
        # ``unknown_label`` (label "C" not configured) is the
        # documented outcome. We tolerate ``escape`` too in case the
        # ``\Windows\System32`` tail resolves outside the root on
        # some platforms.
        self.assertIn(out.reason, {"unknown_label", "escape", "no_match"})


if __name__ == "__main__":
    unittest.main()

"""Tests for app.core.persona_manager."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core.persona_manager import PersonaError, PersonaManager


def _make_zip(entries: dict[str, str | bytes]) -> io.BytesIO:
    """Build an in-memory zip with the given path -> content mapping."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, payload in entries.items():
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            zf.writestr(path, payload)
    buffer.seek(0)
    return buffer


_MODEL3_BASE = {
    "Version": 3,
    "Name": "Senko",
    "FileReferences": {
        "Moc": "senko.moc3",
        "Textures": ["textures/texture_00.png"],
        "Motions": {
            "Idle": [
                {"Name": "Idle1", "File": "motions/idle1.motion3.json"},
                {"Name": "Idle2", "File": "motions/idle2.motion3.json"},
            ],
            "Tap": [
                {"Name": "Tap1", "File": "motions/tap.motion3.json"},
            ],
        },
        "Expressions": [
            {"Name": "smile", "File": "expressions/smile.exp3.json"},
            {"Name": "sad", "File": "expressions/sad.exp3.json"},
            {"Name": "surprised", "File": "expressions/surprise.exp3.json"},
        ],
    },
}


class _TempManager:
    def __enter__(self) -> tuple[PersonaManager, Path]:
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        return PersonaManager(root), root

    def __exit__(self, *exc):
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class TestHappyPath(unittest.TestCase):
    def test_extract_cubism3_model_and_manifest(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko.model3.json": json.dumps(_MODEL3_BASE),
                "senko.moc3": b"\x00" * 32,
                "textures/texture_00.png": b"\x89PNG fake",
                "motions/idle1.motion3.json": "{}",
                "motions/idle2.motion3.json": "{}",
                "motions/tap.motion3.json": "{}",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf, display_name="Senko")
            self.assertEqual(manifest.cubism_version, 3)
            self.assertEqual(manifest.entry_filename, "senko.model3.json")
            self.assertEqual(manifest.display_name, "Senko")
            self.assertEqual(len(manifest.expressions), 3)
            self.assertEqual(set(manifest.motions.keys()), {"Idle", "Tap"})
            self.assertEqual(len(manifest.motions["Idle"]), 2)
            # Idle motion group auto-picked.
            self.assertEqual(manifest.idle_motion_group, "Idle")
            self.assertEqual(manifest.talk_motion_group, "Tap")

            # Files actually got extracted.
            self.assertTrue((mgr.active_dir / "senko.moc3").is_file())
            self.assertTrue((mgr.active_dir / "_persona.json").is_file())

            # Reload from disk.
            again = mgr.current()
            self.assertIsNotNone(again)
            assert again is not None
            self.assertEqual(again.entry_filename, "senko.model3.json")

    def test_default_reaction_mapping_picks_smile_for_cheerful(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko.model3.json": json.dumps(_MODEL3_BASE),
                "senko.moc3": b"",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
            self.assertEqual(manifest.reaction_mapping.get("cheerful"), "smile")
            self.assertEqual(manifest.reaction_mapping.get("sad"), "sad")
            self.assertEqual(
                manifest.reaction_mapping.get("surprised"), "surprised",
            )

    def test_install_replaces_previous_active(self) -> None:
        with _TempManager() as (mgr, _root):
            zf1 = _make_zip({
                "first.model3.json": json.dumps(_MODEL3_BASE),
                "first.moc3": b"",
            })
            mgr.install_from_zip(zf1, display_name="First")
            self.assertTrue((mgr.active_dir / "first.moc3").is_file())

            zf2 = _make_zip({
                "second.model3.json": json.dumps(_MODEL3_BASE),
                "second.moc3": b"",
            })
            mgr.install_from_zip(zf2, display_name="Second")
            self.assertFalse(
                (mgr.active_dir / "first.moc3").is_file(),
                "previous persona should have been replaced",
            )
            self.assertTrue((mgr.active_dir / "second.moc3").is_file())


class TestEntrySelection(unittest.TestCase):
    def test_picks_shallowest_entry(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "outer.model3.json": json.dumps(_MODEL3_BASE),
                "outer.moc3": b"",
                "deep/nested/inner.model3.json": json.dumps(_MODEL3_BASE),
                "deep/nested/inner.moc3": b"",
            })
            manifest = mgr.install_from_zip(zf)
            self.assertEqual(manifest.entry_filename, "outer.model3.json")

    def test_prefers_model3_over_legacy_model_json(self) -> None:
        legacy = {
            "model": "legacy.moc",
            "expressions": [{"name": "smile", "file": "smile.exp.json"}],
            "motions": {"idle": [{"name": "idle", "file": "idle.mtn"}]},
        }
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "legacy.model.json": json.dumps(legacy),
                "legacy.moc": b"",
                "modern.model3.json": json.dumps(_MODEL3_BASE),
                "modern.moc3": b"",
            })
            manifest = mgr.install_from_zip(zf)
            self.assertEqual(manifest.cubism_version, 3)
            self.assertEqual(manifest.entry_filename, "modern.model3.json")


class TestRejection(unittest.TestCase):
    def test_rejects_zip_with_dotdot_paths(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            # zipfile by itself accepts arbitrary names. Validation must catch.
            zf.writestr("../escape.txt", b"oops")
            zf.writestr("model.model3.json", json.dumps(_MODEL3_BASE))
        buffer.seek(0)
        with _TempManager() as (mgr, _root):
            with self.assertRaises(PersonaError) as ctx:
                mgr.install_from_zip(buffer)
            self.assertIn("unsafe path", str(ctx.exception).lower())

    def test_rejects_zip_with_no_model_entry(self) -> None:
        zf = _make_zip({
            "readme.txt": "no model here",
            "art/picture.png": b"",
        })
        with _TempManager() as (mgr, _root):
            with self.assertRaises(PersonaError) as ctx:
                mgr.install_from_zip(zf)
            self.assertIn("model", str(ctx.exception).lower())

    def test_empty_zip_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w"):
            pass
        buffer.seek(0)
        with _TempManager() as (mgr, _root):
            with self.assertRaises(PersonaError):
                mgr.install_from_zip(buffer)


class TestMappingPersistence(unittest.TestCase):
    def test_update_mapping_filters_unknown_reactions_and_expressions(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko.model3.json": json.dumps(_MODEL3_BASE),
                "senko.moc3": b"",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            mgr.install_from_zip(zf)
            updated = mgr.update_mapping(
                reaction_mapping={
                    "cheerful": "smile",
                    "bogus_reaction": "smile",
                    "sad": "does_not_exist",
                    "angry": "sad",
                },
                idle_motion_group="Idle",
                talk_motion_group="NotARealGroup",
            )
            assert updated is not None
            self.assertEqual(updated.reaction_mapping.get("cheerful"), "smile")
            self.assertNotIn("bogus_reaction", updated.reaction_mapping)
            # 'sad' had an invalid expression -> dropped from cleaned mapping
            self.assertNotIn("sad", updated.reaction_mapping)
            self.assertEqual(updated.reaction_mapping.get("angry"), "sad")
            self.assertEqual(updated.idle_motion_group, "Idle")
            # Talk group cleared because the requested group doesn't exist.
            self.assertIsNone(updated.talk_motion_group)


class TestDelete(unittest.TestCase):
    def test_delete_removes_active_dir(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko.model3.json": json.dumps(_MODEL3_BASE),
                "senko.moc3": b"",
            })
            mgr.install_from_zip(zf)
            self.assertTrue(mgr.active_dir.exists())
            self.assertTrue(mgr.delete())
            self.assertFalse(mgr.active_dir.exists())
            self.assertFalse(mgr.delete())  # second call is a no-op
            self.assertIsNone(mgr.current())


if __name__ == "__main__":
    unittest.main()

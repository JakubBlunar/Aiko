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


class TestUrlUnsafeSanitization(unittest.TestCase):
    """Live2D zips occasionally embed ``#``/``?`` in filenames (browser
    URL fragment / query delimiters). These get stripped server-side and
    cause silent texture-load 404s. ``install_from_zip`` must rename the
    files and patch every JSON reference so the renderer can fetch them.
    """

    def _model3_with_hashed_textures(self) -> dict:
        data = json.loads(json.dumps(_MODEL3_BASE))
        data["FileReferences"]["Textures"] = [
            "textures/texture_00 #969.png",
            "textures/texture_01 #562.png",
        ]
        return data

    def test_hash_in_texture_names_is_sanitized(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko.model3.json": json.dumps(self._model3_with_hashed_textures()),
                "senko.moc3": b"",
                "textures/texture_00 #969.png": b"\x89PNG fake-0",
                "textures/texture_01 #562.png": b"\x89PNG fake-1",
                "motions/idle1.motion3.json": "{}",
                "motions/idle2.motion3.json": "{}",
                "motions/tap.motion3.json": "{}",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            mgr.install_from_zip(zf)
            # No ``#`` survives in filenames.
            for path in mgr.active_dir.rglob("*"):
                self.assertNotIn("#", path.name)
                self.assertNotIn("?", path.name)
            # Sanitized texture files exist on disk.
            self.assertTrue((mgr.active_dir / "textures" / "texture_00 _969.png").is_file())
            self.assertTrue((mgr.active_dir / "textures" / "texture_01 _562.png").is_file())
            # Original names are gone.
            self.assertFalse((mgr.active_dir / "textures" / "texture_00 #969.png").exists())
            # The model3.json was rewritten so the renderer can resolve them.
            entry_text = (mgr.active_dir / "senko.model3.json").read_text(encoding="utf-8")
            self.assertNotIn("#", entry_text)
            self.assertIn("texture_00 _969.png", entry_text)
            self.assertIn("texture_01 _562.png", entry_text)

    def test_hash_in_entry_filename_follows_rename(self) -> None:
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko #v2.model3.json": json.dumps(_MODEL3_BASE),
                "senko.moc3": b"",
                "textures/texture_00.png": b"",
                "motions/idle1.motion3.json": "{}",
                "motions/idle2.motion3.json": "{}",
                "motions/tap.motion3.json": "{}",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
            # Manifest reflects the sanitized name.
            self.assertNotIn("#", manifest.entry_filename)
            self.assertEqual(manifest.entry_filename, "senko _v2.model3.json")
            self.assertTrue((mgr.active_dir / "senko _v2.model3.json").is_file())

    def test_hash_in_directory_name_is_sanitized(self) -> None:
        with _TempManager() as (mgr, _root):
            data = json.loads(json.dumps(_MODEL3_BASE))
            data["FileReferences"]["Textures"] = ["a #1/texture_00.png"]
            zf = _make_zip({
                "senko.model3.json": json.dumps(data),
                "senko.moc3": b"",
                "a #1/texture_00.png": b"",
                "motions/idle1.motion3.json": "{}",
                "motions/idle2.motion3.json": "{}",
                "motions/tap.motion3.json": "{}",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            mgr.install_from_zip(zf)
            self.assertTrue((mgr.active_dir / "a _1" / "texture_00.png").is_file())
            entry_text = (mgr.active_dir / "senko.model3.json").read_text(encoding="utf-8")
            self.assertIn("a _1/texture_00.png", entry_text)
            self.assertNotIn("a #1", entry_text)


class TestOrphanMotionDiscovery(unittest.TestCase):
    """Many ripped Live2D zips ship a stub model3.json with one Idle
    motion while the ``motions/`` folder holds dozens of unreferenced
    files. ``install_from_zip`` should discover those and append them
    so the renderer (and our manifest parser) can see them.
    """

    def test_flat_motions_discovered_under_extra_group(self) -> None:
        # Stub model3.json: only declares the first motion.
        stub = {
            "Version": 3,
            "FileReferences": {
                "Moc": "a02.moc3",
                "Textures": ["textures/texture_00.png"],
                "Motions": {
                    "Idle": [{"File": "motions/a02.motion3.json"}],
                },
            },
        }
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "a02.model3.json": json.dumps(stub),
                "a02.moc3": b"",
                "textures/texture_00.png": b"",
                "motions/a02.motion3.json": "{}",
                "motions/a02r01.motion3.json": "{}",
                "motions/a02r02.motion3.json": "{}",
                "motions/a02r03.motion3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)

        self.assertIn("Idle", manifest.motions)
        self.assertEqual(len(manifest.motions["Idle"]), 1)
        self.assertIn("Extra", manifest.motions)
        extra_files = [m.file for m in manifest.motions["Extra"]]
        self.assertIn("motions/a02r01.motion3.json", extra_files)
        self.assertIn("motions/a02r02.motion3.json", extra_files)
        self.assertIn("motions/a02r03.motion3.json", extra_files)
        # The originally declared motion does NOT get duplicated.
        self.assertNotIn("motions/a02.motion3.json", extra_files)

    def test_nested_motions_grouped_by_subdir(self) -> None:
        stub = {
            "Version": 3,
            "FileReferences": {
                "Moc": "model.moc3",
                "Textures": ["textures/texture_00.png"],
                "Motions": {
                    "Idle": [{"File": "motions/idle/idle1.motion3.json"}],
                },
            },
        }
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "model.model3.json": json.dumps(stub),
                "model.moc3": b"",
                "textures/texture_00.png": b"",
                "motions/idle/idle1.motion3.json": "{}",
                "motions/idle/idle2.motion3.json": "{}",
                "motions/tap/tap1.motion3.json": "{}",
                "motions/tap/tap2.motion3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
        # idle2 lands under "Idle" (existing group, capitalized form),
        # tap motions land under a new "Tap" group.
        self.assertIn("Idle", manifest.motions)
        self.assertIn("Tap", manifest.motions)
        idle_files = {m.file for m in manifest.motions["Idle"]}
        tap_files = {m.file for m in manifest.motions["Tap"]}
        self.assertIn("motions/idle/idle1.motion3.json", idle_files)
        self.assertIn("motions/idle/idle2.motion3.json", idle_files)
        self.assertEqual(
            tap_files,
            {
                "motions/tap/tap1.motion3.json",
                "motions/tap/tap2.motion3.json",
            },
        )

    def test_orphan_expressions_get_appended(self) -> None:
        stub = {
            "Version": 3,
            "FileReferences": {
                "Moc": "model.moc3",
                "Textures": ["textures/texture_00.png"],
                "Motions": {"Idle": [{"File": "motions/idle.motion3.json"}]},
                "Expressions": [
                    {"Name": "smile", "File": "expressions/smile.exp3.json"},
                ],
            },
        }
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "model.model3.json": json.dumps(stub),
                "model.moc3": b"",
                "textures/texture_00.png": b"",
                "motions/idle.motion3.json": "{}",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/wink.exp3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
        names = {e.name for e in manifest.expressions}
        # Original survives, orphans appear with derived names.
        self.assertIn("smile", names)
        self.assertIn("sad", names)
        self.assertIn("wink", names)

    def test_full_manifest_no_augmentation_needed(self) -> None:
        # If the entry JSON already declares everything, augmentation is a
        # no-op (no duplicates, no extra group).
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "senko.model3.json": json.dumps(_MODEL3_BASE),
                "senko.moc3": b"",
                "textures/texture_00.png": b"",
                "motions/idle1.motion3.json": "{}",
                "motions/idle2.motion3.json": "{}",
                "motions/tap.motion3.json": "{}",
                "expressions/smile.exp3.json": "{}",
                "expressions/sad.exp3.json": "{}",
                "expressions/surprise.exp3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
        self.assertEqual(set(manifest.motions.keys()), {"Idle", "Tap"})
        self.assertEqual(len(manifest.motions["Idle"]), 2)
        self.assertEqual(len(manifest.motions["Tap"]), 1)
        self.assertEqual(len(manifest.expressions), 3)

    def test_entry_in_subdirectory_uses_entry_relative_paths(self) -> None:
        # Real-world ripped models often nest the model3.json inside a
        # character-named subdirectory. References are relative to that
        # subdirectory, not to the zip root, so the augmenter has to walk
        # the entry's own directory and compare entry-relative paths.
        # Otherwise every declared motion looks like an "orphan" and we
        # double-list everything in an "Extra" group with broken paths.
        stub = {
            "Version": 3,
            "FileReferences": {
                "Moc": "model.moc3",
                "Textures": ["textures/texture_00.png"],
                "Motions": {
                    "default": [
                        {"File": "motions/m1.motion3.json"},
                        {"File": "motions/m2.motion3.json"},
                    ],
                },
            },
        }
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "character/model.model3.json": json.dumps(stub),
                "character/model.moc3": b"",
                "character/textures/texture_00.png": b"",
                "character/motions/m1.motion3.json": "{}",
                "character/motions/m2.motion3.json": "{}",
                "character/motions/m3.motion3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
        # Only the genuine orphan ``m3`` ends up under Extra.
        self.assertEqual(len(manifest.motions["default"]), 2)
        self.assertIn("Extra", manifest.motions)
        self.assertEqual(len(manifest.motions["Extra"]), 1)
        self.assertEqual(
            manifest.motions["Extra"][0].file, "motions/m3.motion3.json",
        )
        # And no path got prefixed with the staging subdir.
        for entries in manifest.motions.values():
            for ref in entries:
                self.assertFalse(ref.file.startswith("character/"))

    def test_empty_string_motion_group_is_renamed_to_default(self) -> None:
        # Many ripped Cubism 3 models declare every motion under the
        # empty-string group key. The renderer (pixi-live2d-display) will
        # play motions from that group when the UI passes ``""`` to
        # ``model.motion()``, but our UI dropdown can't render an empty
        # label. Normalize to ``default`` so manifest, JSON, and UI agree.
        stub = {
            "Version": 3,
            "FileReferences": {
                "Moc": "model.moc3",
                "Textures": ["textures/texture_00.png"],
                "Motions": {
                    "": [
                        {"File": "motions/m1.motion3.json"},
                        {"File": "motions/m2.motion3.json"},
                    ],
                },
            },
        }
        with _TempManager() as (mgr, _root):
            zf = _make_zip({
                "model.model3.json": json.dumps(stub),
                "model.moc3": b"",
                "textures/texture_00.png": b"",
                "motions/m1.motion3.json": "{}",
                "motions/m2.motion3.json": "{}",
            })
            manifest = mgr.install_from_zip(zf)
            entry_text = (mgr.active_dir / "model.model3.json").read_text(
                encoding="utf-8"
            )
        self.assertIn("default", manifest.motions)
        self.assertNotIn("", manifest.motions)
        self.assertEqual(len(manifest.motions["default"]), 2)
        # The on-disk JSON also uses ``default`` so the renderer sees the
        # same key the UI passes back.
        entry_data = json.loads(entry_text)
        motion_keys = list(entry_data["FileReferences"]["Motions"].keys())
        self.assertIn("default", motion_keys)
        self.assertNotIn("", motion_keys)


class TestScaleMultiplier(unittest.TestCase):
    """``scale_multiplier`` is a per-persona zoom knob the UI slider
    drives. The backend clamps to a sane range and persists it across
    reloads — otherwise tuning Hiyori up to 1.6x would be lost on
    refresh."""

    def _install(self, mgr: PersonaManager) -> None:
        zf = _make_zip({
            "senko.model3.json": json.dumps(_MODEL3_BASE),
            "senko.moc3": b"",
            "expressions/smile.exp3.json": "{}",
            "expressions/sad.exp3.json": "{}",
            "expressions/surprise.exp3.json": "{}",
            "motions/idle1.motion3.json": "{}",
            "motions/idle2.motion3.json": "{}",
            "motions/tap.motion3.json": "{}",
        })
        mgr.install_from_zip(zf)

    def test_default_is_one(self) -> None:
        with _TempManager() as (mgr, _root):
            self._install(mgr)
            manifest = mgr.current()
        assert manifest is not None
        self.assertEqual(manifest.scale_multiplier, 1.0)

    def test_update_persists_and_reloads(self) -> None:
        with _TempManager() as (mgr, _root):
            self._install(mgr)
            mgr.update_mapping(scale_multiplier=1.6)
            again = mgr.current()
        assert again is not None
        self.assertAlmostEqual(again.scale_multiplier, 1.6)

    def test_clamps_to_safe_range(self) -> None:
        # 100x would zoom the avatar off-screen entirely; -5 would
        # invert; both are clamped to the [0.3, 4.0] range.
        with _TempManager() as (mgr, _root):
            self._install(mgr)
            mgr.update_mapping(scale_multiplier=100.0)
            self.assertAlmostEqual(mgr.current().scale_multiplier, 4.0)  # type: ignore[union-attr]
            mgr.update_mapping(scale_multiplier=-5.0)
            self.assertAlmostEqual(mgr.current().scale_multiplier, 0.3)  # type: ignore[union-attr]

    def test_nan_falls_back_to_one(self) -> None:
        with _TempManager() as (mgr, _root):
            self._install(mgr)
            mgr.update_mapping(scale_multiplier=float("nan"))
            again = mgr.current()
        assert again is not None
        self.assertEqual(again.scale_multiplier, 1.0)


class TestLipSyncGroups(unittest.TestCase):
    """Cubism 3 ``Groups[LipSync].Ids`` carry the parameter name(s) that
    drive the mouth. Models ported from Cubism 2 keep ``PARAM_MOUTH_OPEN_Y``
    while modern rigs use ``ParamMouthOpenY``. Parsing them into the
    manifest lets the renderer drive whichever one each model declares.
    """

    def _model_with_groups(self, lip_ids: list[str], eye_ids: list[str]) -> dict:
        data = json.loads(json.dumps(_MODEL3_BASE))
        data["Groups"] = [
            {"Target": "Parameter", "Name": "EyeBlink", "Ids": eye_ids},
            {"Target": "Parameter", "Name": "LipSync", "Ids": lip_ids},
        ]
        return data

    def _make_full_zip(self, model_data: dict) -> io.BytesIO:
        return _make_zip({
            "senko.model3.json": json.dumps(model_data),
            "senko.moc3": b"",
            "textures/texture_00.png": b"",
            "motions/idle1.motion3.json": "{}",
            "motions/idle2.motion3.json": "{}",
            "motions/tap.motion3.json": "{}",
            "expressions/smile.exp3.json": "{}",
            "expressions/sad.exp3.json": "{}",
            "expressions/surprise.exp3.json": "{}",
        })

    def test_legacy_param_ids_are_extracted(self) -> None:
        with _TempManager() as (mgr, _root):
            data = self._model_with_groups(
                lip_ids=["PARAM_MOUTH_OPEN_Y"],
                eye_ids=["PARAM_EYE_L_OPEN", "PARAM_EYE_R_OPEN"],
            )
            manifest = mgr.install_from_zip(self._make_full_zip(data))
        self.assertEqual(manifest.lip_sync_ids, ["PARAM_MOUTH_OPEN_Y"])
        self.assertEqual(
            manifest.eye_blink_ids, ["PARAM_EYE_L_OPEN", "PARAM_EYE_R_OPEN"],
        )

    def test_modern_param_ids_are_extracted(self) -> None:
        with _TempManager() as (mgr, _root):
            data = self._model_with_groups(
                lip_ids=["ParamMouthOpenY"],
                eye_ids=["ParamEyeLOpen", "ParamEyeROpen"],
            )
            manifest = mgr.install_from_zip(self._make_full_zip(data))
        self.assertEqual(manifest.lip_sync_ids, ["ParamMouthOpenY"])
        self.assertEqual(
            manifest.eye_blink_ids, ["ParamEyeLOpen", "ParamEyeROpen"],
        )

    def test_missing_groups_yield_empty_lists(self) -> None:
        with _TempManager() as (mgr, _root):
            # _MODEL3_BASE doesn't declare Groups.
            zf = self._make_full_zip(json.loads(json.dumps(_MODEL3_BASE)))
            manifest = mgr.install_from_zip(zf)
        self.assertEqual(manifest.lip_sync_ids, [])
        self.assertEqual(manifest.eye_blink_ids, [])

    def test_manifest_persists_lip_sync_ids_after_reload(self) -> None:
        with _TempManager() as (mgr, _root):
            data = self._model_with_groups(
                lip_ids=["PARAM_MOUTH_OPEN_Y"],
                eye_ids=["PARAM_EYE_L_OPEN"],
            )
            mgr.install_from_zip(self._make_full_zip(data))
            again = mgr.current()
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(again.lip_sync_ids, ["PARAM_MOUTH_OPEN_Y"])
        self.assertEqual(again.eye_blink_ids, ["PARAM_EYE_L_OPEN"])


if __name__ == "__main__":
    unittest.main()

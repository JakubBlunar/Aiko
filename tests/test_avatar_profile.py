"""Tests for ``app.core.avatar_profile``.

Uses tiny hand-authored fixtures under ``tests/fixtures/avatar_min/``
and ``tests/fixtures/avatar_bare/`` so CI doesn't need the gitignored
Alexia files.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.avatar_profile import (
    AvatarProfile,
    AvatarProfileError,
    OutfitBinding,
    OutfitParam,
    OverlayBinding,
    from_disk,
)


_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_MIN = _FIXTURES / "avatar_min"
_BARE = _FIXTURES / "avatar_bare"


class FromDiskTests(unittest.TestCase):
    def test_loads_basic_manifest_fields(self) -> None:
        profile = from_disk(_MIN, display_name="Mini")
        self.assertIsInstance(profile, AvatarProfile)
        self.assertEqual(profile.display_name, "Mini")
        self.assertEqual(profile.entry_filename, "Mini.model3.json")
        self.assertEqual(profile.cubism_version, 3)

    def test_parses_expressions_and_motions(self) -> None:
        profile = from_disk(_MIN)
        names = [e.name for e in profile.expressions]
        self.assertIn("lh", names)
        self.assertIn("yfmz", names)
        # File paths are kept relative + forward-slash normalised so the
        # web server can serve them at /avatar/.
        for expr in profile.expressions:
            self.assertNotIn("\\", expr.file)
        # Idle group identified for motion playback.
        self.assertIn("Idle", profile.motions)
        self.assertEqual(profile.idle_motion_group, "Idle")

    def test_extracts_lip_sync_and_eye_blink_groups(self) -> None:
        profile = from_disk(_MIN)
        self.assertEqual(profile.lip_sync_ids, ["ParamMouthOpenY"])
        self.assertEqual(
            profile.eye_blink_ids, ["ParamEyeLOpen", "ParamEyeROpen"],
        )

    def test_missing_root_raises(self) -> None:
        with self.assertRaises(AvatarProfileError):
            from_disk(_FIXTURES / "does_not_exist")

    def test_missing_entry_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(AvatarProfileError):
                from_disk(tmp)


class CapabilityDetectionTests(unittest.TestCase):
    def test_chinese_labels_match_capability_synonyms(self) -> None:
        profile = from_disk(_MIN)
        # Every capability whose synonym matches the cdi3 Chinese name
        # should flip on with a usable binding.
        self.assertTrue(profile.capabilities.get("has_blush", False))
        self.assertTrue(profile.capabilities.get("has_sweat", False))
        self.assertTrue(profile.capabilities.get("has_pajamas", False))
        self.assertTrue(profile.capabilities.get("has_day_clothes", False))
        self.assertTrue(profile.capabilities.get("has_glasses", False))
        self.assertTrue(profile.capabilities.get("has_cat_tail", False))

    def test_overlay_bindings_resolve_to_real_param_ids(self) -> None:
        profile = from_disk(_MIN)
        blush = profile.overlays.get("blush")
        self.assertIsInstance(blush, OverlayBinding)
        assert blush is not None
        self.assertEqual(blush.param_id, "Param58")
        sweat = profile.overlays.get("sweat")
        assert sweat is not None
        self.assertEqual(sweat.param_id, "Param44")

    def test_outfit_bindings_carry_mutex(self) -> None:
        profile = from_disk(_MIN)
        pj = profile.outfits.get("pajamas")
        day = profile.outfits.get("day_clothes")
        self.assertIsInstance(pj, OutfitBinding)
        self.assertIsInstance(day, OutfitBinding)
        assert pj is not None and day is not None
        self.assertIn("day_clothes", pj.mutex_with)
        self.assertIn("pajamas", day.mutex_with)

    def test_outfit_bindings_are_multi_param_from_exp3(self) -> None:
        # Pajamas should carry BOTH Param16 (clothes body) and Param17
        # (clothes-with-hood) at on_value=30, parsed from yfmz.exp3.json.
        # Day-clothes should only retain Param16 (the others have
        # Value=0 in yf.exp3.json and are filtered as "no contribution").
        profile = from_disk(_MIN)
        pj = profile.outfits.get("pajamas")
        day = profile.outfits.get("day_clothes")
        assert pj is not None and day is not None
        self.assertTrue(all(isinstance(p, OutfitParam) for p in pj.params))
        pj_ids = [p.param_id for p in pj.params]
        self.assertIn("Param16", pj_ids)
        self.assertIn("Param17", pj_ids)
        for p in pj.params:
            self.assertEqual(p.on_value, 30.0)
        day_ids = [p.param_id for p in day.params]
        self.assertEqual(day_ids, ["Param16"])
        self.assertEqual(day.params[0].on_value, 30.0)

    def test_cat_tail_param_ids_are_collected_in_order(self) -> None:
        profile = from_disk(_MIN)
        self.assertEqual(
            profile.cat_tail_param_ids,
            [
                "Param_Angle_Rotation_0_ArtMesh202",
                "Param_Angle_Rotation_1_ArtMesh202",
            ],
        )

    def test_lzx_expression_maps_to_grin_not_hood(self) -> None:
        # lzx.exp3.json drives Param54 (咧嘴笑 = toothy grin), not a hood.
        # ``has_hood`` should not be a recognised capability; the lzx
        # expression flips ``has_grin`` on instead.
        profile = from_disk(_MIN)
        self.assertTrue(profile.capabilities.get("has_grin", False))
        self.assertNotIn("has_hood", profile.capabilities)

    def test_body_angle_capability_flags(self) -> None:
        # ``ParamBodyAngleY`` and ``ParamBodyAngleZ`` are present in the
        # mini cdi3, so the body-language layer's gates flip on.
        profile = from_disk(_MIN)
        self.assertTrue(profile.capabilities.get("has_body_angle_y", False))
        self.assertTrue(profile.capabilities.get("has_body_angle_z", False))

    def test_wink_capability_requires_both_eye_params(self) -> None:
        # ``ParamEyeLOpen`` + ``ParamEyeROpen`` both exist in the mini
        # fixture so the LLM grammar can offer ``[[overlay:wink_*]]``.
        profile = from_disk(_MIN)
        self.assertTrue(profile.capabilities.get("has_wink", False))

    def test_tail_wag_aliases_cat_tail(self) -> None:
        # ``has_tail_wag`` is a synonym for ``has_cat_tail`` so the
        # renderer can boost the existing wag loop on demand.
        profile = from_disk(_MIN)
        self.assertEqual(
            profile.capabilities.get("has_tail_wag", False),
            profile.capabilities.get("has_cat_tail", False),
        )

    def test_motion_names_strip_motion3_suffix(self) -> None:
        # Live2D ships motion files named ``foo.motion3.json``. The
        # loader stores the bare gesture name (``foo``) so the prompt
        # grammar matcher can intersect with its registry of stems
        # like ``wave`` / ``nod`` / ``bow``.
        profile = from_disk(_MIN)
        names = {ref.name for refs in profile.motions.values() for ref in refs}
        for name in names:
            self.assertFalse(
                name.lower().endswith(".motion3"),
                f"motion ref name {name!r} kept the .motion3 suffix",
            )

    def test_cat_ear_param_ids_and_ear_wiggle_capability(self) -> None:
        # The mini cdi3 declares Param38..41 with 左耳/右耳 names; we
        # collect them in declaration order so the renderer can drive
        # a synchronised sine across both sides during ear_wiggle.
        profile = from_disk(_MIN)
        self.assertEqual(
            profile.cat_ear_param_ids,
            ["Param38", "Param39", "Param40", "Param41"],
        )
        self.assertTrue(profile.capabilities.get("has_ear_wiggle", False))

    def test_english_ear_labels_also_detected(self) -> None:
        # After translating Alexia's cdi3 from Chinese to English the
        # ear segments now read "Left ear 1" / "Right ear 2". The
        # ear-segment synonym list has to recognise both languages so a
        # translated rig keeps ``has_ear_wiggle`` on.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Mini.model3.json").write_text(json.dumps({
                "Version": 3,
                "FileReferences": {
                    "Moc": "Mini.moc3",
                    "DisplayInfo": "Mini.cdi3.json",
                },
            }), encoding="utf-8")
            (root / "Mini.cdi3.json").write_text(json.dumps({
                "Version": 3,
                "Parameters": [
                    {"Id": "ParamA", "GroupId": "", "Name": "Left ear 1"},
                    {"Id": "ParamB", "GroupId": "", "Name": "Left ear 2"},
                    {"Id": "ParamC", "GroupId": "", "Name": "Right ear 1"},
                    {"Id": "ParamD", "GroupId": "", "Name": "Right ear 2"},
                    # An earring should NOT be picked up — substring
                    # match on bare "ear" would be too greedy.
                    {"Id": "ParamE", "GroupId": "", "Name": "Earring"},
                ],
                "Parts": [],
            }), encoding="utf-8")
            profile = from_disk(root)
        self.assertEqual(
            profile.cat_ear_param_ids,
            ["ParamA", "ParamB", "ParamC", "ParamD"],
        )
        self.assertTrue(profile.capabilities.get("has_ear_wiggle", False))

    def test_glasses_synonym_does_not_steal_sunglasses_param(self) -> None:
        # On the translated Alexia rig "Sunglasses" is declared before
        # "Eyeglasses". A bare ``glasses`` substring synonym would have
        # the sunglasses param claim ``has_glasses`` first; we use
        # ``eyeglasses`` instead so the two rigs stay distinct.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Mini.model3.json").write_text(json.dumps({
                "Version": 3,
                "FileReferences": {
                    "Moc": "Mini.moc3",
                    "DisplayInfo": "Mini.cdi3.json",
                },
            }), encoding="utf-8")
            (root / "Mini.cdi3.json").write_text(json.dumps({
                "Version": 3,
                "Parameters": [
                    {"Id": "ParamSun", "GroupId": "", "Name": "Sunglasses"},
                    {"Id": "ParamReg", "GroupId": "", "Name": "Eyeglasses"},
                ],
                "Parts": [],
            }), encoding="utf-8")
            profile = from_disk(root)
        self.assertTrue(profile.capabilities.get("has_glasses", False))
        self.assertTrue(profile.capabilities.get("has_sunglasses", False))
        # Each binding must point at its own param, not the other one.
        glasses = profile.overlays.get("glasses")
        sunglasses = profile.overlays.get("sunglasses")
        assert glasses is not None and sunglasses is not None
        self.assertEqual(glasses.param_id, "ParamReg")
        self.assertEqual(sunglasses.param_id, "ParamSun")

    def test_bare_model_degrades_gracefully(self) -> None:
        # The bare fixture has only ParamAngleX + ParamMouthOpenY and
        # two stock expressions. None of the rich capabilities should
        # be flagged; the renderer no-ops them.
        profile = from_disk(_BARE)
        for cap_name in (
            "has_pajamas", "has_day_clothes", "has_blush", "has_sweat",
            "has_dizzy", "has_stars", "has_question", "has_cry",
            "has_angry_marks", "has_grin", "has_glasses",
            "has_sunglasses", "has_cat_tail", "has_cat_ears",
            "has_body_angle_y", "has_body_angle_z",
            "has_wink", "has_tail_wag", "has_ear_wiggle",
        ):
            self.assertFalse(
                profile.capabilities.get(cap_name, False),
                f"bare model unexpectedly has {cap_name}",
            )
        self.assertEqual(profile.overlays, {})
        self.assertEqual(profile.outfits, {})
        self.assertEqual(profile.cat_tail_param_ids, [])
        self.assertEqual(profile.cat_ear_param_ids, [])


class ReactionMappingTests(unittest.TestCase):
    def test_alexia_explicit_mapping_wins_when_expression_present(self) -> None:
        profile = from_disk(_MIN)
        # Mini fixture ships ``lh`` (blush) so tender → lh per Alexia table.
        self.assertEqual(profile.reaction_mapping.get("tender"), "lh")
        # ``wh`` (question mark) → surprised
        self.assertEqual(profile.reaction_mapping.get("surprised"), "wh")
        # ``h`` (sweat) is in the fixture; Alexia table maps no specific
        # reaction to it but the fuzzy matcher should not produce a
        # garbage mapping.
        for value in profile.reaction_mapping.values():
            self.assertIn(
                value, [e.name for e in profile.expressions] + [""],
            )

    def test_bare_model_uses_synonym_fuzzy_matching(self) -> None:
        profile = from_disk(_BARE)
        # Bare fixture only has ``smile`` / ``frown`` — no Alexia
        # expression names — so the synonym matcher fills in something
        # for the canonical reactions whose synonyms include those words.
        self.assertEqual(profile.reaction_mapping.get("cheerful"), "smile")
        self.assertIn(
            profile.reaction_mapping.get("serious"), {"frown", None},
        )

    def test_to_dict_round_trip_is_json_serialisable(self) -> None:
        profile = from_disk(_MIN)
        as_dict = profile.to_dict()
        # Must be plain Python types so FastAPI can JSONResponse it.
        encoded = json.dumps(as_dict)
        self.assertIn("capabilities", encoded)
        self.assertIn("overlays", encoded)


if __name__ == "__main__":
    unittest.main()

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
    ExpressionParam,
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
        # All other outfit capabilities must appear in mutex_with so
        # the renderer fades them out when this one ramps up.
        self.assertIn("day_clothes", pj.mutex_with)
        self.assertIn("pajamas_hooded", pj.mutex_with)
        self.assertIn("pajamas", day.mutex_with)
        self.assertIn("pajamas_hooded", day.mutex_with)

    def test_alexia_outfit_capability_mapping_matches_visual_rig(self) -> None:
        # Locks in the visual outfit assignment for Alexia so a future
        # refactor can't silently flip ``yf`` <-> ``yfmz`` again. See
        # ``app.core.avatar_profile._ALEXIA_EXPR_TO_CAPABILITY`` (and
        # ``docs/alexia-model-notes.md`` for the full audit).
        #
        # User-confirmed visual mapping (May 2026):
        #   - BASELINE                   -> day clothes (streetwear)
        #   - yf  (Param16=30 only)      -> pajamas + hood UP
        #   - yfmz (Param16=30,Param17=30) -> pajamas, hood pulled DOWN
        # Param17 is a "lift hood off" toggle, not an "add hood" one.
        from app.core.avatar_profile import _ALEXIA_EXPR_TO_CAPABILITY
        self.assertEqual(_ALEXIA_EXPR_TO_CAPABILITY["yf"], "pajamas_hooded")
        self.assertEqual(_ALEXIA_EXPR_TO_CAPABILITY["yfmz"], "pajamas")

    def test_outfit_bindings_match_visual_rig(self) -> None:
        # Three outfit bindings must exist for the rig, with the
        # canonical param assignments (see ``docs/alexia-model-notes``):
        #   - pajamas        -> Param16 + Param17 (from yfmz; Param17
        #                       lifts the hood off, leaving bare pajamas)
        #   - pajamas_hooded -> Param16 alone (from yf; default art is
        #                       hooded, so just toggling the alternate
        #                       outfit gets you the hooded look)
        #   - day_clothes    -> empty params list (baseline = no toggle;
        #                       the binding still exists so the
        #                       SettingsDrawer radio always offers the
        #                       "Day" option even though no exp3 file
        #                       activates it)
        profile = from_disk(_MIN)
        pj = profile.outfits.get("pajamas")
        hooded = profile.outfits.get("pajamas_hooded")
        day = profile.outfits.get("day_clothes")
        assert pj is not None and hooded is not None and day is not None
        # pajamas: both Param16 and Param17 at on_value=30 (hood-off)
        self.assertTrue(all(isinstance(p, OutfitParam) for p in pj.params))
        pj_ids = sorted(p.param_id for p in pj.params)
        self.assertEqual(pj_ids, ["Param16", "Param17"])
        for p in pj.params:
            self.assertEqual(p.on_value, 30.0)
        # pajamas_hooded: Param16 only at on_value=30 (hood-on default)
        self.assertEqual(
            [p.param_id for p in hooded.params], ["Param16"],
        )
        self.assertEqual(hooded.params[0].on_value, 30.0)
        # day_clothes: empty (baseline)
        self.assertEqual(day.params, [])

    def test_pajama_variants_share_param16_for_additive_renderer(self) -> None:
        # The two pajama variants intentionally BOTH carry Param16 so
        # the additive-sum renderer keeps Param16 at on_value during
        # a pajamas <-> pajamas_hooded crossfade. Only Param17 fades
        # in/out (the hood toggle). day_clothes stays empty so it
        # never contributes a competing zero write.
        profile = from_disk(_MIN)
        pj = profile.outfits.get("pajamas")
        hooded = profile.outfits.get("pajamas_hooded")
        day = profile.outfits.get("day_clothes")
        assert pj is not None and hooded is not None and day is not None
        pj_ids = {p.param_id for p in pj.params}
        hooded_ids = {p.param_id for p in hooded.params}
        day_ids = {p.param_id for p in day.params}
        self.assertEqual(
            pj_ids & hooded_ids, {"Param16"},
            "pajama variants must share Param16 for additive crossfade",
        )
        # The hood-toggle (Param17) lives in the bare pajamas binding
        # only — that's what visually removes the hood. The hooded
        # variant relies on Param17 staying at zero.
        self.assertIn("Param17", pj_ids)
        self.assertNotIn("Param17", hooded_ids)
        self.assertEqual(
            pj_ids & day_ids, set(),
            "day_clothes must not share params with pajamas",
        )
        self.assertEqual(
            hooded_ids & day_ids, set(),
            "day_clothes must not share params with pajamas_hooded",
        )

    def test_outfit_capability_flags_set_for_alexia(self) -> None:
        # All three outfit capabilities must light up so the
        # SettingsDrawer can render the four-radio control (auto,
        # day, pajamas, pajamas_hooded) without a missing option.
        profile = from_disk(_MIN)
        self.assertTrue(profile.capabilities.get("has_pajamas", False))
        self.assertTrue(profile.capabilities.get("has_pajamas_hooded", False))
        self.assertTrue(profile.capabilities.get("has_day_clothes", False))

    def test_outfit_mutex_lists_other_two_outfits(self) -> None:
        # Mutex tuples drive the "fade siblings out" envelope logic in
        # the renderer; each outfit must list the *other two* as its
        # mutually-exclusive peers so a switch transitions cleanly.
        profile = from_disk(_MIN)
        pj = profile.outfits.get("pajamas")
        hooded = profile.outfits.get("pajamas_hooded")
        day = profile.outfits.get("day_clothes")
        assert pj is not None and hooded is not None and day is not None
        self.assertEqual(
            sorted(pj.mutex_with), sorted(("day_clothes", "pajamas_hooded")),
        )
        self.assertEqual(
            sorted(hooded.mutex_with), sorted(("day_clothes", "pajamas")),
        )
        self.assertEqual(
            sorted(day.mutex_with), sorted(("pajamas", "pajamas_hooded")),
        )

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
        # No exp3 files in the bare rig either — ``expression_params``
        # should be empty so ExpressionChannel falls back to the
        # rig's natural Add-blend amplitude.
        self.assertEqual(profile.expression_params, {})


class ExpressionParamBindingTests(unittest.TestCase):
    """Verify the ``.exp3.json`` -> ``ExpressionParam`` plumbing the
    ``ExpressionChannel`` continuous-expressiveness layer reads from
    on the wire."""

    def test_expression_params_keyed_by_expression_name(self) -> None:
        profile = from_disk(_MIN)
        # ``yfmz.exp3.json`` ships Param16/Param17/Param61 with the
        # zero-Value Param61 stripped. Same for ``yf.exp3.json``.
        yfmz = profile.expression_params.get("yfmz")
        yf = profile.expression_params.get("yf")
        self.assertIsNotNone(yfmz, "missing expression_params entry for yfmz")
        self.assertIsNotNone(yf, "missing expression_params entry for yf")
        assert yfmz is not None and yf is not None
        for binding in yfmz:
            self.assertIsInstance(binding, ExpressionParam)
            self.assertGreater(binding.on_value, 0)
        ids = sorted(b.param_id for b in yfmz)
        self.assertEqual(ids, ["Param16", "Param17"])
        for binding in yfmz:
            self.assertEqual(binding.on_value, 30.0)

    def test_zero_value_params_are_stripped(self) -> None:
        # Live2D treats Value=0 as "no contribution"; our parser
        # filters those so the renderer doesn't waste a frame writing
        # zeros that the manager would write anyway.
        profile = from_disk(_MIN)
        for name, bindings in profile.expression_params.items():
            for binding in bindings:
                self.assertNotEqual(
                    binding.on_value, 0,
                    f"expression {name!r} kept a zero-value param "
                    f"{binding.param_id!r}",
                )

    def test_missing_exp3_files_are_skipped(self) -> None:
        # The Mini model3.json references ``lh`` / ``h`` / ``wh`` /
        # ``lzx`` files that don't exist on disk in the fixture. The
        # parser must silently skip those rather than crash; the
        # renderer falls back to the manager's natural amplitude when
        # a binding is absent.
        profile = from_disk(_MIN)
        # Only ``yf`` and ``yfmz`` actually have exp3 files in the
        # mini fixture, so those are the only entries we expect.
        self.assertEqual(set(profile.expression_params.keys()), {"yf", "yfmz"})

    def test_expression_params_round_trip_through_to_dict(self) -> None:
        # The WS payload serialises ``AvatarProfile.to_dict()`` so the
        # new field must survive the dataclass -> dict -> json
        # encode without dropping bindings.
        profile = from_disk(_MIN)
        encoded = profile.to_dict()
        self.assertIn("expression_params", encoded)
        ep = encoded["expression_params"]
        self.assertIn("yf", ep)
        # ``asdict`` flattens ExpressionParam to a plain dict.
        first = ep["yf"][0]
        self.assertIn("param_id", first)
        self.assertIn("on_value", first)
        # Final sanity: the whole structure is JSON-serialisable.
        json.dumps(encoded)


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

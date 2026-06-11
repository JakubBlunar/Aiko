"""Tests for ``app.core.persona.avatar_profile``.

Uses tiny hand-authored fixtures under ``tests/fixtures/avatar_min/``
and ``tests/fixtures/avatar_bare/`` so CI doesn't need the gitignored
Alexia files.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.persona.avatar_profile import (
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

    def test_reaction_affect_targets_on_manifest(self) -> None:
        # K45: rig-independent map shipped to ExpressionChannel so the
        # renderer can damp non-mouth expression params without a TS
        # mirror of the Python impulse table.
        profile = from_disk(_MIN)
        targets = profile.reaction_affect_targets
        self.assertIn("excited", targets)
        self.assertIn("sad", targets)
        # Directionless reactions are intentionally absent.
        self.assertNotIn("neutral", targets)
        for valence, arousal in targets.values():
            self.assertGreaterEqual(valence, -1.0)
            self.assertLessEqual(valence, 1.0)
            self.assertGreaterEqual(arousal, 0.0)
            self.assertLessEqual(arousal, 1.0)

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
        self.assertTrue(profile.capabilities.get("has_eyeglasses", False))
        self.assertTrue(profile.capabilities.get("has_lollipop", False))
        self.assertTrue(profile.capabilities.get("has_head_sunglasses", False))
        self.assertTrue(profile.capabilities.get("has_crossed_arms", False))
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
        # ``app.core.persona.avatar_profile._ALEXIA_EXPR_TO_CAPABILITY`` (and
        # ``docs/alexia-model-notes.md`` for the full audit).
        #
        # User-confirmed visual mapping (May 2026):
        #   - BASELINE                   -> day clothes (streetwear)
        #   - yf  (Param16=30 only)      -> pajamas + hood UP
        #   - yfmz (Param16=30,Param17=30) -> pajamas, hood pulled DOWN
        # Param17 is a "lift hood off" toggle, not an "add hood" one.
        from app.core.persona.avatar_profile import _ALEXIA_EXPR_TO_CAPABILITY
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
        # the sunglasses param claim ``has_eyeglasses`` first; we use
        # the explicit ``eyeglasses`` / ``head_sunglasses`` capability
        # names instead so the two rigs stay distinct.
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
        self.assertTrue(profile.capabilities.get("has_eyeglasses", False))
        self.assertTrue(profile.capabilities.get("has_head_sunglasses", False))
        # Each binding must point at its own param, not the other one.
        glasses = profile.overlays.get("eyeglasses")
        sunglasses = profile.overlays.get("head_sunglasses")
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
            "has_angry_marks", "has_grin", "has_eyeglasses",
            "has_head_sunglasses", "has_lollipop", "has_crossed_arms",
            "has_cat_tail", "has_cat_ears",
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
        # If the model3.json references an expression file that
        # doesn't exist on disk, the parser must silently skip it
        # rather than crash; the renderer falls back to the manager's
        # natural amplitude when a binding is absent. We build a
        # throw-away fixture inline so this test stays valid as the
        # shared ``avatar_min`` fixture grows real exp3 files.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Mini.model3.json").write_text(json.dumps({
                "Version": 3,
                "FileReferences": {
                    "Moc": "Mini.moc3",
                    "DisplayInfo": "Mini.cdi3.json",
                    "Expressions": [
                        {"Name": "ghost", "File": "expressions/ghost.exp3.json"},
                        {"Name": "real", "File": "expressions/real.exp3.json"},
                    ],
                },
            }), encoding="utf-8")
            (root / "Mini.cdi3.json").write_text(json.dumps({
                "Version": 3,
                "Parameters": [
                    {"Id": "ParamReal", "GroupId": "", "Name": "real"},
                ],
                "Parts": [],
            }), encoding="utf-8")
            (root / "expressions").mkdir()
            (root / "expressions" / "real.exp3.json").write_text(json.dumps({
                "Type": "Live2D Expression",
                "Parameters": [
                    {"Id": "ParamReal", "Value": 30, "Blend": "Add"},
                ],
            }), encoding="utf-8")
            profile = from_disk(root)
        # Only the ``real`` expression should produce a binding;
        # ``ghost`` was referenced but never written and must be
        # silently absent.
        self.assertEqual(set(profile.expression_params.keys()), {"real"})

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


class MouthOverlayParamDetectionTests(unittest.TestCase):
    """Verify the cdi3 walk that flags params drawing a stylised
    mouth overlay (Alexia ``Param54`` "Grin") so the frontend
    ``ExpressionChannel`` can taper them against live audio
    amplitude — fixing the "two mouths at once" regression where a
    grin reaction painted a static toothy mouth on top of the
    flapping lip-sync mouth."""

    def test_mini_fixture_detects_param54_as_mouth_overlay(self) -> None:
        # The mini fixture's cdi3 names Param54 = "咧嘴笑" which the
        # synonym table maps to the grin overlay.
        profile = from_disk(_MIN)
        self.assertIn("Param54", profile.mouth_overlay_param_ids)

    def test_bare_fixture_has_no_mouth_overlay(self) -> None:
        # The bare rig only has ParamMouthOpenY (the actual lip-sync
        # mouth), no grin / smirk overlay. The list must be empty so
        # the channel skips its suppression branch entirely.
        profile = from_disk(_BARE)
        self.assertEqual(profile.mouth_overlay_param_ids, [])

    def test_mouth_overlay_excludes_plain_mouth_open(self) -> None:
        # Critical: ParamMouthOpenY is the LIP-SYNC param, not a
        # mouth overlay. If we ever accidentally matched it, the
        # channel would suppress the very thing it's trying to
        # protect. Both fixtures expose ParamMouthOpenY.
        for fixture in (_MIN, _BARE):
            profile = from_disk(fixture)
            self.assertNotIn(
                "ParamMouthOpenY", profile.mouth_overlay_param_ids,
                f"{fixture.name} fixture leaked the lip-sync param "
                f"into the mouth-overlay set",
            )

    def test_round_trips_through_to_dict(self) -> None:
        profile = from_disk(_MIN)
        encoded = profile.to_dict()
        self.assertIn("mouth_overlay_param_ids", encoded)
        self.assertEqual(encoded["mouth_overlay_param_ids"], ["Param54"])
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


class AlexiaReactionMapRegressionTests(unittest.TestCase):
    """Pin the post-visual-audit mapping so future refactors can't
    silently regress the three concrete bugs we just fixed:

      - ``cry`` must NOT map to ``bbt`` (which is a lollipop prop,
        not a cry overlay) — see ``docs/alexia-model-notes.md`` §3a.
      - ``tired`` must NOT map to ``y`` (which is dizzy / spiral eyes,
        not a weary look). The body-slump in ``AmbientBodyChannel``
        carries weary now.
      - ``confused`` is the new home for ``y``'s dizzy visual.
    """

    def test_cry_does_not_resolve_to_bbt(self) -> None:
        profile = from_disk(_MIN)
        self.assertNotEqual(profile.reaction_mapping.get("cry"), "bbt")

    def test_tired_does_not_resolve_to_y(self) -> None:
        profile = from_disk(_MIN)
        self.assertNotEqual(profile.reaction_mapping.get("tired"), "y")

    def test_confused_resolves_to_y_on_alexia(self) -> None:
        profile = from_disk(_MIN)
        # ``y`` (Param56 = Dizzy) is the spiral-eye overlay — the
        # natural home for the ``confused`` reaction.
        self.assertEqual(profile.reaction_mapping.get("confused"), "y")

    def test_bbt_is_lollipop_capability_not_emotional_reaction(self) -> None:
        # ``bbt`` must surface as a ``has_lollipop`` capability, NOT
        # as the target of any emotional reaction. The third
        # classification finally lands in the accessory tier.
        profile = from_disk(_MIN)
        self.assertTrue(profile.capabilities.get("has_lollipop", False))
        for reaction, expr in profile.reaction_mapping.items():
            self.assertNotEqual(
                expr, "bbt",
                f"reaction {reaction!r} regressed to bbt (lollipop)",
            )


class MouthBlockingExpressionsTests(unittest.TestCase):
    """Verify the cross-reference between ``mouth_overlay_param_ids``
    and ``expression_params`` that flags expressions whose firing
    would paint a stylised mouth shape competing with lip-sync."""

    def test_alexia_lzx_is_flagged_as_mouth_blocker(self) -> None:
        # The mini fixture ships ``lzx.exp3.json`` writing Param54
        # (= 咧嘴笑 = toothy grin). That param is in
        # ``mouth_overlay_param_ids`` so ``lzx`` must surface here.
        profile = from_disk(_MIN)
        self.assertIn("lzx", profile.mouth_blocking_expressions)

    def test_non_mouth_expressions_are_not_flagged(self) -> None:
        # ``lh`` (blush, Param58), ``h`` (sweat, Param44), and ``wh``
        # (question, ParamAngleX) touch no mouth-overlay param and
        # must stay out of the blocker list.
        profile = from_disk(_MIN)
        for name in ("lh", "h", "wh"):
            self.assertNotIn(name, profile.mouth_blocking_expressions)

    def test_empty_when_rig_has_no_mouth_overlay_params(self) -> None:
        # Rigs without a mouth-overlay param at all have no possible
        # blockers, so the list collapses to empty regardless of how
        # many expressions are loaded.
        profile = from_disk(_BARE)
        self.assertEqual(profile.mouth_blocking_expressions, [])


class OutfitGatedExpressionsTests(unittest.TestCase):
    """Verify the exp3 heuristic that marks expressions as
    outfit-gated when they explicitly zero outfit-envelope params
    (Param16 / Param17 on Alexia-style rigs)."""

    def test_alexia_zs1_is_day_clothes_only(self) -> None:
        # ``zs1.exp3.json`` zeroes Param16 + Param17 (the outfit
        # envelope params), signalling the crossed-arms pose only
        # renders against the day_clothes baseline.
        profile = from_disk(_MIN)
        self.assertEqual(
            profile.outfit_gated_expressions.get("zs1"),
            ["day_clothes"],
        )

    def test_non_gated_expressions_have_no_entry(self) -> None:
        # ``bbt`` / ``y`` / ``lh`` etc. don't zero any outfit param,
        # so they're absent from the gate dict (the renderer treats
        # absent = permissive).
        profile = from_disk(_MIN)
        for name in ("bbt", "y", "lh", "h", "wh"):
            self.assertNotIn(name, profile.outfit_gated_expressions)

    def test_outfit_gate_round_trips_through_to_dict(self) -> None:
        # The WS payload uses asdict() so the new field must survive
        # the JSON encode round-trip.
        profile = from_disk(_MIN)
        encoded = profile.to_dict()
        self.assertEqual(
            encoded["outfit_gated_expressions"].get("zs1"),
            ["day_clothes"],
        )
        json.dumps(encoded)


class AvatarOverridesTests(unittest.TestCase):
    """``avatar_overrides.json`` is a per-rig escape hatch for cases
    where the multilingual synonym tables in
    :mod:`app.core.persona.avatar_profile` can't catch the rig's chosen
    parameter names. The motivating case is Alexia's cat ears, which
    live on parameters named ``Hair 5`` / ``Hair 5-1`` / ``Hair 5-2``
    / ``Hair 5-3`` after the cdi3 translation pass — none match
    ``_EAR_SEGMENT_SYNONYMS``, so synonym detection silently skips
    them. The override file pins the IDs explicitly. The mechanism
    deliberately stays narrow: only ``cat_ear_param_ids`` and
    ``cat_tail_param_ids`` are supported in this pass."""

    def _write_min_rig(self, root: Path, *, parameters: list[dict[str, str]]) -> None:
        (root / "Mini.model3.json").write_text(json.dumps({
            "Version": 3,
            "FileReferences": {
                "Moc": "Mini.moc3",
                "DisplayInfo": "Mini.cdi3.json",
            },
        }), encoding="utf-8")
        (root / "Mini.cdi3.json").write_text(json.dumps({
            "Version": 3,
            "Parameters": parameters,
            "Parts": [],
        }), encoding="utf-8")

    def test_override_replaces_synonym_detected_ear_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Rig has ``Hair 5*`` params that the synonym table does
            # NOT recognise as ear segments — without an override we
            # would expect ``cat_ear_param_ids = []`` and
            # ``has_ear_wiggle = False``.
            self._write_min_rig(root, parameters=[
                {"Id": "Param13", "GroupId": "ParamGroup5", "Name": "Hair 5"},
                {"Id": "Param14", "GroupId": "ParamGroup5", "Name": "Hair 5-1"},
                {"Id": "Param15", "GroupId": "ParamGroup5", "Name": "Hair 5-2"},
                {"Id": "Param18", "GroupId": "ParamGroup5", "Name": "Hair 5-3"},
            ])
            (root / "avatar_overrides.json").write_text(json.dumps({
                "cat_ear_param_ids": ["Param13", "Param14", "Param15", "Param18"],
            }), encoding="utf-8")
            profile = from_disk(root)
        self.assertEqual(
            profile.cat_ear_param_ids,
            ["Param13", "Param14", "Param15", "Param18"],
        )
        self.assertTrue(profile.capabilities.get("has_ear_wiggle", False))

    def test_override_can_pin_tail_ids_and_flip_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No tail-named params in the rig, so synonym detection
            # leaves the list empty and ``has_tail_wag`` False.
            self._write_min_rig(root, parameters=[
                {"Id": "ParamX", "GroupId": "", "Name": "Random"},
            ])
            (root / "avatar_overrides.json").write_text(json.dumps({
                "cat_tail_param_ids": [
                    "Param_Angle_Rotation_1_ArtMesh202",
                    "Param_Angle_Rotation_2_ArtMesh202",
                ],
            }), encoding="utf-8")
            profile = from_disk(root)
        self.assertEqual(
            profile.cat_tail_param_ids,
            [
                "Param_Angle_Rotation_1_ArtMesh202",
                "Param_Angle_Rotation_2_ArtMesh202",
            ],
        )
        self.assertTrue(profile.capabilities.get("has_tail_wag", False))
        self.assertTrue(profile.capabilities.get("has_cat_tail", False))

    def test_empty_override_list_clears_detected_values(self) -> None:
        # Explicit ``[]`` is a way to disable a feature on a rig whose
        # synonyms accidentally trigger it. ``has_ear_wiggle`` must
        # follow.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_min_rig(root, parameters=[
                {"Id": "ParamA", "GroupId": "", "Name": "Left ear 1"},
                {"Id": "ParamB", "GroupId": "", "Name": "Right ear 1"},
            ])
            (root / "avatar_overrides.json").write_text(json.dumps({
                "cat_ear_param_ids": [],
            }), encoding="utf-8")
            profile = from_disk(root)
        self.assertEqual(profile.cat_ear_param_ids, [])
        self.assertFalse(profile.capabilities.get("has_ear_wiggle", True))

    def test_missing_override_file_leaves_detection_intact(self) -> None:
        # Without an override file, the bare-rig fixture's behaviour
        # must be unchanged.
        profile = from_disk(_MIN)
        # _MIN already detects the ear params via synonyms; the test
        # is just that loading still works (no spurious error from
        # the optional override loader).
        self.assertTrue(profile.capabilities.get("has_ear_wiggle", False))

    def test_malformed_override_file_is_ignored_with_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_min_rig(root, parameters=[
                {"Id": "ParamA", "GroupId": "", "Name": "Left ear 1"},
            ])
            (root / "avatar_overrides.json").write_text(
                "{not valid json", encoding="utf-8",
            )
            # Loader must not raise — corrupted user-edited files
            # should fall back to synonym detection rather than
            # crashing the whole avatar pipeline.
            profile = from_disk(root)
        # Synonym detection still picked up "Left ear 1".
        self.assertEqual(profile.cat_ear_param_ids, ["ParamA"])

    def test_non_string_override_values_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_min_rig(root, parameters=[
                {"Id": "ParamA", "GroupId": "", "Name": "Left ear 1"},
            ])
            (root / "avatar_overrides.json").write_text(json.dumps({
                "cat_ear_param_ids": [123, None, "Param13"],
            }), encoding="utf-8")
            profile = from_disk(root)
        # Bad types -> override is rejected wholesale, fall back to
        # synonym detection.
        self.assertEqual(profile.cat_ear_param_ids, ["ParamA"])


class AlexiaCatEarOverrideTests(unittest.TestCase):
    """Regression pin on the bundled Alexia override file. The four
    ``Hair 5*`` parameters (``Param13`` / ``Param14`` / ``Param15``
    / ``Param18``) are physics outputs of ``PhysicsSetting13`` and
    ``PhysicsSetting14`` — they animate the ``Cat ears`` part. If
    this file ever drifts, ``[[overlay:ear_wiggle]]`` will silently
    no-op on Alexia."""

    def test_alexia_override_file_pins_canonical_ear_param_ids(self) -> None:
        override_path = (
            Path(__file__).resolve().parent.parent
            / "data" / "personas" / "active" / "Alexia"
            / "avatar_overrides.json"
        )
        self.assertTrue(
            override_path.exists(),
            "data/personas/active/Alexia/avatar_overrides.json missing — "
            "ear_wiggle requires this file on Alexia",
        )
        data = json.loads(override_path.read_text(encoding="utf-8"))
        self.assertEqual(
            data.get("cat_ear_param_ids"),
            ["Param13", "Param14", "Param15", "Param18"],
        )


if __name__ == "__main__":
    unittest.main()

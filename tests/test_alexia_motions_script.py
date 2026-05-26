"""Tests for ``scripts.generate_alexia_motions``.

The script ships hand-tuned head/body curves for a small set of
gestures plus the new B2 backchannel micro-gestures (``tilt_left``,
``tilt_right``, ``microshake``). These tests lock in:

  - The rendered motion3.json shape (Curves / Segments / Meta) for
    each new gesture so a refactor of the renderer doesn't silently
    drift the timing.
  - The model3.json patch is idempotent and routes each gesture to
    its declared group (``Tap`` for one-shots, ``Backchannel`` for
    listening micro-cues).

Tests work in a temporary directory so they don't touch the real
``live-2d-models/Alexia/`` bundle on disk.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import generate_alexia_motions as gen


def _segments_to_keyframes(segments: list) -> list[tuple[float, float]]:
    """Decode a Cubism-style flat ``Segments`` list into ``(t, v)``
    keyframes for assertion. Only handles the linear (type 0)
    segment our author uses; raises if it sees any other code."""
    if len(segments) < 2:
        return []
    out: list[tuple[float, float]] = [(float(segments[0]), float(segments[1]))]
    i = 2
    while i < len(segments):
        seg_type = segments[i]
        if seg_type != 0:
            raise AssertionError(
                f"unexpected non-linear segment type {seg_type!r} at index {i}",
            )
        end_t = float(segments[i + 1])
        end_v = float(segments[i + 2])
        out.append((end_t, end_v))
        i += 3
    return out


class BackchannelMotionRenderingTests(unittest.TestCase):
    """Each new B2 gesture renders to a stable motion3.json shape."""

    def test_tilt_left_renders_six_tenths_of_a_second(self) -> None:
        rendered = gen._render(gen._tilt_left())
        self.assertAlmostEqual(rendered["Meta"]["Duration"], 0.6)
        self.assertEqual(rendered["Meta"]["Loop"], False)
        # Single curve on ParamAngleX with 4 keyframes (start, ramp,
        # hold, return).
        self.assertEqual(rendered["Meta"]["CurveCount"], 1)
        self.assertEqual(rendered["Meta"]["TotalPointCount"], 4)
        self.assertEqual(rendered["Meta"]["TotalSegmentCount"], 3)
        curves = rendered["Curves"]
        self.assertEqual(len(curves), 1)
        self.assertEqual(curves[0]["Id"], "ParamAngleX")
        kfs = _segments_to_keyframes(curves[0]["Segments"])
        self.assertEqual(
            kfs,
            [(0.0, 0.0), (0.25, -8.0), (0.35, -8.0), (0.6, 0.0)],
        )

    def test_tilt_right_mirrors_tilt_left(self) -> None:
        left = gen._render(gen._tilt_left())
        right = gen._render(gen._tilt_right())
        self.assertEqual(left["Meta"], right["Meta"])
        left_kfs = _segments_to_keyframes(left["Curves"][0]["Segments"])
        right_kfs = _segments_to_keyframes(right["Curves"][0]["Segments"])
        # Same time codes; values are mirrored across zero.
        for (lt, lv), (rt, rv) in zip(left_kfs, right_kfs):
            self.assertAlmostEqual(lt, rt)
            self.assertAlmostEqual(lv, -rv)

    def test_microshake_drives_paramAngleZ_with_two_oscillations(self) -> None:
        rendered = gen._render(gen._microshake())
        self.assertAlmostEqual(rendered["Meta"]["Duration"], 0.7)
        self.assertEqual(rendered["Meta"]["CurveCount"], 1)
        self.assertEqual(rendered["Meta"]["TotalPointCount"], 5)
        self.assertEqual(rendered["Meta"]["TotalSegmentCount"], 4)
        curves = rendered["Curves"]
        self.assertEqual(curves[0]["Id"], "ParamAngleZ")
        kfs = _segments_to_keyframes(curves[0]["Segments"])
        self.assertEqual(
            kfs,
            [
                (0.0, 0.0),
                (0.175, -3.0),
                (0.35, 3.0),
                (0.525, -2.0),
                (0.7, 0.0),
            ],
        )

    def test_render_meta_counts_match_curves(self) -> None:
        # Belt-and-braces: re-derive CurveCount / segment / point
        # totals from the Curves array and assert they match the Meta
        # block. A bug in ``_render`` would surface here even if the
        # individual gesture goldens above happen to agree.
        for factory in (gen._tilt_left, gen._tilt_right, gen._microshake):
            rendered = gen._render(factory())
            curves = rendered["Curves"]
            point_total = sum(
                len(_segments_to_keyframes(c["Segments"])) for c in curves
            )
            segment_total = sum(
                max(0, len(_segments_to_keyframes(c["Segments"])) - 1)
                for c in curves
            )
            self.assertEqual(rendered["Meta"]["CurveCount"], len(curves))
            self.assertEqual(rendered["Meta"]["TotalPointCount"], point_total)
            self.assertEqual(rendered["Meta"]["TotalSegmentCount"], segment_total)


class WriteAndPatchTests(unittest.TestCase):
    """``_write_motions`` + ``_patch_model3`` must wire the new B2
    motions into the ``Backchannel`` group while leaving the existing
    ``Tap`` group intact, and stay idempotent on a re-run."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Seed a model3.json with an existing Tap entry so we can
        # check that patching doesn't blow it away.
        self.model3_path = self.root / "Alexia.model3.json"
        self.model3_path.write_text(
            json.dumps({
                "Version": 3,
                "FileReferences": {
                    "Moc": "Alexia.moc3",
                    "Motions": {
                        "": [{"File": "dh.motion3.json"}],
                        "Tap": [{"File": "nod.motion3.json"}],
                    },
                },
            }, indent="\t"),
            encoding="utf-8",
        )

    def test_writes_each_motion_to_disk(self) -> None:
        gestures = [gen._tilt_left(), gen._tilt_right(), gen._microshake()]
        written = gen._write_motions(gestures, motion_dir=self.root)
        names = [n for n, _ in written]
        self.assertEqual(
            sorted(names),
            ["microshake.motion3.json", "tilt_left.motion3.json", "tilt_right.motion3.json"],
        )
        # Each written file is parseable JSON with the expected shape.
        for fname, _group in written:
            data = json.loads((self.root / fname).read_text(encoding="utf-8"))
            self.assertEqual(data["Version"], 3)
            self.assertIn("Curves", data)
            self.assertIn("Meta", data)

    def test_patch_model3_routes_backchannel_motions_to_new_group(self) -> None:
        gestures = [gen._tilt_left(), gen._tilt_right(), gen._microshake()]
        written = gen._write_motions(gestures, motion_dir=self.root)
        added = gen._patch_model3(written, model3_path=self.model3_path)
        self.assertEqual(added, 3)
        body = json.loads(self.model3_path.read_text(encoding="utf-8"))
        motions = body["FileReferences"]["Motions"]
        # Existing Tap group preserved untouched.
        self.assertEqual(
            motions["Tap"], [{"File": "nod.motion3.json"}],
        )
        # New Backchannel group has all three new files.
        files = [entry["File"] for entry in motions["Backchannel"]]
        self.assertEqual(
            sorted(files),
            sorted([
                "tilt_left.motion3.json",
                "tilt_right.motion3.json",
                "microshake.motion3.json",
            ]),
        )

    def test_patch_model3_is_idempotent(self) -> None:
        gestures = [gen._tilt_left(), gen._tilt_right(), gen._microshake()]
        written = gen._write_motions(gestures, motion_dir=self.root)
        added_first = gen._patch_model3(written, model3_path=self.model3_path)
        added_second = gen._patch_model3(written, model3_path=self.model3_path)
        self.assertEqual(added_first, 3)
        # Second run sees identical entries -> nothing to add.
        self.assertEqual(added_second, 0)
        body = json.loads(self.model3_path.read_text(encoding="utf-8"))
        files = [entry["File"] for entry in body["FileReferences"]["Motions"]["Backchannel"]]
        self.assertEqual(len(files), 3)

    def test_main_path_routes_one_shots_to_tap_and_backchannels_to_backchannel(self) -> None:
        # Run the full renderer against the temp tree, with all
        # gestures (Tap + Backchannel buckets).
        all_gestures = [
            gen._nod(), gen._shake(), gen._bow(),
            gen._tilt_left(), gen._tilt_right(), gen._microshake(),
        ]
        written = gen._write_motions(all_gestures, motion_dir=self.root)
        gen._patch_model3(written, model3_path=self.model3_path)
        body = json.loads(self.model3_path.read_text(encoding="utf-8"))
        motions = body["FileReferences"]["Motions"]
        tap_files = sorted(entry["File"] for entry in motions["Tap"])
        bc_files = sorted(entry["File"] for entry in motions["Backchannel"])
        # Tap retained the seed entry plus shake + bow (nod was already there).
        self.assertIn("nod.motion3.json", tap_files)
        self.assertIn("shake.motion3.json", tap_files)
        self.assertIn("bow.motion3.json", tap_files)
        self.assertEqual(
            bc_files,
            ["microshake.motion3.json", "tilt_left.motion3.json", "tilt_right.motion3.json"],
        )


if __name__ == "__main__":
    unittest.main()

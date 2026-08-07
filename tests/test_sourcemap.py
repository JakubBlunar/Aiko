"""Tests for the server-side sourcemap symbolicator.

The value of this module is entirely in whether a *production* crash
stack becomes readable, so the tests build real base64-VLQ maps rather
than mocking the decode. :class:`RealBundleTests` goes further and runs
against ``web/dist`` when a build is present, because a hand-rolled map
proves the decoder self-consistent but not that it agrees with Vite.

The recurring theme in the assertions is **degrade, never lie**: a
missing, stale, truncated or hostile map must leave the stack exactly as
it was rather than emit a plausible-but-wrong file and line.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.infra import sourcemap


_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _vlq(value: int) -> str:
    """Encode one signed integer as base64-VLQ (the inverse of the decoder)."""
    v = ((-value) << 1) | 1 if value < 0 else value << 1
    out = ""
    while True:
        digit = v & 31
        v >>= 5
        if v:
            digit |= 32
        out += _B64[digit]
        if not v:
            return out


def _segment(*fields: int) -> str:
    return "".join(_vlq(f) for f in fields)


def _write_map(
    directory: Path,
    name: str,
    *,
    sources: list[str],
    names: list[str],
    mappings: str,
) -> Path:
    path = directory / f"{name}.map"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "file": name,
                "sources": sources,
                "names": names,
                "mappings": mappings,
            }
        ),
        encoding="utf-8",
    )
    return path


class VlqDecodingTests(unittest.TestCase):
    def test_round_trips_signed_values(self) -> None:
        for value in (0, 1, -1, 15, 16, -16, 1023, -1024, 123456, -123456):
            with self.subTest(value=value):
                self.assertEqual(sourcemap._decode_vlq(_vlq(value)), [value])

    def test_reads_a_multi_field_segment(self) -> None:
        self.assertEqual(
            sourcemap._decode_vlq(_segment(9, 0, 4, 2, 0)),
            [9, 0, 4, 2, 0],
        )

    def test_rejects_a_character_outside_the_alphabet(self) -> None:
        with self.assertRaises(ValueError):
            sourcemap._decode_vlq("!!")


class MappingSemanticsTests(unittest.TestCase):
    """The delta rules are the part that is easy to get subtly wrong."""

    def test_generated_column_resets_each_line_but_source_state_does_not(self) -> None:
        # Line 1: gen col 0 -> src line 0. Line 2: gen col 0 again (reset),
        # with a +5 source-line delta that must accumulate on top of line 1.
        mappings = _segment(0, 0, 0, 0) + ";" + _segment(0, 0, 5, 0)
        rows = sourcemap._parse_mappings(mappings)
        self.assertEqual(rows[0][1][0][0], 0)
        self.assertEqual(rows[0][1][0][2], 0)
        self.assertEqual(rows[1][1][0][0], 0, "generated column must reset")
        self.assertEqual(rows[1][1][0][2], 5, "source line delta must accumulate")

    def test_a_one_field_segment_marks_generated_only_code(self) -> None:
        rows = sourcemap._parse_mappings(_segment(4))
        self.assertEqual(rows[0][1][0][1], -1, "no source index")

    def test_an_empty_group_is_still_a_line(self) -> None:
        rows = sourcemap._parse_mappings(_segment(0, 0, 0, 0) + ";;" + _segment(0, 0, 1, 0))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1], [], "the blank line has no segments")


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        # One generated line with two segments: col 0 and col 20.
        # ``SourceMap`` stores sources verbatim — tidying happens in
        # ``_load_map``, which is where ``sourceRoot`` is known — so these
        # are written the way a load would already have normalised them.
        mappings = _segment(0, 0, 9, 4, 0) + "," + _segment(20, 0, 30, 2)
        self.map = sourcemap.SourceMap(["src/App.tsx"], ["handleClick"], mappings)

    def test_a_position_resolves_to_a_one_based_location(self) -> None:
        hit = self.map.lookup(1, 1)
        assert hit is not None
        source, line, column, name = hit
        self.assertEqual(source, "src/App.tsx")
        self.assertEqual((line, column), (10, 5))
        self.assertEqual(name, "handleClick")

    def test_a_column_inside_a_segment_uses_that_segments_start(self) -> None:
        # Column 15 falls between the two segments, so it belongs to the
        # first — this is the whole reason lookup bisects rather than
        # requiring an exact hit, since a minified frame almost never
        # lands exactly on a mapping boundary.
        hit = self.map.lookup(1, 15)
        assert hit is not None
        self.assertEqual(hit[1], 10)

    def test_the_next_segment_takes_over_at_its_own_column(self) -> None:
        hit = self.map.lookup(1, 21)
        assert hit is not None
        self.assertEqual(hit[1], 40, "second segment: 9 + 30 + 1")
        self.assertEqual(hit[3], "", "no name index on this segment")

    def test_a_line_past_the_end_resolves_to_nothing(self) -> None:
        self.assertIsNone(self.map.lookup(99, 0))

    def test_line_zero_is_out_of_range(self) -> None:
        # Stack frames are 1-based; a 0 means the caller got it wrong and
        # we must not silently read row -1.
        self.assertIsNone(self.map.lookup(0, 0))


class SourcePathTests(unittest.TestCase):
    def test_strips_the_walk_up_to_the_bundle(self) -> None:
        self.assertEqual(sourcemap._tidy_source("../../src/App.tsx"), "src/App.tsx")

    def test_anchors_an_absolute_path_on_the_frontend_root(self) -> None:
        self.assertEqual(
            sourcemap._tidy_source("/home/me/proj/web/src/store.ts"),
            "web/src/store.ts",
        )

    def test_keeps_a_dependency_path_recognisable(self) -> None:
        self.assertEqual(
            sourcemap._tidy_source("../node_modules/react-dom/client.js"),
            "node_modules/react-dom/client.js",
        )

    def test_survives_a_synthesised_module_marker(self) -> None:
        self.assertEqual(sourcemap._tidy_source("\0virtual:thing"), "virtual:thing")


class SymbolicationTests(unittest.TestCase):
    def setUp(self) -> None:
        sourcemap.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.assets = Path(self._tmp.name)
        _write_map(
            self.assets,
            "index-abc123.js",
            sources=["../../src/live2d/channels/ExpressionChannel.ts"],
            names=["applyReaction"],
            # Second generated line, generated column 9.
            mappings=";" + _segment(9, 0, 4, 2, 0),
        )

    def tearDown(self) -> None:
        sourcemap.clear_cache()
        self._tmp.cleanup()

    def _sym(self, stack: str) -> str:
        return sourcemap.symbolicate_stack(stack, assets_dir=self.assets)

    def test_rewrites_a_chrome_frame(self) -> None:
        out = self._sym("    at Ln (http://localhost:6275/assets/index-abc123.js:2:10)")
        self.assertIn("src/live2d/channels/ExpressionChannel.ts:5:3", out)
        self.assertIn("applyReaction", out)

    def test_rewrites_a_firefox_frame(self) -> None:
        # Firefox/Safari use ``fn@url:line:col`` with no parentheses.
        out = self._sym("Xn@http://localhost:6275/assets/index-abc123.js:2:10")
        self.assertIn("ExpressionChannel.ts:5:3", out)

    def test_rewrites_an_anonymous_frame(self) -> None:
        out = self._sym("    at http://localhost:6275/assets/index-abc123.js:2:10")
        self.assertIn("ExpressionChannel.ts:5:3", out)

    def test_rewrites_every_frame_in_a_multi_line_stack(self) -> None:
        stack = "\n".join(
            [
                "TypeError: nope",
                "    at a (http://x/assets/index-abc123.js:2:10)",
                "    at b (http://x/assets/index-abc123.js:2:10)",
            ]
        )
        self.assertEqual(self._sym(stack).count("ExpressionChannel.ts"), 2)

    def test_ignores_a_query_string_on_the_asset_url(self) -> None:
        out = self._sym("at f (http://x/assets/index-abc123.js?v=8:2:10)")
        self.assertIn("ExpressionChannel.ts:5:3", out)

    def test_a_frame_with_no_map_is_left_alone(self) -> None:
        original = "    at Q (http://x/assets/vendor-zzz.js:9:9)"
        self.assertEqual(self._sym(original), original)

    def test_a_stack_with_nothing_mappable_is_returned_verbatim(self) -> None:
        original = "TypeError: nope\n    at Object.<anonymous> (native)"
        self.assertEqual(self._sym(original), original)

    def test_a_missing_assets_directory_is_not_an_error(self) -> None:
        original = "at f (http://x/assets/index-abc123.js:2:10)"
        self.assertEqual(
            sourcemap.symbolicate_stack(original, assets_dir=Path("nope-not-here")),
            original,
        )

    def test_an_unparseable_map_leaves_the_stack_intact(self) -> None:
        (self.assets / "broken-1.js.map").write_text("{not json", encoding="utf-8")
        original = "at f (http://x/assets/broken-1.js:1:0)"
        self.assertEqual(self._sym(original), original)

    def test_a_map_with_a_corrupt_mappings_string_is_refused(self) -> None:
        _write_map(
            self.assets,
            "bad-vlq.js",
            sources=["../../src/App.tsx"],
            names=[],
            mappings="!!!not-vlq!!!",
        )
        original = "at f (http://x/assets/bad-vlq.js:1:0)"
        self.assertEqual(self._sym(original), original)

    def test_a_segment_pointing_outside_the_sources_list_is_refused(self) -> None:
        # A truncated or hand-edited map can index past ``sources``;
        # emitting sources[3] of a 1-entry list would be a lie.
        _write_map(
            self.assets,
            "oob.js",
            sources=["../../src/App.tsx"],
            names=[],
            mappings=_segment(0, 5, 0, 0),
        )
        original = "at f (http://x/assets/oob.js:1:0)"
        self.assertEqual(self._sym(original), original)

    def test_a_traversal_in_the_asset_name_cannot_escape_the_assets_dir(self) -> None:
        original = "at f (http://x/assets/../../../etc/passwd.js:1:0)"
        # Resolved to a bare filename, so at worst it looks for a map that
        # isn't there — never outside the directory.
        self.assertEqual(self._sym(original), original)

    def test_empty_input_is_handled(self) -> None:
        self.assertEqual(sourcemap.symbolicate_stack("", assets_dir=self.assets), "")

    def test_a_rebuilt_map_invalidates_the_cache(self) -> None:
        stack = "at f (http://x/assets/index-abc123.js:2:10)"
        self.assertIn("ExpressionChannel.ts", self._sym(stack))
        # Same filename, different content — mtime/size guard must notice.
        path = _write_map(
            self.assets,
            "index-abc123.js",
            sources=["../../src/OtherFile.ts"],
            names=[],
            mappings=";" + _segment(9, 0, 0, 0),
        )
        import os
        stat = path.stat()
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
        self.assertIn("OtherFile.ts", self._sym(stack))

    def test_reports_whether_anything_changed(self) -> None:
        stack = "at f (http://x/assets/index-abc123.js:2:10)"
        self.assertTrue(sourcemap.stack_is_symbolicated(stack, self._sym(stack)))
        untouched = "at f (native)"
        self.assertFalse(
            sourcemap.stack_is_symbolicated(untouched, self._sym(untouched))
        )

    def test_lists_the_bundles_it_can_map(self) -> None:
        self.assertIn("index-abc123.js", list(sourcemap.available_bundles(self.assets)))


class RealBundleTests(unittest.TestCase):
    """Agreement with Vite's own output, when a build is on disk.

    Skipped rather than failed without ``web/dist`` so the suite still
    runs on a checkout that has never built the frontend. The check is
    deliberately end-to-end: locate a string literal we know the origin
    of, convert its offset in the minified bundle to a line/column, and
    assert the symbolicator names the file it actually came from.
    """

    def setUp(self) -> None:
        sourcemap.clear_cache()
        self.assets = sourcemap.DEFAULT_ASSETS_DIR
        if not self.assets.is_dir():
            self.skipTest("web/dist/assets missing — run `npm run build`")
        bundles = [
            p for p in self.assets.glob("index-*.js")
            if (self.assets / f"{p.name}.map").is_file()
        ]
        if not bundles:
            self.skipTest("no built bundle with a sourcemap")
        self.bundle = bundles[0]
        self.text = self.bundle.read_text(encoding="utf-8", errors="replace")

    def tearDown(self) -> None:
        sourcemap.clear_cache()

    def _frame_for(self, needle: str) -> str | None:
        index = self.text.find(needle)
        if index < 0:
            return None
        line = self.text.count("\n", 0, index) + 1
        column = index - (self.text.rfind("\n", 0, index) + 1) + 1
        return f"    at x (http://localhost:6275/assets/{self.bundle.name}:{line}:{column})"

    def test_a_known_literal_maps_back_to_the_file_it_came_from(self) -> None:
        # The error-boundary heading is a stable, unique string that only
        # ever appears in ErrorBoundary.tsx.
        frame = self._frame_for("Something went wrong")
        if frame is None:
            self.skipTest("marker string not present in this build")
        out = sourcemap.symbolicate_stack(frame, assets_dir=self.assets)
        self.assertIn("ErrorBoundary", out)
        self.assertNotIn(self.bundle.name, out, "the minified location should be gone")


if __name__ == "__main__":
    unittest.main()

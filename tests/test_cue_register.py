"""Tests for the K51 cue-register rotation pure module.

Covers prefix rotation determinism, the bare-shape capitalisation
contract, multi-line blocks, ordinal advancement, the per-turn seed,
and the shared-prefix lint.
"""
from __future__ import annotations

import unittest

from app.core.conversation.cue_register import (
    _SHAPES,
    count_cue_lines,
    lint_shared_prefixes,
    rotate_cue_prefix,
    turn_seed,
)


_CUE = "Heads-up: you just gave a short reply after a long answer."


class TurnSeedTests(unittest.TestCase):
    def test_deterministic_for_same_inputs(self) -> None:
        self.assertEqual(turn_seed("hello", 7), turn_seed("hello", 7))

    def test_varies_with_user_text(self) -> None:
        self.assertNotEqual(turn_seed("hello", 7), turn_seed("goodbye", 7))

    def test_varies_with_history_len(self) -> None:
        # Repeated "ok" turns must still rotate: same text, different
        # history length -> different seed.
        self.assertNotEqual(turn_seed("ok", 3), turn_seed("ok", 4))

    def test_non_negative(self) -> None:
        for text, hist in [("", 0), ("ok", 0), ("x" * 500, 10_000)]:
            self.assertGreaterEqual(turn_seed(text, hist), 0)

    def test_empty_and_none_like_text_safe(self) -> None:
        self.assertEqual(turn_seed("", 0), turn_seed("", 0))


class RotateCuePrefixTests(unittest.TestCase):
    def test_deterministic_for_same_seed_and_ordinal(self) -> None:
        a = rotate_cue_prefix(_CUE, seed=11, ordinal=2)
        b = rotate_cue_prefix(_CUE, seed=11, ordinal=2)
        self.assertEqual(a, b)

    def test_consecutive_ordinals_give_different_shapes(self) -> None:
        seen = {
            rotate_cue_prefix(_CUE, seed=5, ordinal=i)
            for i in range(len(_SHAPES))
        }
        self.assertEqual(len(seen), len(_SHAPES))

    def test_keep_shape_preserves_original(self) -> None:
        # Shape index 0 keeps the literal prefix. seed=0, ordinal=0
        # selects it.
        self.assertEqual(rotate_cue_prefix(_CUE, seed=0, ordinal=0), _CUE)

    def test_bare_shape_capitalises_and_preserves_body(self) -> None:
        # Shape index 3 is the bare register: prefix stripped, first
        # body letter capitalised, rest untouched.
        bare_index = _SHAPES.index("")
        result = rotate_cue_prefix(_CUE, seed=bare_index, ordinal=0)
        self.assertEqual(
            result,
            "You just gave a short reply after a long answer.",
        )

    def test_alternate_prefix_shapes(self) -> None:
        quiet = rotate_cue_prefix(_CUE, seed=1, ordinal=0)
        noticing = rotate_cue_prefix(_CUE, seed=2, ordinal=0)
        self.assertTrue(quiet.startswith("Quiet note: "))
        self.assertTrue(noticing.startswith("Noticing: "))
        for rotated in (quiet, noticing):
            self.assertTrue(
                rotated.endswith(
                    "you just gave a short reply after a long answer.",
                ),
            )

    def test_non_cue_block_untouched(self) -> None:
        block = "Tone shell: lean warm and unhurried."
        for ordinal in range(4):
            self.assertEqual(
                rotate_cue_prefix(block, seed=9, ordinal=ordinal), block,
            )

    def test_empty_block_untouched(self) -> None:
        self.assertEqual(rotate_cue_prefix("", seed=1, ordinal=0), "")

    def test_mid_block_heads_up_not_rewritten(self) -> None:
        # Only lines that *start* with the prefix rotate; a quoted
        # "Heads-up" inside a line is body text.
        block = "Quiet note: he wrote Heads-up: in the doc."
        self.assertEqual(rotate_cue_prefix(block, seed=3, ordinal=0), block)

    def test_multi_line_block_gets_distinct_per_line_shapes(self) -> None:
        # K30 self-noticing joins up to three Heads-up lines in one
        # block — each must land with a different shape.
        block = (
            "Heads-up: line one body.\n"
            "Heads-up: line two body.\n"
            "Heads-up: line three body."
        )
        rotated = rotate_cue_prefix(block, seed=0, ordinal=0)
        lines = rotated.split("\n")
        self.assertEqual(len(lines), 3)
        prefixes = [line.split(" body")[0].rsplit("line", 1)[0] for line in lines]
        self.assertEqual(len(set(prefixes)), 3)

    def test_multi_line_preserves_non_cue_lines(self) -> None:
        block = (
            "Heads-up: line one body.\n"
            "context detail that stays put\n"
            "Heads-up: line two body."
        )
        rotated = rotate_cue_prefix(block, seed=2, ordinal=0)
        self.assertIn("context detail that stays put", rotated.split("\n")[1])

    def test_prefix_only_line_left_alone(self) -> None:
        # Degenerate "Heads-up:" with no body: nothing to reshape.
        block = "Heads-up:"
        bare_index = _SHAPES.index("")
        self.assertEqual(
            rotate_cue_prefix(block, seed=bare_index, ordinal=0), block,
        )


class CountCueLinesTests(unittest.TestCase):
    def test_zero_for_non_cue_block(self) -> None:
        self.assertEqual(count_cue_lines("Tone shell: lean warm."), 0)
        self.assertEqual(count_cue_lines(""), 0)

    def test_counts_only_prefix_lines(self) -> None:
        block = (
            "Heads-up: one.\n"
            "plain line\n"
            "Heads-up: two."
        )
        self.assertEqual(count_cue_lines(block), 2)


class LintSharedPrefixesTests(unittest.TestCase):
    # The histogram keys on the first two words of each block's first
    # line — for real cues that's e.g. "Heads-up: you".

    def test_silent_below_threshold(self) -> None:
        blocks = ["Heads-up: you a.", "Heads-up: you b."]
        self.assertEqual(lint_shared_prefixes(blocks), [])

    def test_fires_above_threshold(self) -> None:
        blocks = [
            "Heads-up: you a.",
            "Heads-up: you b.",
            "Heads-up: you c.",
        ]
        offenders = lint_shared_prefixes(blocks)
        self.assertEqual(offenders, [("Heads-up: you", 3)])

    def test_ignores_empty_blocks(self) -> None:
        blocks = [
            "",
            "  ",
            "Heads-up: you a.",
            "Heads-up: you b.",
            "Heads-up: you c.",
        ]
        offenders = lint_shared_prefixes(blocks)
        self.assertEqual(offenders, [("Heads-up: you", 3)])

    def test_only_first_line_counts(self) -> None:
        block = "Tone shell: lean warm.\nHeads-up: you buried."
        blocks = [block, "Heads-up: you a.", "Heads-up: you b."]
        # Only two blocks *open* with the prefix -> below threshold.
        self.assertEqual(lint_shared_prefixes(blocks), [])

    def test_single_word_lines_ignored(self) -> None:
        self.assertEqual(
            lint_shared_prefixes(["Hello", "Hello", "Hello"]), [],
        )

    def test_custom_threshold(self) -> None:
        blocks = ["Quiet note: a.", "Quiet note: b."]
        offenders = lint_shared_prefixes(blocks, threshold=1)
        self.assertEqual(offenders, [("Quiet note:", 2)])


if __name__ == "__main__":
    unittest.main()

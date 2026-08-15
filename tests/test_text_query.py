"""Tests for the shared search-box matcher.

One module because both the Concepts and Memories panels call it, and the
whole point of it living in one place is that its rules are pinned once.
"""
from __future__ import annotations

import unittest

from app.core.infra.text_query import compile_query


class CompileTests(unittest.TestCase):
    def test_blank_input_compiles_to_nothing(self) -> None:
        # Load-bearing: callers use ``None`` to skip filtering entirely,
        # so an empty box must not become a query that matches no rows.
        for raw in (None, "", "   ", "\t\n"):
            self.assertIsNone(compile_query(raw), raw)

    def test_terms_are_lowercased_and_split(self) -> None:
        q = compile_query("  Bottle   CAP ")
        assert q is not None
        self.assertEqual(q.plain, ("bottle", "cap"))
        self.assertEqual(q.globs, ())

    def test_wildcard_terms_are_separated_out(self) -> None:
        q = compile_query("collect* bottle")
        assert q is not None
        self.assertEqual(q.plain, ("bottle",))
        self.assertEqual(q.globs, ("*collect**",))


class MatchTests(unittest.TestCase):
    def test_case_is_ignored(self) -> None:
        q = compile_query("bottle")
        assert q is not None
        self.assertTrue(q.matches("A Bottle Of Water"))

    def test_every_term_must_appear(self) -> None:
        q = compile_query("bottle cap")
        assert q is not None
        self.assertTrue(q.matches("he collects bottle caps"))
        self.assertFalse(q.matches("he collects bottles"))

    def test_term_order_does_not_matter(self) -> None:
        # The reason this is term-AND rather than substring: word order is
        # exactly what you are least sure of when searching for a memory
        # you only half remember.
        q = compile_query("bottle cap")
        assert q is not None
        self.assertTrue(q.matches("the cap of a bottle"))

    def test_terms_may_land_in_different_fields(self) -> None:
        q = compile_query("bottle cap")
        assert q is not None
        self.assertTrue(q.matches("bottles", "the cap fell off"))

    def test_a_wildcard_reaches_inside_a_word(self) -> None:
        q = compile_query("collect*")
        assert q is not None
        self.assertTrue(q.matches("he is collecting things"))
        self.assertTrue(q.matches("a collection of caps"))
        self.assertFalse(q.matches("he gathers things"))

    def test_a_wildcard_term_is_not_anchored(self) -> None:
        # ``fnmatch`` matches whole strings, so an unwrapped pattern would
        # find "collecting caps" but not "he is collecting caps" -- which
        # reads as the search silently missing rows.
        q = compile_query("collect*caps")
        assert q is not None
        self.assertTrue(q.matches("he is collecting bottle caps now"))

    def test_a_single_char_wildcard_matches_one_character(self) -> None:
        q = compile_query("c?p")
        assert q is not None
        self.assertTrue(q.matches("a cap"))
        self.assertTrue(q.matches("a cup"))
        self.assertFalse(q.matches("a crisp"))

    def test_empty_haystack_never_matches(self) -> None:
        q = compile_query("bottle")
        assert q is not None
        self.assertFalse(q.matches(""))
        self.assertFalse(q.matches(None))
        self.assertFalse(q.matches(None, ""))

    def test_a_bracket_is_a_literal_not_a_pattern(self) -> None:
        # Bracket expressions are not something anyone types into a search
        # box on purpose; treating them as globs would make a stray
        # bracket match nothing at all.
        q = compile_query("[dream]")
        assert q is not None
        self.assertEqual(q.globs, ())
        self.assertTrue(q.matches("[dream] I was flying"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

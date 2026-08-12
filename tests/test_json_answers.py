"""Shape tolerance for worker JSON answers (:mod:`app.llm.json_answers`).

The bug these pin: Ollama's ``format: "json"`` constrains generation to a
JSON *object*, so a prompt asking for a bare array cannot be satisfied.
Models answered ``{}`` (meaning "nothing") or a single unwrapped item, and
a parser that only accepted ``[...]`` read both as failures — which is how
the promise extractor managed 58 LLM calls and 0 promises.

The distinction that matters throughout: ``None`` means *unparseable*
(worth logging as a failure), ``[]`` means *the model had nothing to
report* (a success). Conflating them is what hid the fault.
"""
from __future__ import annotations

import json
import unittest

from app.llm.json_answers import parse_json_array_answer


class ObjectWrappedTests(unittest.TestCase):
    def test_the_documented_shape_parses(self) -> None:
        raw = json.dumps({"promises": [{"who": "user", "what": "call back"}]})
        out = parse_json_array_answer(raw, key="promises")
        assert out is not None
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["what"], "call back")

    def test_empty_array_under_the_key_is_nothing_to_report(self) -> None:
        out = parse_json_array_answer('{"promises": []}', key="promises")
        self.assertEqual(out, [])

    def test_pretty_printed_whitespace_is_fine(self) -> None:
        out = parse_json_array_answer('{\n "promises": []\n}', key="promises")
        self.assertEqual(out, [])

    def test_a_drifted_key_still_yields_its_list(self) -> None:
        # The model wrapped the array under a name we didn't ask for.
        # There is exactly one list in the object, so take it rather than
        # calling a perfectly good answer unparseable.
        out = parse_json_array_answer('{"items": [1, 2]}', key="promises")
        self.assertEqual(out, [1, 2])


class BareObjectTests(unittest.TestCase):
    def test_empty_object_means_nothing_not_failure(self) -> None:
        # Observed verbatim from qwen3.6:27b, whose reasoning trace ended
        # "No concrete promises. Empty array is correct. Output: []" —
        # and then the JSON grammar forced an object.
        self.assertEqual(parse_json_array_answer("{}", key="promises"), [])

    def test_a_single_unwrapped_item_is_recovered(self) -> None:
        # Also observed: a real promise emitted without the wrapper. This
        # was being thrown away.
        raw = json.dumps(
            {"who": "user", "what": "send the staging config", "deadline": None},
        )
        out = parse_json_array_answer(
            raw, key="promises", item_hint_keys=("who", "what", "deadline"),
        )
        assert out is not None
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["what"], "send the staging config")

    def test_an_unrecognised_object_is_unparseable(self) -> None:
        # No list, no item fields: we genuinely don't know what this is,
        # and guessing [] would be a silent success again.
        out = parse_json_array_answer(
            '{"weather": "sunny"}',
            key="promises",
            item_hint_keys=("who", "what"),
        )
        self.assertIsNone(out)


class BareArrayTests(unittest.TestCase):
    def test_bare_array_still_works(self) -> None:
        # Providers without an object grammar (and every existing test
        # fixture) return this shape.
        out = parse_json_array_answer('[{"who": "user"}]', key="promises")
        assert out is not None
        self.assertEqual(out[0]["who"], "user")

    def test_empty_bare_array(self) -> None:
        self.assertEqual(parse_json_array_answer("[]", key="promises"), [])

    def test_array_wrapped_in_prose_is_salvaged(self) -> None:
        raw = 'Sure! Here you go:\n[{"who": "user"}]\nHope that helps.'
        out = parse_json_array_answer(raw, key="promises")
        assert out is not None
        self.assertEqual(len(out), 1)


class FailureTests(unittest.TestCase):
    def test_prose_only_is_unparseable(self) -> None:
        self.assertIsNone(parse_json_array_answer("not json", key="promises"))

    def test_empty_string_is_unparseable(self) -> None:
        # Callers should detect this earlier (it has its own cause: the
        # reasoning trace ate the budget) but the parser must not invent
        # an empty list for it.
        self.assertIsNone(parse_json_array_answer("", key="promises"))
        self.assertIsNone(parse_json_array_answer("   ", key="promises"))

    def test_malformed_json_is_unparseable(self) -> None:
        self.assertIsNone(
            parse_json_array_answer('{"promises": [{"who"', key="promises"),
        )

    def test_a_scalar_is_unparseable(self) -> None:
        self.assertIsNone(parse_json_array_answer("42", key="promises"))


if __name__ == "__main__":
    unittest.main()

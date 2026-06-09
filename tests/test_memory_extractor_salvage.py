"""Tests for the memory-extractor truncation hardening.

Covers the three fixes:
  * ``_salvage_memories`` recovers complete objects from a JSON array
    cut off mid-object (the ``num_predict`` truncation failure mode).
  * ``_parse_response`` falls back to salvage on a JSONDecodeError.
  * the system prompt carries the new count + length constraints.
  * the configurable ``max_tokens`` ceiling is stored.
"""
from __future__ import annotations

import unittest

from app.core.memory.memory_extractor import (
    MemoryExtractor,
    _build_system_prompt,
    _salvage_memories,
)


# A truncated response exactly like the ones seen in data/app.log: the
# array opens, one object closes cleanly, the second is cut mid-string.
_TRUNCATED = (
    '{\n  "memories": [\n'
    '    {"content": "Jacob lives in Prague", "kind": "fact", '
    '"salience": 0.6, "temporal_type": "durable", "event_time": null},\n'
    '    {"content": "Jacob is building a Discord alternative that '
    'supports real-time video streaming, screen sharing, and multiple '
)


class SalvageTests(unittest.TestCase):
    def test_recovers_complete_objects_from_truncation(self) -> None:
        out = _salvage_memories(_TRUNCATED)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], "Jacob lives in Prague")

    def test_recovers_multiple_complete_objects(self) -> None:
        raw = (
            '{"memories": ['
            '{"content": "A", "kind": "fact"},'
            '{"content": "B", "kind": "preference"},'
            '{"content": "C", "kind": "fac'  # cut mid-object
        )
        out = _salvage_memories(raw)
        self.assertEqual([o["content"] for o in out], ["A", "B"])

    def test_handles_braces_inside_strings(self) -> None:
        raw = (
            '{"memories": ['
            '{"content": "uses {curly} braces", "kind": "fact"},'
            '{"content": "next one cut'
        )
        out = _salvage_memories(raw)
        self.assertEqual(len(out), 1)
        self.assertIn("curly", out[0]["content"])

    def test_no_array_returns_empty(self) -> None:
        self.assertEqual(_salvage_memories("totally not json"), [])
        self.assertEqual(_salvage_memories(""), [])


class ParseResponseSalvageTests(unittest.TestCase):
    def _extractor(self) -> MemoryExtractor:
        # _parse_response / _validate_entries don't touch db/store/embedder/
        # ollama, so placeholders are fine.
        return MemoryExtractor(
            object(), object(), object(), object(), model="x",
        )

    def test_parse_response_salvages_truncated(self) -> None:
        ext = self._extractor()
        out = ext._parse_response(_TRUNCATED)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], "Jacob lives in Prague")
        self.assertEqual(out[0]["kind"], "fact")

    def test_parse_response_clean_json_unaffected(self) -> None:
        ext = self._extractor()
        raw = '{"memories": [{"content": "Jacob likes tea", "kind": "preference"}]}'
        out = ext._parse_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "preference")


class PromptAndConfigTests(unittest.TestCase):
    def test_prompt_carries_count_and_length_caps(self) -> None:
        prompt = _build_system_prompt("Jacob", max_memories=3)
        self.assertIn("AT MOST 3", prompt)
        self.assertIn("120", prompt)

    def test_max_tokens_configurable(self) -> None:
        ext = MemoryExtractor(
            object(), object(), object(), object(),
            model="x", max_tokens=2048,
        )
        self.assertEqual(ext._max_tokens, 2048)

    def test_max_tokens_floor(self) -> None:
        ext = MemoryExtractor(
            object(), object(), object(), object(),
            model="x", max_tokens=10,
        )
        self.assertEqual(ext._max_tokens, 256)


if __name__ == "__main__":
    unittest.main()

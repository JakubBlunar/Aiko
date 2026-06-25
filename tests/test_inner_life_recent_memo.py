"""Tests for the P22 shared recent-history memo.

``InnerLifePart1Mixin._inner_life_recent_messages`` lets the K23
misattunement, K30 self-noticing and K54 topic-appetite providers share
a single ``get_messages`` read within one prompt assembly instead of each
issuing their own overlapping query. Correctness comes from keying the
cache on the assembler's per-assembly ``_assembly_seq`` plus the active
session key.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from app.core.session.inner_life_part1 import InnerLifePart1Mixin


@dataclass(frozen=True, slots=True)
class _Row:
    role: str
    content: str


class _CountingDb:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows
        self.calls: list[int] = []

    def get_messages(self, session_id: str, *, limit: int | None = None):  # noqa: ARG002
        self.calls.append(limit if limit is not None else -1)
        if limit is None:
            return list(self._rows)
        return list(self._rows[-limit:])


class _Host(InnerLifePart1Mixin):
    def __init__(self, rows: list[_Row]) -> None:
        self._chat_db = _CountingDb(rows)
        self.session_key = "sess-1"
        self._prompt_assembler = SimpleNamespace(_assembly_seq=1)
        self._inner_life_msg_cache = None


def _rows(n: int) -> list[_Row]:
    return [
        _Row(role="assistant" if i % 2 else "user", content=f"m{i}")
        for i in range(n)
    ]


class SharedMemoTests(unittest.TestCase):
    def test_overlapping_reads_collapse_to_one_query(self) -> None:
        host = _Host(_rows(40))
        # Default windows: misattunement 6, self-noticing/topic-appetite 24.
        first = host._inner_life_recent_messages(6)
        second = host._inner_life_recent_messages(24)
        third = host._inner_life_recent_messages(24)
        # The floor (24) means the first 6-row request already fetches 24,
        # so the two 24-row callers hit the memo.
        self.assertEqual(host._chat_db.calls, [24])
        # Same backing rows handed to every caller.
        self.assertIs(first, second)
        self.assertIs(second, third)

    def test_new_assembly_seq_invalidates(self) -> None:
        host = _Host(_rows(40))
        host._inner_life_recent_messages(24)
        self.assertEqual(host._chat_db.calls, [24])
        # A fresh assembly bumps the seq -> the next read misses.
        host._prompt_assembler._assembly_seq = 2
        host._inner_life_recent_messages(24)
        self.assertEqual(host._chat_db.calls, [24, 24])

    def test_larger_window_refetches(self) -> None:
        host = _Host(_rows(80))
        host._inner_life_recent_messages(24)
        host._inner_life_recent_messages(40)  # wider than cached -> refetch
        self.assertEqual(host._chat_db.calls, [24, 40])

    def test_no_assembler_skips_cache(self) -> None:
        host = _Host(_rows(40))
        host._prompt_assembler = None  # outside an assembly
        host._inner_life_recent_messages(24)
        host._inner_life_recent_messages(24)
        # No seq -> never trusts the memo, so both reads hit the db.
        self.assertEqual(host._chat_db.calls, [24, 24])

    def test_floor_applies_to_small_request(self) -> None:
        host = _Host(_rows(40))
        host._inner_life_recent_messages(6)
        self.assertEqual(host._chat_db.calls, [24])


if __name__ == "__main__":
    unittest.main()

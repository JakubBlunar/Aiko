"""Unit tests for the H9 diary facade on :class:`MemoryFacadeMixin`.

Exercises the real ``list_diary`` / ``diary_count`` / ``_diary_kinds``
methods against a tiny fake memory store, so the kind allow-list
clamping, newest-first sort, and pagination are covered without the
full ``SessionController`` startup cost.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from app.core.session.memory_facade_mixin import MemoryFacadeMixin


@dataclass
class _FakeMemory:
    id: int
    kind: str
    content: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "created_at": self.created_at,
        }


class _FakeStore:
    def __init__(self, rows: list[_FakeMemory]) -> None:
        self.rows = rows

    def iter_by_kinds(self, kinds: Any) -> list[_FakeMemory]:
        kind_set = {str(k).strip().lower() for k in kinds}
        return [m for m in self.rows if m.kind in kind_set]


class _Host(MemoryFacadeMixin):
    """Minimal host exposing only what the diary methods touch."""

    def __init__(self, store: Any) -> None:
        self._memory_store = store


def _sample_rows() -> list[_FakeMemory]:
    return [
        _FakeMemory(1, "reflection", "[dream] orchard", "2026-01-01T08:00:00+00:00"),
        _FakeMemory(2, "fact", "Jacob likes tea", "2026-01-02T08:00:00+00:00"),
        _FakeMemory(3, "reflection", "[mindmap] mostly work", "2026-01-03T08:00:00+00:00"),
        _FakeMemory(4, "shared_moment", "we laughed", "2026-01-04T08:00:00+00:00"),
        _FakeMemory(5, "open_question", "what's his sister's name?", "2026-01-05T08:00:00+00:00"),
        _FakeMemory(6, "preference", "prefers mornings", "2026-01-06T08:00:00+00:00"),
        _FakeMemory(7, "diary", "Today felt close.", "2026-01-07T08:00:00+00:00"),
    ]


class DiaryFacadeTests(unittest.TestCase):
    def test_only_journal_kinds_surface(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        out = host.list_diary(limit=50)
        kinds = {row["kind"] for row in out}
        self.assertEqual(
            kinds, {"diary", "reflection", "shared_moment", "open_question"},
        )
        self.assertNotIn("fact", kinds)
        self.assertNotIn("preference", kinds)

    def test_newest_first(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        out = host.list_diary(limit=50)
        ids = [row["id"] for row in out]
        # id 7 (Jan 07, diary) is newest journal row; 5, 4, 3, 1 follow.
        self.assertEqual(ids, [7, 5, 4, 3, 1])

    def test_kind_filter_clamped_to_journal_allow_list(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        # A non-journal kind must NOT leak factual rows — it falls back
        # to the full journal set instead.
        out = host.list_diary(limit=50, kind="fact")
        self.assertEqual({row["kind"] for row in out}, {
            "diary", "reflection", "shared_moment", "open_question",
        })

    def test_kind_filter_narrows_to_diary_entries(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        out = host.list_diary(limit=50, kind="diary")
        self.assertEqual({row["kind"] for row in out}, {"diary"})
        self.assertEqual(host.diary_count(kind="diary"), 1)

    def test_kind_filter_narrows_to_one_journal_kind(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        out = host.list_diary(limit=50, kind="reflection")
        self.assertEqual({row["kind"] for row in out}, {"reflection"})
        self.assertEqual(host.diary_count(kind="reflection"), 2)

    def test_pagination(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        page1 = host.list_diary(limit=2, offset=0)
        page2 = host.list_diary(limit=2, offset=2)
        self.assertEqual([r["id"] for r in page1], [7, 5])
        self.assertEqual([r["id"] for r in page2], [4, 3])

    def test_count_matches_total_journal_rows(self) -> None:
        host = _Host(_FakeStore(_sample_rows()))
        self.assertEqual(host.diary_count(), 5)

    def test_no_store_is_empty(self) -> None:
        host = _Host(None)
        self.assertEqual(host.list_diary(), [])
        self.assertEqual(host.diary_count(), 0)


if __name__ == "__main__":
    unittest.main()

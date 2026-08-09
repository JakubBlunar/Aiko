"""K85b — the pursuit_note kind and the three things that write one.

The interesting behaviour is the bar, not the write: a beat that left no
trace on her room is a real beat and belongs in the journal ring, but
filing it here would bury the ones worth mining under ambient weather.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

import numpy as np

from app.core.memory.memory_store import VALID_KINDS
from app.core.memory.pursuit_notes import (
    PURSUIT_NOTE_KIND,
    PursuitNoteWriter,
)


_NOW = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)


class _Embedder:
    def embed(self, text: str) -> Any:
        return np.zeros(4, dtype=np.float32)


class _BrokenEmbedder:
    def embed(self, text: str) -> Any:
        raise RuntimeError("cold")


class _FakeMemory:
    def __init__(self, mem_id: int) -> None:
        self.id = mem_id


class _FakeStore:
    """Records adds; ``dedupe`` makes the next one return ``None``."""

    def __init__(self, *, dedupe: bool = False, boom: bool = False) -> None:
        self.adds: list[dict[str, Any]] = []
        self._dedupe = dedupe
        self._boom = boom
        self._next_id = 1

    def add(self, **kwargs: Any) -> Any:
        if self._boom:
            raise RuntimeError("locked")
        self.adds.append(kwargs)
        if self._dedupe:
            return None
        mem = _FakeMemory(self._next_id)
        self._next_id += 1
        return mem


class KindTests(unittest.TestCase):
    def test_the_kind_is_registered(self) -> None:
        self.assertIn(PURSUIT_NOTE_KIND, VALID_KINDS)

    def test_it_reads_as_a_journal_kind(self) -> None:
        from app.core.session.memory_facade_mixin import MemoryFacadeMixin

        self.assertIn(PURSUIT_NOTE_KIND, MemoryFacadeMixin.DIARY_KINDS)


class WriterTests(unittest.TestCase):
    def _writer(self, store: _FakeStore) -> PursuitNoteWriter:
        return PursuitNoteWriter(store, _Embedder())  # type: ignore[arg-type]

    def test_a_note_lands_long_term_with_its_provenance(self) -> None:
        store = _FakeStore()
        note_id = self._writer(store).write(
            "Nine chapters into the sci-fi series.",
            source="hobby_milestone",
            topic="scifi_series",
            at=_NOW,
        )
        self.assertEqual(note_id, 1)
        call = store.adds[0]
        self.assertEqual(call["kind"], PURSUIT_NOTE_KIND)
        self.assertEqual(call["tier"], "long_term")
        self.assertEqual(call["provenance"], "inferred")
        meta = call["metadata"]
        self.assertEqual(meta["source"], "hobby_milestone")
        self.assertEqual(meta["topic"], "scifi_series")
        self.assertEqual(meta["noted_at"], "2026-08-09T10:00:00+00:00")

    def test_extra_metadata_rides_along(self) -> None:
        store = _FakeStore()
        self._writer(store).write(
            "Watered the lettuce, which really needed it.",
            source="away_beat",
            at=_NOW,
            extra={"keys": ["garden_tend"]},
        )
        self.assertEqual(store.adds[0]["metadata"]["keys"], ["garden_tend"])

    def test_a_scrap_of_text_is_not_a_note(self) -> None:
        store = _FakeStore()
        self.assertIsNone(
            self._writer(store).write("hm", source="away_beat", at=_NOW)
        )
        self.assertEqual(store.adds, [])

    def test_a_dedupe_reads_as_nothing_new(self) -> None:
        store = _FakeStore(dedupe=True)
        self.assertIsNone(
            self._writer(store).write(
                "Watered the lettuce again.", source="away_beat", at=_NOW,
            )
        )

    def test_a_cold_embedder_never_raises(self) -> None:
        store = _FakeStore()
        writer = PursuitNoteWriter(
            store, _BrokenEmbedder(),  # type: ignore[arg-type]
        )
        self.assertIsNone(
            writer.write("Read another chapter.", source="away_beat")
        )
        self.assertEqual(store.adds, [])

    def test_a_failing_store_never_raises(self) -> None:
        store = _FakeStore(boom=True)
        self.assertIsNone(
            self._writer(store).write(
                "Read another chapter tonight.", source="away_beat",
            )
        )


class _Notes:
    """Captures writes without going near the memory layer."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    def write(
        self,
        content: str,
        *,
        source: str,
        topic: str = "",
        at: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        self.written.append(
            {
                "content": content,
                "source": source,
                "topic": topic,
                "extra": extra or {},
            }
        )
        return len(self.written)


class HobbyNoteTests(unittest.TestCase):
    def _worker(self, notes: _Notes | None) -> Any:
        from app.core.proactive.hobby_worker import HobbyWorker
        from types import SimpleNamespace

        kv: dict[str, str] = {}
        db = SimpleNamespace(
            kv_get=kv.get,
            kv_set=lambda k, v: kv.__setitem__(k, v),
        )
        return HobbyWorker(
            chat_db=db,  # type: ignore[arg-type]
            agent_settings=SimpleNamespace(hobby_worker_enabled=True),
            memory_settings=SimpleNamespace(
                hobby_max_advances=12,
                hobby_milestone_every=3,
                hobby_advance_min_hours=0.0,
            ),
            user_display_name_provider=lambda: "Jacob",
            pursuit_notes=notes,  # type: ignore[arg-type]
        )

    def _state(self, **over: Any) -> dict[str, Any]:
        state = {
            "key": "scifi_series",
            "label": "working through a sci-fi series",
            "kind": "reading",
            "unit": "chapter",
            "progress": 8,
            "advances": 2,
            "started_at": "2026-07-01T10:00:00+00:00",
            "last_advanced_at": None,
        }
        state.update(over)
        return state

    def test_a_milestone_advance_is_kept(self) -> None:
        notes = _Notes()
        worker = self._worker(notes)
        worker._advance_hobby(_NOW, self._state())
        self.assertEqual(len(notes.written), 1)
        entry = notes.written[0]
        self.assertEqual(entry["source"], "hobby_milestone")
        self.assertEqual(entry["topic"], "scifi_series")
        self.assertIn("9 chapters into", entry["content"])
        self.assertIn("sci-fi series", entry["content"])

    def test_an_ordinary_advance_is_not(self) -> None:
        notes = _Notes()
        worker = self._worker(notes)
        worker._advance_hobby(_NOW, self._state(advances=0, progress=0))
        self.assertEqual(notes.written, [])

    def test_the_finished_thread_survives_rotation(self) -> None:
        notes = _Notes()
        worker = self._worker(notes)
        worker._rotate_hobby(_NOW, self._state(progress=12, advances=12))
        self.assertEqual(len(notes.written), 1)
        entry = notes.written[0]
        self.assertEqual(entry["source"], "hobby_wrapup")
        self.assertIn("Wrapped up working through a sci-fi series", entry["content"])
        self.assertIn("12 chapters", entry["content"])

    def test_a_single_chapter_is_singular(self) -> None:
        notes = _Notes()
        worker = self._worker(notes)
        worker._rotate_hobby(_NOW, self._state(progress=1, advances=12))
        self.assertIn("after 1 chapter.", notes.written[0]["content"])

    def test_no_writer_is_not_an_error(self) -> None:
        worker = self._worker(None)
        result = worker._advance_hobby(_NOW, self._state())
        self.assertTrue(result["advanced"])


if __name__ == "__main__":
    unittest.main()

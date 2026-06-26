"""Tests for the Phase 2c :class:`CatchphraseMiner`.

Three contract surfaces:

  * ``_harvest_candidates`` finds the right n-grams (3-7 word phrases
    used by *both* sides at least N times) and rejects pure-stoplist
    or one-sided fillers.
  * ``CatchphraseMiner.maybe_run`` persists a top-K set of candidates
    as ``kind="catchphrase"`` :class:`Memory` rows and respects its
    throttle.
  * The miner is a no-op without a memory store / embedder.
"""
from __future__ import annotations

import hashlib
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.memory.catchphrase_miner import (
    CatchphraseMiner,
    _harvest_candidates,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore


@dataclass
class _Row:
    role: str
    content: str


class _FakeEmbedder:
    def embed(self, text: str) -> np.ndarray:
        # Stable hash (not Python's per-process-seeded ``hash()``) so the
        # embedding for a given phrase is identical across runs and test
        # orders — keeps the miner's dedup deterministic under pytest-randomly.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:4], "little")
        rng = np.random.default_rng(seed)
        return rng.normal(size=8).astype(np.float32)


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.path)
        self.memory = MemoryStore(self.path, max_memories=100, dedupe_threshold=0.999)
        self.embedder = _FakeEmbedder()

    def close(self) -> None:
        try:
            self.memory.close()
        except Exception:
            pass
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def add_messages(self, items: list[tuple[str, str]]) -> None:
        for role, content in items:
            self.db.add_message(
                session_id="s1",
                role=role,
                content=content,
                token_count=max(1, len(content.split())),
            )


class HarvestCandidatesTests(unittest.TestCase):
    def test_picks_phrase_used_by_both_sides(self) -> None:
        rows = [
            _Row("user", "fish-shaped cookie time again"),
            _Row("assistant", "yes, fish-shaped cookie time it is"),
            _Row("user", "we deserve another fish-shaped cookie time today"),
        ]
        cands = _harvest_candidates(rows, min_total_count=3)
        # The 3-gram "fish-shaped cookie time" should make it (count=3).
        phrases = [c.phrase for c in cands]
        self.assertTrue(
            any("fish-shaped cookie time" in p for p in phrases),
            f"got phrases: {phrases}",
        )

    def test_rejects_one_sided_filler(self) -> None:
        rows = [
            _Row("user", "you know what I mean here right"),
            _Row("user", "you know what I mean really now"),
            _Row("user", "you know what I mean honestly anyway"),
            _Row("assistant", "got it"),
        ]
        cands = _harvest_candidates(rows, min_total_count=2)
        # "you know what" / "you know what i" should be filtered:
        # ``you`` and ``i`` and ``what`` are stopwords, leaving 0 content
        # words above threshold AND assistant_count == 0.
        for c in cands:
            self.assertNotEqual(c.assistant_count, 0)

    def test_rejects_pure_stopword_ngram(self) -> None:
        # Every word in this sentence is in the miner's stoplist, so
        # NO candidate should make it through the meaningful-content
        # filter even though both sides repeat it verbatim.
        rows = [
            _Row("user", "yeah right okay so but and"),
            _Row("assistant", "yeah right okay so but and"),
            _Row("user", "yeah right okay so but and"),
        ]
        cands = _harvest_candidates(rows, min_total_count=2)
        self.assertEqual(cands, [], f"unexpected candidates: {cands}")

    def test_empty_history_returns_empty(self) -> None:
        self.assertEqual(_harvest_candidates([], min_total_count=2), [])


class CatchphraseMinerPersistenceTests(unittest.TestCase):
    def _make_miner(self, fx: _Fixture, **overrides) -> CatchphraseMiner:
        kwargs = dict(
            chat_db=fx.db,
            memory_store=fx.memory,
            embedder=fx.embedder,
            history_window=50,
            min_n=3,
            max_n=5,
            min_total_count=3,
            require_both_sides=True,
            max_writes_per_run=3,
            min_seconds_between=0.0,
            min_new_user_turns=0,
        )
        kwargs.update(overrides)
        return CatchphraseMiner(**kwargs)

    def test_persists_top_candidates(self) -> None:
        f = _Fixture()
        try:
            f.add_messages([
                ("user", "fish-shaped cookie time again"),
                ("assistant", "yes fish-shaped cookie time again"),
                ("user", "still going for fish-shaped cookie time"),
                ("assistant", "always fish-shaped cookie time around here"),
            ])
            miner = self._make_miner(f)
            written = miner.maybe_run(session_key="s1")
            self.assertGreaterEqual(written, 1)
            top = f.memory.list_top(limit=10)
            phrases = [m.content for m in top if m.kind == "catchphrase"]
            self.assertTrue(
                any("fish-shaped cookie time" in p for p in phrases),
                f"got: {phrases}",
            )
        finally:
            f.close()

    def test_throttle_blocks_double_run(self) -> None:
        f = _Fixture()
        try:
            f.add_messages([
                ("user", "level up time again"),
                ("assistant", "level up time it is"),
                ("user", "another level up time today"),
                ("assistant", "perfect level up time then"),
            ])
            miner = self._make_miner(f, min_seconds_between=600.0)
            first = miner.maybe_run(session_key="s1")
            self.assertGreaterEqual(first, 1)
            second = miner.maybe_run(session_key="s1")
            self.assertEqual(second, 0)
            self.assertGreaterEqual(miner.stats()["skipped_throttled"], 1)
        finally:
            f.close()

    def test_no_op_without_memory_or_embedder(self) -> None:
        f = _Fixture()
        try:
            miner = CatchphraseMiner(
                chat_db=f.db,
                memory_store=None,
                embedder=None,
                min_seconds_between=0.0,
                min_new_user_turns=0,
            )
            self.assertEqual(miner.maybe_run(session_key="s1"), 0)
            self.assertGreaterEqual(miner.stats()["skipped_disabled"], 1)
        finally:
            f.close()

    def test_min_new_user_turns_throttle(self) -> None:
        f = _Fixture()
        try:
            f.add_messages([
                ("user", "level up time again"),
                ("assistant", "yes level up time"),
            ])
            miner = self._make_miner(
                f, min_seconds_between=0.0, min_new_user_turns=10,
            )
            written = miner.maybe_run(session_key="s1")
            self.assertEqual(written, 0)
        finally:
            f.close()

    def test_subsumed_phrase_not_double_promoted(self) -> None:
        """If 'fish-shaped cookie time' already exists, we should not
        promote 'fish-shaped cookie time again' as a near-duplicate."""
        f = _Fixture()
        try:
            f.add_messages([
                ("user", "fish-shaped cookie time again"),
                ("assistant", "fish-shaped cookie time again"),
                ("user", "fish-shaped cookie time again you bet"),
                ("assistant", "fish-shaped cookie time again indeed"),
            ])
            miner = self._make_miner(f)
            miner.maybe_run(session_key="s1")
            top = f.memory.list_top(limit=10)
            catch = [m for m in top if m.kind == "catchphrase"]
            phrases = [m.content for m in catch]
            seen = {p for p in phrases}
            # Either only the canonical short form survives, OR the
            # longer version may exist on its own — but we should not
            # have BOTH the short and long versions of the SAME phrase.
            short = [p for p in seen if p == "fish-shaped cookie time"]
            long_ = [p for p in seen if p == "fish-shaped cookie time again"]
            self.assertFalse(bool(short) and bool(long_), msg=str(phrases))
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()

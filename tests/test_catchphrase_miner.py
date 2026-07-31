"""Tests for the Phase 2c :class:`CatchphraseMiner`.

Three contract surfaces:

  * ``_harvest_candidates`` finds the right n-grams (3-7 word phrases
    used by *both* sides at least N times) and rejects pure-stoplist
    or one-sided fillers.
  * ``CatchphraseMiner.maybe_run`` persists a top-K set of candidates
    as ``kind="catchphrase"`` :class:`Memory` rows and respects its
    throttle.
  * The miner is a no-op without a memory store / embedder.

K80's fast path (``detect_inside_joke_birth`` / ``bless_inside_joke`` /
``render_inside_joke_block``) is covered at the bottom: the echo + laugh
gate, the once-per-bit rule, and the two writes a birth produces.
"""
from __future__ import annotations

import hashlib
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.memory.catchphrase_miner import (
    CatchphraseMiner,
    bless_inside_joke,
    detect_inside_joke_birth,
    render_inside_joke_block,
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

    def test_records_who_said_it_first(self) -> None:
        """K26 provenance: whoever used the phrase first in the window."""
        rows = [
            _Row("user", "that is deeply cursed behaviour"),
            _Row("assistant", "deeply cursed behaviour indeed"),
            _Row("user", "more deeply cursed behaviour today"),
        ]
        cands = _harvest_candidates(rows, min_total_count=3)
        match = [c for c in cands if "deeply cursed behaviour" in c.phrase]
        self.assertTrue(match, f"got: {[c.phrase for c in cands]}")
        self.assertEqual(match[0].first_speaker, "user")

    def test_provenance_follows_the_earlier_speaker(self) -> None:
        rows = [
            _Row("assistant", "deeply cursed behaviour indeed"),
            _Row("user", "that is deeply cursed behaviour"),
            _Row("user", "more deeply cursed behaviour today"),
        ]
        cands = _harvest_candidates(rows, min_total_count=3)
        match = [c for c in cands if "deeply cursed behaviour" in c.phrase]
        self.assertEqual(match[0].first_speaker, "assistant")


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

    def test_write_carries_provenance_metadata(self) -> None:
        """K26 reads ``metadata.origin`` to decide what Aiko may adopt."""
        f = _Fixture()
        try:
            f.add_messages([
                ("user", "fish-shaped cookie time again"),
                ("assistant", "yes fish-shaped cookie time again"),
                ("user", "still going for fish-shaped cookie time"),
                ("assistant", "always fish-shaped cookie time around here"),
            ])
            self._make_miner(f).maybe_run(session_key="s1")
            catch = [
                m for m in f.memory.list_top(limit=10)
                if m.kind == "catchphrase"
            ]
            self.assertTrue(catch)
            for mem in catch:
                self.assertEqual(mem.metadata.get("origin"), "user")
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


# ── K80: inside-joke birth (the fast path) ──────────────────────────


_AIKO_LINE = "that is peak fish-shaped cookie energy right there"


class InsideJokeBirthTests(unittest.TestCase):
    def _detect(self, user_text: str, **kw):
        params = dict(
            user_text=user_text,
            origins=[(11, _AIKO_LINE)],
            laughed_ids=frozenset(),
        )
        params.update(kw)
        return detect_inside_joke_birth(**params)

    def test_echo_with_laughter_in_text_is_a_birth(self) -> None:
        birth = self._detect("lol fish-shaped cookie energy, I'm dying")
        self.assertIsNotNone(birth)
        self.assertIn("fish-shaped cookie energy", birth.phrase)
        self.assertTrue(birth.amused)
        self.assertFalse(birth.laughed)
        self.assertEqual(birth.origin_message_id, 11)
        self.assertEqual(birth.lag_turns, 0)

    def test_echo_with_a_laugh_reaction_is_a_birth(self) -> None:
        birth = self._detect(
            "fish-shaped cookie energy it is", laughed_ids={11},
        )
        self.assertIsNotNone(birth)
        self.assertTrue(birth.laughed)

    def test_echo_without_amusement_is_not_a_birth(self) -> None:
        # Repeating a phrase back is just how conversations work; the
        # slow miner handles anything that genuinely recurs.
        self.assertIsNone(self._detect("fish-shaped cookie energy, sure"))

    def test_laughter_without_an_echo_is_not_a_birth(self) -> None:
        self.assertIsNone(self._detect("lol that's so true"))

    def test_already_known_phrase_is_not_reborn(self) -> None:
        self.assertIsNone(
            self._detect(
                "haha fish-shaped cookie energy again",
                known_phrases=["fish-shaped cookie energy"],
            )
        )

    def test_subsumed_known_phrase_blocks_the_longer_form(self) -> None:
        self.assertIsNone(
            self._detect(
                "haha peak fish-shaped cookie energy",
                known_phrases=["fish-shaped cookie"],
            )
        )

    def test_prefers_the_longest_echoed_phrase(self) -> None:
        birth = self._detect("lol peak fish-shaped cookie energy right there")
        self.assertIsNotNone(birth)
        self.assertGreaterEqual(len(birth.phrase.split()), 4)

    def test_prefers_the_most_recent_origin(self) -> None:
        birth = detect_inside_joke_birth(
            user_text="haha fish-shaped cookie energy and level up time",
            origins=[
                (22, "pure fish-shaped cookie energy honestly"),
                (11, "classic level up time for you"),
            ],
            laughed_ids=frozenset(),
        )
        self.assertIsNotNone(birth)
        self.assertEqual(birth.origin_message_id, 22)
        self.assertEqual(birth.lag_turns, 0)

    def test_reaches_back_a_turn_when_the_laugh_is_there(self) -> None:
        birth = detect_inside_joke_birth(
            user_text="okay but level up time though",
            origins=[(22, "anyway how did the deploy go"), (11, "classic level up time")],
            laughed_ids={11},
        )
        self.assertIsNotNone(birth)
        self.assertEqual(birth.lag_turns, 1)
        self.assertEqual(birth.origin_message_id, 11)

    def test_stopword_echo_is_rejected(self) -> None:
        self.assertIsNone(
            detect_inside_joke_birth(
                user_text="haha yeah right okay so but and",
                origins=[(11, "yeah right okay so but and")],
                laughed_ids=frozenset(),
            )
        )

    def test_empty_inputs(self) -> None:
        self.assertIsNone(
            detect_inside_joke_birth(
                user_text="", origins=[(11, _AIKO_LINE)],
            )
        )
        self.assertIsNone(
            detect_inside_joke_birth(user_text="lol whatever", origins=[]),
        )

    def test_amusement_marker_must_be_a_word(self) -> None:
        # "hallo" / "shall" contain "ha"/"hal" but nobody is laughing.
        self.assertIsNone(
            self._detect("shall we do fish-shaped cookie energy again"),
        )


class InsideJokeRenderTests(unittest.TestCase):
    def _birth(self, **kw):
        return detect_inside_joke_birth(
            user_text=kw.pop("user_text", "lol fish-shaped cookie energy"),
            origins=[(11, _AIKO_LINE)],
            **kw,
        )

    def test_names_the_phrase_and_the_user(self) -> None:
        out = render_inside_joke_block(self._birth(), user_display_name="Jacob")
        self.assertIn("fish-shaped cookie energy", out)
        self.assertIn("Jacob", out)
        self.assertIn("officially a thing", out)

    def test_mentions_the_reaction_when_he_laughed(self) -> None:
        birth = self._birth(
            user_text="fish-shaped cookie energy it is", laughed_ids={11},
        )
        self.assertIn("laughed", render_inside_joke_block(birth))


class BlessInsideJokeTests(unittest.TestCase):
    def _birth(self):
        return detect_inside_joke_birth(
            user_text="lol fish-shaped cookie energy",
            origins=[(11, _AIKO_LINE)],
        )

    def test_writes_a_catchphrase_and_a_shared_moment(self) -> None:
        from app.core.relationship.shared_moments import SharedMomentsStore

        f = _Fixture()
        try:
            moments = SharedMomentsStore(
                memory_store=f.memory, embedder=f.embedder,
            )
            out = bless_inside_joke(
                self._birth(),
                memory_store=f.memory,
                embedder=f.embedder,
                moments_store=moments,
                session_key="s1",
            )
            self.assertIsNotNone(out["catchphrase_id"])
            self.assertIsNotNone(out["moment_id"])
            kinds = {m.kind for m in f.memory.list_top(limit=10)}
            self.assertIn("catchphrase", kinds)
            self.assertIn("shared_moment", kinds)
        finally:
            f.close()

    def test_works_without_a_moments_store(self) -> None:
        f = _Fixture()
        try:
            out = bless_inside_joke(
                self._birth(),
                memory_store=f.memory,
                embedder=f.embedder,
            )
            self.assertIsNotNone(out["catchphrase_id"])
            self.assertIsNone(out["moment_id"])
        finally:
            f.close()

    def test_no_store_is_a_silent_no_op(self) -> None:
        out = bless_inside_joke(
            self._birth(), memory_store=None, embedder=None,
        )
        self.assertEqual(
            out, {"catchphrase_id": None, "moment_id": None},
        )


if __name__ == "__main__":
    unittest.main()

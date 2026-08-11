"""F12 -- semantic echo, and the retention gates it nearly broke.

Two properties matter more than the rest here.

**The floor discount is load-bearing, not cosmetic.** Scratchpad TTL used
to delete a memory only when ``revival_score == 0.0`` exactly. Semantic
echo gives a memory a score for merely being close to the reply in
embedding space, and surfaced memories were selected for topical
similarity to the turn in the first place -- so with full credit, nearly
every scratchpad row would acquire a score and become immortal. The tests
below pin down that a *quoted* memory is still rescued exactly as before
while a merely on-topic one is still cleaned up.

**A miss must still record its cosine.** The floor is a guess right now,
and the only way to replace a guess with a measurement is to keep the near
misses. A ledger that stored only the rows clearing the current floor
could never tell us the floor was wrong.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory import echo_detector
from app.core.memory.echo_detector import (
    ECHO_LEXICAL,
    ECHO_NONE,
    ECHO_SEMANTIC,
)
from app.core.memory.surfacing_outcome_store import (
    ITEM_KIND_MEMORY,
    SurfacedItem,
    SurfacingOutcomeStore,
)
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


def _vec(*parts: float) -> np.ndarray:
    arr = np.asarray(parts, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


# ── the detector itself ──────────────────────────────────────────────


class DetectorTests(unittest.TestCase):
    def test_a_quote_is_lexical_and_reports_its_overlap(self) -> None:
        verdict = echo_detector.detect(
            reply_tokens=echo_detector.tokens(
                "the sourdough starter in the fridge is fine",
            ),
            item_text="dmitri keeps a sourdough starter in the fridge",
            min_overlap=3,
        )
        self.assertEqual(verdict.kind, ECHO_LEXICAL)
        self.assertTrue(verdict.is_lexical)
        self.assertGreaterEqual(verdict.score, 3.0)

    def test_the_lexical_path_wins_even_when_a_floor_is_supplied(self) -> None:
        """Lexical is checked first because a quote is unambiguous, and
        the caller needs the strong verdict rather than whichever fires.
        """
        verdict = echo_detector.detect(
            reply_tokens=echo_detector.tokens(
                "sourdough starter fridge dmitri keeps",
            ),
            item_text="dmitri keeps a sourdough starter in the fridge",
            min_overlap=3,
            reply_vec=_vec(1, 0), item_vec=_vec(1, 0), min_cosine=0.5,
        )
        self.assertEqual(verdict.kind, ECHO_LEXICAL)

    def test_a_paraphrase_is_caught_semantically(self) -> None:
        """The whole point: no shared content words, real use."""
        verdict = echo_detector.detect(
            reply_tokens=echo_detector.tokens(
                "you mentioned wanting to get back into film photography",
            ),
            item_text="user shot 35mm in college and misses it",
            min_overlap=3,
            reply_vec=_vec(1.0, 0.1), item_vec=_vec(1.0, 0.0),
            min_cosine=0.6,
        )
        self.assertEqual(verdict.kind, ECHO_SEMANTIC)
        self.assertGreater(verdict.score, 0.6)

    def test_no_floor_means_no_semantic_pass_at_all(self) -> None:
        """The caller enables the fallback by supplying a floor, so there
        is no way to ask for semantic matching without saying how strict.
        """
        verdict = echo_detector.detect(
            reply_tokens={"unrelated", "words"},
            item_text="dmitri keeps a sourdough starter",
            min_overlap=3,
            reply_vec=_vec(1, 0), item_vec=_vec(1, 0), min_cosine=None,
        )
        self.assertEqual(verdict.kind, ECHO_NONE)
        self.assertEqual(verdict.score, 0.0)

    def test_a_sub_floor_cosine_is_reported_as_the_strength_of_the_miss(
        self,
    ) -> None:
        """A floor cannot be re-derived from verdicts that discard their
        near misses -- so a miss carries its cosine.
        """
        verdict = echo_detector.detect(
            reply_tokens={"unrelated", "words"},
            item_text="dmitri keeps a sourdough starter",
            min_overlap=3,
            reply_vec=_vec(1.0, 1.0), item_vec=_vec(1.0, 0.0),
            min_cosine=0.9,
        )
        self.assertEqual(verdict.kind, ECHO_NONE)
        self.assertFalse(verdict.echoed)
        self.assertAlmostEqual(verdict.score, 0.7071, places=3)

    def test_a_lexical_hit_still_reports_the_cosine(self) -> None:
        """The calibration channel. ``score`` carries only the signal that
        won, so a lexical verdict that dropped its cosine took the control
        group out of the one comparison the stored distribution exists to
        support: where the semantic floor belongs.
        """
        verdict = echo_detector.detect(
            reply_tokens=echo_detector.tokens(
                "sourdough starter fridge dmitri keeps",
            ),
            item_text="dmitri keeps a sourdough starter in the fridge",
            min_overlap=3,
            reply_vec=_vec(1.0, 1.0), item_vec=_vec(1.0, 0.0),
            min_cosine=0.5,
        )
        self.assertEqual(verdict.kind, ECHO_LEXICAL)
        self.assertGreaterEqual(verdict.score, 3.0)
        self.assertAlmostEqual(verdict.cosine or 0.0, 0.7071, places=3)

    def test_the_cosine_is_measured_even_with_no_floor_to_judge_it_by(
        self,
    ) -> None:
        """Withholding the floor withholds the *verdict*, not the reading."""
        verdict = echo_detector.detect(
            reply_tokens={"unrelated", "words"},
            item_text="dmitri keeps a sourdough starter",
            min_overlap=3,
            reply_vec=_vec(1.0, 1.0), item_vec=_vec(1.0, 0.0),
            min_cosine=None,
        )
        self.assertEqual(verdict.kind, ECHO_NONE)
        self.assertEqual(verdict.score, 0.0)
        self.assertAlmostEqual(verdict.cosine or 0.0, 0.7071, places=3)

    def test_nothing_to_compare_is_not_a_similarity_of_zero(self) -> None:
        verdict = echo_detector.detect(
            reply_tokens={"unrelated"},
            item_text="dmitri keeps a sourdough starter",
            min_overlap=3,
        )
        self.assertIsNone(verdict.cosine)

    def test_unusable_vectors_degrade_to_no_echo(self) -> None:
        for reply_vec, item_vec in (
            (None, _vec(1, 0)),
            (_vec(1, 0), None),
            (_vec(1, 0, 0), _vec(1, 0)),          # shape mismatch
            (np.asarray([], dtype=np.float32), _vec(1, 0)),
            ("not a vector", _vec(1, 0)),
        ):
            with self.subTest(reply_vec=type(reply_vec).__name__):
                verdict = echo_detector.detect(
                    reply_tokens={"unrelated"},
                    item_text="dmitri keeps a sourdough starter",
                    min_overlap=3,
                    reply_vec=reply_vec, item_vec=item_vec, min_cosine=0.5,
                )
                self.assertEqual(verdict.kind, ECHO_NONE)

    def test_empty_text_on_either_side_is_not_an_echo(self) -> None:
        self.assertFalse(echo_detector.detect(
            reply_tokens=set(), item_text="anything at all here",
            min_overlap=1,
        ).echoed)
        self.assertFalse(echo_detector.detect(
            reply_tokens={"anything"}, item_text="", min_overlap=1,
        ).echoed)

    def test_stopwords_and_short_words_cannot_carry_an_echo(self) -> None:
        """Otherwise any two sentences in English would echo each other."""
        verdict = echo_detector.detect(
            reply_tokens=echo_detector.tokens(
                "that is what you would have with them here",
            ),
            item_text="this was what they could have from us there",
            min_overlap=2,
        )
        self.assertEqual(verdict.kind, ECHO_NONE)


# ── revival: option A semantics ──────────────────────────────────────


@dataclass
class _Mem:
    id: int
    content: str
    embedding: np.ndarray


class _MemStore:
    def __init__(self, rows: dict[int, _Mem]) -> None:
        self.rows = rows
        self.bumps: list[tuple[list[int], float]] = []

    def get(self, key: int):
        return self.rows.get(int(key))

    def mark_revived(self, ids, *, delta: float) -> None:
        self.bumps.append((sorted(int(i) for i in ids), float(delta)))


class _Retriever:
    def __init__(self, ids: list[int]) -> None:
        self.last_surfaced_memory_ids = ids


@dataclass
class _MemSettings:
    tiers_enabled: bool = True
    revival_min_word_overlap: int = 3
    revival_per_hit: float = 0.15
    semantic_revival_enabled: bool = True
    semantic_revival_min_cosine: float = 0.62
    semantic_revival_per_hit: float = 0.05


class _Agent:
    surfacing_echo_min_overlap_concept = 1


class _Settings:
    agent = _Agent()


class _RevivalHost(PostTurnHelpersMixin):
    def __init__(self, mem_store, retriever, settings: _MemSettings) -> None:
        self._memory_store = mem_store
        self._rag_retriever = retriever
        self._memory_settings = settings
        self._settings = _Settings()
        self._last_assistant_vec = None


class RevivalCreditTests(unittest.TestCase):
    """A quote and a resemblance are not the same evidence."""

    def _host(self, **overrides) -> _RevivalHost:
        store = _MemStore({
            9: _Mem(9, "dmitri keeps a sourdough starter in the fridge",
                    _vec(1.0, 0.0)),
            8: _Mem(8, "user shot 35mm in college and misses it",
                    _vec(0.0, 1.0)),
        })
        host = _RevivalHost(
            store, _Retriever([9, 8]), _MemSettings(**overrides),
        )
        return host

    def test_a_quote_earns_the_full_historical_bump(self) -> None:
        host = self._host()
        host._last_assistant_vec = _vec(1.0, 0.0)
        host._mark_revived_memories(
            assistant_text="how is the sourdough starter in the fridge",
        )
        bumps = dict(
            (tuple(ids), delta) for ids, delta in host._memory_store.bumps
        )
        self.assertEqual(bumps[(9,)], 0.15)

    def test_a_paraphrase_earns_the_smaller_semantic_bump(self) -> None:
        host = self._host()
        # Points at memory 8, which shares no content words with the reply.
        host._last_assistant_vec = _vec(0.05, 1.0)
        host._mark_revived_memories(
            assistant_text="you were into taking pictures back then, right",
        )
        bumps = dict(
            (tuple(ids), delta) for ids, delta in host._memory_store.bumps
        )
        self.assertEqual(bumps[(8,)], 0.05)

    def test_the_semantic_bump_stays_under_the_ttl_rescue_bar(self) -> None:
        """The property option A rests on. One semantic hit must not be
        enough to exempt a scratchpad memory from cleanup, or TTL is off.
        """
        settings = _MemSettings()
        from app.core.infra.memory_settings import MemorySettings

        defaults = MemorySettings()
        self.assertLess(
            settings.semantic_revival_per_hit,
            defaults.scratchpad_ttl_min_revival,
        )
        # ...and a quote must still clear it, exactly as before F12.
        self.assertGreaterEqual(
            settings.revival_per_hit, defaults.scratchpad_ttl_min_revival,
        )

    def test_the_two_kinds_are_written_as_separate_batches(self) -> None:
        host = self._host()
        host._last_assistant_vec = _vec(0.05, 1.0)
        host._mark_revived_memories(
            assistant_text="the sourdough starter in your fridge, and photos",
        )
        deltas = sorted(delta for _ids, delta in host._memory_store.bumps)
        self.assertEqual(deltas, [0.05, 0.15])

    def test_disabling_the_semantic_half_restores_the_old_behaviour(
        self,
    ) -> None:
        host = self._host(semantic_revival_enabled=False)
        host._last_assistant_vec = _vec(0.0, 1.0)
        host._mark_revived_memories(
            assistant_text="you were into taking pictures back then, right",
        )
        self.assertEqual(host._memory_store.bumps, [])

    def test_no_reply_vector_falls_back_to_lexical_only(self) -> None:
        """The embed can be skipped (short reply, no embedder), and that
        must degrade rather than lose the lexical half too.
        """
        host = self._host()
        host._last_assistant_vec = None
        host._mark_revived_memories(
            assistant_text="how is the sourdough starter in the fridge",
        )
        bumps = dict(
            (tuple(ids), delta) for ids, delta in host._memory_store.bumps
        )
        self.assertEqual(bumps, {(9,): 0.15})

    def test_a_curt_reply_can_still_land_a_semantic_hit(self) -> None:
        """Before F12 a reply with fewer than ``min_overlap`` content words
        was an early return. Cosine does not care about word count.
        """
        host = self._host()
        host._last_assistant_vec = _vec(0.0, 1.0)
        host._mark_revived_memories(assistant_text="oh nice")
        bumps = dict(
            (tuple(ids), delta) for ids, delta in host._memory_store.bumps
        )
        self.assertEqual(bumps, {(8,): 0.05})

    def test_tiers_disabled_still_short_circuits(self) -> None:
        host = self._host(tiers_enabled=False)
        host._last_assistant_vec = _vec(1.0, 0.0)
        host._mark_revived_memories(
            assistant_text="how is the sourdough starter in the fridge",
        )
        self.assertEqual(host._memory_store.bumps, [])

    def test_a_zero_floor_disables_the_semantic_half(self) -> None:
        host = self._host(semantic_revival_min_cosine=0.0)
        self.assertIsNone(host._semantic_echo_floor())


# ── the TTL gate ─────────────────────────────────────────────────────


@dataclass
class _TierMem:
    id: int
    revival_score: float
    use_count: int = 0
    pinned: bool = False
    created_at: str = ""
    last_used_at: str | None = None
    tier: str = "scratchpad"
    metadata: dict = field(default_factory=dict)


class _TierStore:
    def __init__(self, rows: list[_TierMem]) -> None:
        self.rows = rows
        self.deleted: list[int] = []

    def iter_by_tier(self, tier: str):
        return [r for r in self.rows if r.tier == tier]

    def delete(self, mem_id: int) -> bool:
        self.deleted.append(int(mem_id))
        return True

    def update(self, *_a, **_kw):
        return None

    def prune(self) -> int:
        return 0


class ScratchpadTtlTests(unittest.TestCase):
    """The gate that a naive F12 would have silently switched off."""

    def _sweep(self, rows, *, ttl_min_revival: float) -> _TierStore:
        from app.core.infra.memory_settings import MemorySettings
        from app.core.memory.memory_promotion_worker import (
            MemoryPromotionWorker,
        )

        settings = MemorySettings()
        settings.scratchpad_ttl_min_revival = ttl_min_revival
        store = _TierStore(rows)
        MemoryPromotionWorker(store=store, settings=settings).run()
        return store

    def _old(self, days: int = 40) -> str:
        return (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()

    def test_an_untouched_memory_is_still_deleted(self) -> None:
        rows = [_TierMem(1, revival_score=0.0, created_at=self._old())]
        store = self._sweep(rows, ttl_min_revival=0.10)
        self.assertEqual(store.deleted, [1])

    def test_a_semantic_only_hit_does_not_rescue_it(self) -> None:
        """Option A. One 0.05 semantic bump sits under the 0.10 bar."""
        rows = [_TierMem(1, revival_score=0.05, created_at=self._old())]
        store = self._sweep(rows, ttl_min_revival=0.10)
        self.assertEqual(store.deleted, [1])

    def test_a_quoted_memory_is_rescued_exactly_as_before(self) -> None:
        rows = [_TierMem(1, revival_score=0.15, created_at=self._old())]
        store = self._sweep(rows, ttl_min_revival=0.10)
        self.assertEqual(store.deleted, [])

    def test_repeated_semantic_use_eventually_rescues(self) -> None:
        """Two on-topic hits are weak evidence twice, which is worth more
        than once -- the threshold, not a special case, decides this.
        """
        rows = [_TierMem(1, revival_score=0.10, created_at=self._old())]
        store = self._sweep(rows, ttl_min_revival=0.10)
        self.assertEqual(store.deleted, [])

    def test_a_configured_zero_still_deletes_dead_rows(self) -> None:
        """``< 0.0`` matches nothing, which would read as "TTL off" to
        anyone who set the knob to zero expecting the old behaviour.
        """
        rows = [_TierMem(1, revival_score=0.0, created_at=self._old())]
        store = self._sweep(rows, ttl_min_revival=0.0)
        self.assertEqual(store.deleted, [1])


# ── the ledger's calibration reads ───────────────────────────────────


class EchoBreakdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = SurfacingOutcomeStore(self.db)

    def tearDown(self) -> None:
        # Windows keeps the file handle until the thread-local connection
        # is closed explicitly, so a bare cleanup() raises PermissionError.
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

    def _row(self, msg_id: int, verdict, label: str) -> None:
        self.store.add_many(
            msg_id, [SurfacedItem(ITEM_KIND_MEMORY, msg_id)],
            echoes={(ITEM_KIND_MEMORY, msg_id): verdict},
        )
        self.store.settle(msg_id, label)

    def test_engagement_splits_by_how_the_echo_was_decided(self) -> None:
        """The query the deferred full-credit decision turns on."""
        self._row(1, echo_detector.EchoVerdict(ECHO_LEXICAL, 4.0), "engaged")
        self._row(2, echo_detector.EchoVerdict(ECHO_LEXICAL, 3.0), "engaged")
        self._row(3, echo_detector.EchoVerdict(ECHO_SEMANTIC, 0.7), "neutral")
        rows = {
            r["echo_kind"]: r
            for r in self.store.echo_breakdown(window_days=None)
        }
        self.assertEqual(rows[ECHO_LEXICAL]["settled"], 2)
        self.assertEqual(rows[ECHO_LEXICAL]["engaged_rate"], 1.0)
        self.assertEqual(rows[ECHO_SEMANTIC]["engaged_rate"], 0.0)
        self.assertAlmostEqual(rows[ECHO_SEMANTIC]["avg_score"], 0.7, places=4)

    def test_pre_v27_rows_are_reported_as_unrecorded(self) -> None:
        """They were judged by the lexical test alone. Folding them into
        ``none`` would claim a semantic comparison was made and lost.
        """
        self.store.add_many(5, [SurfacedItem(ITEM_KIND_MEMORY, 5)])
        self.db._get_conn().execute(
            "UPDATE surfacing_outcomes SET settled_at = ?, "
            "engagement_label = 'engaged'", ("2026-01-01T00:00:00+00:00",),
        )
        self.db._get_conn().commit()
        kinds = {
            r["echo_kind"] for r in self.store.echo_breakdown(window_days=None)
        }
        self.assertEqual(kinds, {"unrecorded"})

    def test_floor_replay_counts_what_each_floor_would_have_matched(
        self,
    ) -> None:
        """Every settled row kept its cosine, misses included, so each
        candidate floor can be replayed over the same history.
        """
        self._row(1, echo_detector.EchoVerdict(ECHO_NONE, 0.52), "neutral")
        self._row(2, echo_detector.EchoVerdict(ECHO_SEMANTIC, 0.68), "engaged")
        self._row(3, echo_detector.EchoVerdict(ECHO_SEMANTIC, 0.81), "engaged")
        by_floor = {
            r["floor"]: r
            for r in self.store.semantic_floor_candidates(
                window_days=None, floors=(0.50, 0.65, 0.75, 0.90),
            )
        }
        self.assertEqual(by_floor[0.50]["would_match"], 3)
        self.assertEqual(by_floor[0.65]["would_match"], 2)
        self.assertEqual(by_floor[0.65]["engaged_rate"], 1.0)
        self.assertEqual(by_floor[0.75]["would_match"], 1)
        self.assertEqual(by_floor[0.90]["would_match"], 0)
        self.assertIsNone(by_floor[0.90]["engaged_rate"])

    def test_floor_replay_ignores_rows_the_lexical_test_already_claimed(
        self,
    ) -> None:
        """A floor only decides the rows lexical did not catch; counting
        quotes would flatter every floor equally.
        """
        self._row(1, echo_detector.EchoVerdict(ECHO_LEXICAL, 5.0), "engaged")
        rows = self.store.semantic_floor_candidates(
            window_days=None, floors=(0.5,),
        )
        self.assertEqual(rows[0]["would_match"], 0)

    def test_the_reads_survive_a_missing_table(self) -> None:
        self.db._get_conn().execute("DROP TABLE surfacing_outcomes")
        self.assertEqual(self.store.echo_breakdown(window_days=7), [])
        self.assertEqual(self.store.semantic_floor_candidates(), [])


if __name__ == "__main__":
    unittest.main()

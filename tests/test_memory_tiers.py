"""Schema v8 tier + revival + wall-clock-decay tests.

Covers the additions in E1/E2:
- ``Memory.tier`` / ``revival_score`` round-trip through ``add`` / ``update``.
- Pinning coerces tier to ``long_term``.
- ``decay()`` is wall-clock-driven: passing ``elapsed_days`` skips the
  ``kv_meta`` anchor read; the actual delta scales with elapsed time.
- ``decay()`` applies a revival rebate proportional to ``revival_score``.
- ``mark_revived`` clamps to [0, 1].
- ``prune()`` honours per-tier caps independently.
- ``MemoryPromotionWorker`` promotes, deletes, demotes, and coerces.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_promotion_worker import MemoryPromotionWorker
from app.core.memory.memory_store import MemoryStore


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


def _store_factory(tier_caps: dict[str, int] | None = None) -> "tuple[Path, MemoryStore]":
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    store = MemoryStore(
        path,
        scratchpad_cap=(tier_caps or {}).get("scratchpad", 100),
        archive_cap=(tier_caps or {}).get("archive", 100),
        max_memories=(tier_caps or {}).get("long_term", 100),
    )
    return path, store


def _emb(text: str) -> np.ndarray:
    return _FakeEmbedder().embed(text)


class TestTierRoundTrip(unittest.TestCase):
    def test_default_tier_is_long_term(self) -> None:
        _, store = _store_factory()
        mem = store.add("hello world", "fact", _emb("hello"))
        assert mem is not None
        self.assertEqual(mem.tier, "long_term")
        self.assertEqual(mem.revival_score, 0.0)

    def test_scratchpad_tier_round_trip(self) -> None:
        _, store = _store_factory()
        mem = store.add("ephemeral thought", "fact", _emb("eph"), tier="scratchpad")
        assert mem is not None
        self.assertEqual(mem.tier, "scratchpad")

    def test_invalid_tier_coerced_to_long_term(self) -> None:
        _, store = _store_factory()
        mem = store.add("foobar baz", "fact", _emb("foo"), tier="garbage")
        assert mem is not None
        self.assertEqual(mem.tier, "long_term")

    def test_update_tier_clamps_revival(self) -> None:
        _, store = _store_factory()
        mem = store.add("hello there", "fact", _emb("x"), tier="scratchpad")
        assert mem is not None
        updated = store.update(mem.id, tier="archive", revival_score=5.0)
        assert updated is not None
        self.assertEqual(updated.tier, "archive")
        self.assertAlmostEqual(updated.revival_score, 1.0)

    def test_pinning_coerces_tier_to_long_term(self) -> None:
        _, store = _store_factory()
        mem = store.add("anchor me here", "fact", _emb("a"), tier="scratchpad")
        assert mem is not None
        pinned = store.set_pinned(mem.id, True)
        assert pinned is not None
        self.assertTrue(pinned.pinned)
        self.assertEqual(pinned.tier, "long_term")

    def test_mark_revived_clamps(self) -> None:
        _, store = _store_factory()
        a = store.add("alpha alpha", "fact", _emb("a"))
        b = store.add("beta beta beta", "fact", _emb("b"))
        assert a is not None and b is not None
        store.mark_revived([a.id, b.id], delta=0.6)
        store.mark_revived([a.id], delta=0.6)
        a2 = store.get(a.id)
        b2 = store.get(b.id)
        assert a2 is not None and b2 is not None
        self.assertAlmostEqual(a2.revival_score, 1.0)
        self.assertAlmostEqual(b2.revival_score, 0.6, places=4)


class TestTierFilteredListing(unittest.TestCase):
    """Tier filtering must run before the offset/limit slice.

    Regression: the Memory-tab tier filter used to be applied to the
    already-paginated page in the facade, so an ``archive`` filter only
    surfaced the archive rows that happened to fall in the newest page
    (≈none, since archive rows sort to the bottom) while ``count_memories``
    reported the true total — one item shown, broken pagination.
    """

    def _seed(self):
        _, store = _store_factory()
        # 30 long_term + 5 archive. Archive rows are added last so a
        # recency-ordered, post-slice filter would push them off page 0.
        for i in range(30):
            store.add(f"long term row {i:02d}", "fact", _emb(f"lt{i}"),
                      tier="long_term")
        for i in range(5):
            store.add(f"archive row {i:02d}", "fact", _emb(f"ar{i}"),
                      tier="archive")
        return store

    def test_recent_listing_filters_tier_before_slice(self) -> None:
        store = self._seed()
        # Page 0 of an archive filter must return all 5 archive rows, even
        # though they were the most-recently-added (would still be page 0)
        # AND even if we ask for a tiny window that the long_term rows
        # would otherwise fill.
        page = store.list_recent(limit=50, offset=0, tier="archive")
        self.assertEqual(len(page), 5)
        self.assertTrue(all(m.tier == "archive" for m in page))
        self.assertEqual(store.count_memories(tier="archive"), 5)

    def test_top_listing_filters_tier_before_slice(self) -> None:
        store = self._seed()
        page = store.list_top(limit=50, offset=0, tier="archive")
        self.assertEqual(len(page), 5)
        self.assertTrue(all(m.tier == "archive" for m in page))

    def test_tier_pagination_is_consistent(self) -> None:
        store = self._seed()
        # With a window of 2, the 5 archive rows must paginate as 2/2/1 and
        # never leak a non-archive row.
        seen: list[int] = []
        for offset in (0, 2, 4):
            page = store.list_recent(limit=2, offset=offset, tier="archive")
            self.assertTrue(all(m.tier == "archive" for m in page))
            seen.extend(m.id for m in page)
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)


class TestKindFilteredListing(unittest.TestCase):
    """P33: ``kind`` must filter before the sort/slice, not after.

    The regression this locks down was a *correctness* bug, not just a
    perf one: three call sites read the top-N rows of any kind and then
    filtered in Python, so a low-salience kind (catchphrases) stopped
    surfacing entirely once N higher-salience rows of other kinds
    existed. Filtering inside the store also drops a full-mirror sort off
    the hot path, which is the perf half.
    """

    def _seed(self):
        _, store = _store_factory()
        # 40 high-salience facts, then 3 low-salience catchphrases: the
        # exact shape that starved the old post-filter callers.
        for i in range(40):
            store.add(
                f"high salience fact {i:02d}", "fact", _emb(f"f{i}"),
                salience=0.9,
            )
        for i in range(3):
            store.add(
                f"running joke number {i}", "catchphrase", _emb(f"c{i}"),
                salience=0.2,
            )
        return store

    def test_top_returns_the_low_salience_kind(self) -> None:
        store = self._seed()
        rows = store.list_top(limit=3, kind="catchphrase")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(m.kind == "catchphrase" for m in rows))

    def test_unfiltered_top_would_have_missed_them(self) -> None:
        # Documents *why* the filter is required rather than merely nice:
        # this is what the old call sites saw.
        store = self._seed()
        rows = store.list_top(limit=24)
        self.assertEqual(
            [m for m in rows if m.kind == "catchphrase"], [],
            "if this starts passing, the starvation premise changed",
        )

    def test_kind_is_normalised(self) -> None:
        store = self._seed()
        self.assertEqual(len(store.list_top(limit=5, kind="  CatchPhrase ")), 3)

    def test_recent_filters_kind_too(self) -> None:
        store = self._seed()
        rows = store.list_recent(limit=5, kind="catchphrase")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(m.kind == "catchphrase" for m in rows))

    def test_kind_and_tier_compose(self) -> None:
        _, store = _store_factory()
        store.add("joke in archive", "catchphrase", _emb("a"), tier="archive")
        store.add("joke in long term", "catchphrase", _emb("b"))
        store.add("fact in archive", "fact", _emb("c"), tier="archive")
        rows = store.list_top(limit=10, kind="catchphrase", tier="archive")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "joke in archive")

    def test_unknown_kind_returns_empty(self) -> None:
        store = self._seed()
        self.assertEqual(store.list_top(limit=10, kind="nope"), [])

    def test_no_kind_still_returns_everything(self) -> None:
        store = self._seed()
        self.assertEqual(len(store.list_top(limit=100)), 43)


class TestTextSearchedListing(unittest.TestCase):
    """The Memory tab's "did she remember this?" box.

    Filtering has to happen inside the store for the same reason ``kind``
    and ``tier`` do: a post-slice filter only searches whichever page
    happened to be fetched, which for a 50-row window over thousands of
    rows means the answer is usually "no" regardless of the truth.
    """

    def _seed(self):
        _, store = _store_factory()
        store.add("he collects bottle caps", "fact", _emb("a"))
        store.add("the cap came off the bottle", "event", _emb("b"))
        store.add("she prefers tea in the morning", "preference", _emb("c"))
        return store

    def test_search_finds_rows_in_any_word_order(self) -> None:
        store = self._seed()
        rows = store.list_recent(limit=50, q="bottle cap")
        self.assertEqual(len(rows), 2)

    def test_the_count_agrees_with_the_listing(self) -> None:
        # The pager divides ``count_memories``; if the two disagree the UI
        # offers pages that render empty.
        store = self._seed()
        self.assertEqual(store.count_memories(q="bottle cap"), 2)
        self.assertEqual(store.count_memories(q="xylophone"), 0)

    def test_search_composes_with_kind_and_tier(self) -> None:
        store = self._seed()
        rows = store.list_recent(limit=50, q="bottle", kind="fact")
        self.assertEqual(len(rows), 1)
        self.assertEqual(store.count_memories("fact", q="bottle"), 1)

    def test_search_applies_to_top_order_too(self) -> None:
        store = self._seed()
        rows = store.list_top(limit=50, q="bottle")
        self.assertEqual(len(rows), 2)

    def test_search_narrows_before_the_slice(self) -> None:
        # A page of 1 must walk the matches, not the mirror: post-slice
        # filtering would return the single newest row and then discard it.
        _, store = _store_factory()
        for i in range(30):
            store.add(f"unrelated row {i:02d}", "fact", _emb(f"u{i}"))
        store.add("he collects bottle caps", "fact", _emb("target"))
        for i in range(30, 60):
            store.add(f"unrelated row {i:02d}", "fact", _emb(f"u{i}"))
        rows = store.list_recent(limit=1, q="bottle")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "he collects bottle caps")

    def test_a_blank_search_is_not_a_filter(self) -> None:
        store = self._seed()
        for q in ("", "   ", None):
            self.assertEqual(len(store.list_recent(limit=50, q=q)), 3, repr(q))
            self.assertEqual(store.count_memories(q=q), 3, repr(q))

    def test_a_wildcard_reaches_inside_a_word(self) -> None:
        store = self._seed()
        rows = store.list_recent(limit=50, q="collect*")
        self.assertEqual(len(rows), 1)


class TestWallClockDecay(unittest.TestCase):
    def test_decay_scales_with_elapsed_days(self) -> None:
        _, store = _store_factory()
        sp = store.add("scratch row", "fact", _emb("sp"), tier="scratchpad", salience=1.0)
        lt = store.add("long row", "fact", _emb("lt"), tier="long_term", salience=1.0)
        ar = store.add("archive row", "fact", _emb("ar"), tier="archive", salience=1.0)
        assert sp and lt and ar
        store.decay(
            elapsed_days=1.0,
            decay_rates={"scratchpad": 0.05, "long_term": 0.02, "archive": 0.0},
            revival_coefficient=0.0,
            revival_decay_per_day=0.0,
        )
        self.assertAlmostEqual(store.get(sp.id).salience, 0.95, places=4)
        self.assertAlmostEqual(store.get(lt.id).salience, 0.98, places=4)
        self.assertAlmostEqual(store.get(ar.id).salience, 1.0, places=4)

    def test_decay_skips_pinned(self) -> None:
        _, store = _store_factory()
        m = store.add("pinned anchor", "fact", _emb("a"), salience=1.0)
        assert m is not None
        store.set_pinned(m.id, True)
        store.decay(
            elapsed_days=10.0,
            decay_rates={"long_term": 0.5},
            revival_coefficient=0.0,
            revival_decay_per_day=0.0,
        )
        self.assertEqual(store.get(m.id).salience, 1.0)

    def test_revival_rebate_offsets_decay(self) -> None:
        _, store = _store_factory()
        m = store.add("revive me", "fact", _emb("r"), salience=0.5)
        assert m is not None
        store.update(m.id, revival_score=1.0)
        # rebate = 0.1 * 1.0 * 1.0 = 0.1; decay = 0.02 * 1.0 = 0.02; net +0.08.
        store.decay(
            elapsed_days=1.0,
            decay_rates={"long_term": 0.02},
            revival_coefficient=0.1,
            revival_decay_per_day=0.0,
        )
        self.assertAlmostEqual(store.get(m.id).salience, 0.58, places=4)

    def test_revival_score_decays(self) -> None:
        _, store = _store_factory()
        m = store.add("revive me too", "fact", _emb("r"))
        assert m is not None
        store.update(m.id, revival_score=0.8)
        store.decay(
            elapsed_days=2.0,
            decay_rates={"long_term": 0.0},
            revival_coefficient=0.0,
            revival_decay_per_day=0.1,
        )
        # revival_score = 0.8 - 0.1 * 2.0 = 0.6
        self.assertAlmostEqual(store.get(m.id).revival_score, 0.6, places=4)


class TestPerTierPrune(unittest.TestCase):
    def test_prune_uses_per_tier_caps(self) -> None:
        # MemoryStore clamps caps to a minimum of 50; force the cap to
        # 50 via the public ctor and add 52 rows with varied salience so
        # the cheapest two get pruned.
        _, store = _store_factory({"scratchpad": 50, "long_term": 100, "archive": 100})
        for i in range(52):
            mem = store.add(
                f"scratch entry number {i:03d}", "fact", _emb(f"s{i}"),
                tier="scratchpad", salience=0.01 * (i + 1),
            )
            self.assertIsNotNone(mem)
        # The opportunistic in-add prune should already have trimmed
        # back to 50; an explicit prune() is a no-op.
        store.prune()
        remaining = list(store.iter_by_tier("scratchpad"))
        self.assertEqual(len(remaining), 50)
        # Lowest two salience rows (i=0, i=1) should have been evicted.
        survivors = sorted(m.salience for m in remaining)
        self.assertGreaterEqual(min(survivors), 0.029)


class TestPromotionWorker(unittest.TestCase):
    def _settings(
        self,
        *,
        promote_age: int = 7,
        promote_use: int = 3,
        promote_revival: float = 0.3,
        ttl: int = 14,
        idle: int = 180,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            tiers_enabled=True,
            promotion_worker_interval_seconds=3600,
            scratchpad_promote_min_age_days=promote_age,
            scratchpad_promote_min_use_count=promote_use,
            scratchpad_promote_min_revival=promote_revival,
            scratchpad_ttl_days=ttl,
            archive_demote_idle_days=idle,
        )

    def test_promotes_on_revival_threshold(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "rumor", "fact", _emb("r"), tier="scratchpad", salience=0.6,
        )
        assert mem is not None
        store.update(mem.id, revival_score=0.5)
        worker = MemoryPromotionWorker(store, self._settings())
        result = worker.run()
        self.assertEqual(result["promoted"], 1)
        self.assertEqual(store.get(mem.id).tier, "long_term")

    def test_promotes_on_age_plus_use(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "rumor", "fact", _emb("r"), tier="scratchpad", salience=0.6,
        )
        assert mem is not None
        # Backdate the row and bump use_count.
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        store.update(mem.id)  # no-op to ensure mirror is fresh
        conn = store._get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE memories SET created_at = ?, use_count = ? WHERE id = ?",
            (old_iso, 4, int(mem.id)),
        )
        conn.commit()
        store._reload_mirror()  # noqa: SLF001
        worker = MemoryPromotionWorker(store, self._settings())
        result = worker.run()
        self.assertEqual(result["promoted"], 1)
        self.assertEqual(store.get(mem.id).tier, "long_term")

    def test_deletes_dead_scratchpad(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "stale", "fact", _emb("s"), tier="scratchpad", salience=0.4,
        )
        assert mem is not None
        old_iso = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        conn = store._get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (old_iso, int(mem.id)),
        )
        conn.commit()
        store._reload_mirror()  # noqa: SLF001
        worker = MemoryPromotionWorker(store, self._settings())
        result = worker.run()
        self.assertEqual(result["deleted_scratchpad"], 1)
        self.assertIsNone(store.get(mem.id))

    def test_demotes_idle_long_term(self) -> None:
        _, store = _store_factory()
        mem = store.add("cold", "fact", _emb("c"), tier="long_term", salience=0.4)
        assert mem is not None
        old_iso = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        conn = store._get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE memories SET last_used_at = ? WHERE id = ?",
            (old_iso, int(mem.id)),
        )
        conn.commit()
        store._reload_mirror()  # noqa: SLF001
        worker = MemoryPromotionWorker(store, self._settings())
        result = worker.run()
        self.assertEqual(result["demoted_archive"], 1)
        self.assertEqual(store.get(mem.id).tier, "archive")


class TestPromotionWorkerDemand(unittest.TestCase):
    """The probe runs the same four predicates without moving anything."""

    def _settings(self, **over) -> SimpleNamespace:
        base = dict(
            tiers_enabled=True,
            promotion_worker_interval_seconds=3600,
            scratchpad_promote_min_age_days=7,
            scratchpad_promote_min_use_count=3,
            scratchpad_promote_min_revival=0.3,
            scratchpad_ttl_days=14,
            archive_demote_idle_days=180,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def _probe(self, worker: MemoryPromotionWorker):
        return worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )

    def test_is_ready_is_the_tiers_flag_alone(self) -> None:
        _, store = _store_factory()
        now = datetime.now(timezone.utc)
        worker = MemoryPromotionWorker(store, self._settings())
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        self.assertTrue(
            worker.is_ready(now=now, last_run_at=now - timedelta(seconds=30))
        )
        off = MemoryPromotionWorker(store, self._settings(tiers_enabled=False))
        self.assertFalse(off.is_ready(now=now, last_run_at=None))

    def test_a_settled_store_asks_for_nothing(self) -> None:
        _, store = _store_factory()
        store.add("fresh", "fact", _emb("f"), tier="scratchpad", salience=0.4)
        worker = MemoryPromotionWorker(store, self._settings())
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "0 tier moves")
        self.assertFalse(signal.needs_llm)

    def test_a_promotable_row_shows_up_as_one_move(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "rumor", "fact", _emb("r"), tier="scratchpad", salience=0.6,
        )
        assert mem is not None
        store.update(mem.id, revival_score=0.5)
        worker = MemoryPromotionWorker(store, self._settings())
        signal = self._probe(worker)
        self.assertGreaterEqual(signal.pressure, 0.5)
        self.assertEqual(signal.reason, "1 tier moves")

    def test_a_demotable_row_shows_up_too(self) -> None:
        _, store = _store_factory()
        mem = store.add("cold", "fact", _emb("c"), tier="long_term", salience=0.4)
        assert mem is not None
        old_iso = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        conn = store._get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE memories SET last_used_at = ? WHERE id = ?",
            (old_iso, int(mem.id)),
        )
        conn.commit()
        store._reload_mirror()  # noqa: SLF001
        worker = MemoryPromotionWorker(store, self._settings())
        self.assertGreaterEqual(self._probe(worker).pressure, 0.5)

    def test_disabled_tiers_report_no_pressure(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "rumor", "fact", _emb("r"), tier="scratchpad", salience=0.6,
        )
        assert mem is not None
        store.update(mem.id, revival_score=0.5)
        worker = MemoryPromotionWorker(
            store, self._settings(tiers_enabled=False),
        )
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "tiers disabled")

    def test_the_probe_moves_nothing(self) -> None:
        _, store = _store_factory()
        promotable = store.add(
            "rumor", "fact", _emb("r"), tier="scratchpad", salience=0.6,
        )
        assert promotable is not None
        store.update(promotable.id, revival_score=0.5)
        worker = MemoryPromotionWorker(store, self._settings())
        self._probe(worker)
        self._probe(worker)
        self.assertEqual(store.get(promotable.id).tier, "scratchpad")

    def test_the_count_stops_at_saturation(self) -> None:
        # The probe must not scale with the store: it stops counting
        # once it has seen enough to rank the worker top of its lane.
        from app.core.memory.memory_promotion_worker import _DEMAND_SATURATION

        _, store = _store_factory({"scratchpad": 500})
        for i in range(_DEMAND_SATURATION + 10):
            mem = store.add(
                f"rumor {i}", "fact", _emb(f"r{i}"),
                tier="scratchpad", salience=0.6,
            )
            assert mem is not None
            store.update(mem.id, revival_score=0.5)
        worker = MemoryPromotionWorker(store, self._settings())
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(
            signal.reason, f"{_DEMAND_SATURATION} tier moves",
        )


if __name__ == "__main__":
    unittest.main()

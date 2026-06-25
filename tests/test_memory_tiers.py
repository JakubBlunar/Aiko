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


if __name__ == "__main__":
    unittest.main()

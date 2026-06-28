"""Tests for H20 — a room that evolves (depleting + accruing micro-state).

Two layers: the pure :mod:`app.core.world.room_evolution` transition math
(tea cycle, cookie refill, book progress → finish), and the
:class:`app.core.world.room_evolution_worker.RoomEvolutionWorker` state
machine (candidate selection, wall-clock gate, world broadcast, and the
book-finish seed landing in the shared H17 ring).
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.world import room_evolution as evo
from app.core.world.idle_activity_worker import load_idle_seeds
from app.core.world.room_evolution_worker import (
    KV_LAST_EVOLVED_AT,
    RoomEvolutionWorker,
)


# ── pure math ─────────────────────────────────────────────────────────


class TeaTests(unittest.TestCase):
    def test_full_to_half_to_empty(self) -> None:
        rng = random.Random(0)
        s1, d1, e1 = evo.next_tea({"fullness": "full", "flavor": "jasmine"}, rng)
        self.assertEqual(s1["fullness"], "half")
        self.assertIsNone(e1)
        s2, d2, e2 = evo.next_tea(s1, rng)
        self.assertEqual(s2["fullness"], "empty")
        self.assertIsNone(e2)

    def test_empty_brews_fresh_new_flavor(self) -> None:
        rng = random.Random(3)
        s, desc, event = evo.next_tea({"fullness": "empty", "flavor": "jasmine"}, rng)
        self.assertEqual(s["fullness"], "full")
        self.assertNotEqual(s["flavor"], "jasmine")
        self.assertIsNotNone(event)
        self.assertIn(s["flavor"], desc)

    def test_missing_state_defaults_to_full(self) -> None:
        rng = random.Random(0)
        s, _d, _e = evo.next_tea(None, rng)
        self.assertEqual(s["fullness"], "half")  # full → half


class CookieTests(unittest.TestCase):
    def test_fresh_batch_avoids_prev_flavor(self) -> None:
        rng = random.Random(1)
        for _ in range(10):
            desc, state = evo.fresh_cookie_batch("chocolate chip", rng)
            self.assertNotEqual(state["flavor"], "chocolate chip")
            self.assertEqual(state["freshness"], "fresh")
            self.assertIn(state["flavor"], desc)


class BookTests(unittest.TestCase):
    def test_progress_advances(self) -> None:
        rng = random.Random(0)
        s, name, desc, finished = evo.advance_book(
            {"title": "T", "progress": 0, "total": 5}, rng
        )
        self.assertEqual(s["progress"], 1)
        self.assertEqual(name, "T")
        self.assertIsNone(finished)

    def test_finish_starts_new_book(self) -> None:
        rng = random.Random(2)
        s, name, desc, finished = evo.advance_book(
            {"title": "Old Title", "progress": 4, "total": 5}, rng
        )
        self.assertEqual(finished, "Old Title")
        self.assertNotEqual(name, "Old Title")
        self.assertEqual(s["progress"], 0)
        self.assertEqual(s["status"], "reading")


# ── worker ────────────────────────────────────────────────────────────


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeItem:
    def __init__(
        self, id_: int, slug: str, name: str, *,
        quantity: int = 1, state: dict | None = None,
        consumable: bool = False,
    ) -> None:
        self.id = id_
        self.slug = slug
        self.name = name
        self.description = ""
        self.quantity = quantity
        self.state = state or {}
        self.consumable = consumable
        self.location_id = 1

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "slug": self.slug, "name": self.name,
                "quantity": self.quantity, "state": self.state}


class _FakeLoc:
    def __init__(self, id_: int, slug: str) -> None:
        self.id = id_
        self.slug = slug


class _FakeWorld:
    def __init__(self, items: list[_FakeItem]) -> None:
        self._items = {i.id: i for i in items}
        self.locations = [_FakeLoc(9, "kitchenette")]
        self.added: list[dict[str, Any]] = []

    def list_items(self) -> list[_FakeItem]:
        return list(self._items.values())

    def list_locations(self) -> list[_FakeLoc]:
        return list(self.locations)

    def update_item(self, item_id: int, **kwargs: Any) -> _FakeItem | None:
        item = self._items.get(item_id)
        if item is None:
            return None
        if "name" in kwargs and kwargs["name"] is not None:
            item.name = kwargs["name"]
        if "description" in kwargs and kwargs["description"] is not None:
            item.description = kwargs["description"]
        if "quantity" in kwargs and kwargs["quantity"] is not None:
            item.quantity = kwargs["quantity"]
        if "state" in kwargs and kwargs["state"] is not None:
            item.state = kwargs["state"]
        return item

    def add_item(self, **kwargs: Any):
        self.added.append(kwargs)
        new = _FakeItem(
            99, kwargs["slug"], kwargs["name"],
            quantity=kwargs.get("quantity", 1),
            state=kwargs.get("state"),
            consumable=kwargs.get("consumable", False),
        )
        self._items[new.id] = new
        return new, True


def _mem(**overrides: Any) -> SimpleNamespace:
    base = dict(
        room_evolution_interval_seconds=21600,
        room_evolution_min_hours=8.0,
        idle_seed_max_ring=6,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _worker(
    *, world: _FakeWorld, kv: _FakeKV, enabled: bool = True,
    notify: Any = None, seed: int = 0,
) -> RoomEvolutionWorker:
    return RoomEvolutionWorker(
        world_store=world,
        chat_db=kv,
        agent_settings=SimpleNamespace(room_evolution_enabled=enabled),
        memory_settings=_mem(),
        user_display_name_provider=lambda: "Jacob",
        notify=notify,
        ollama=None,  # exercises the deterministic book-finish fallback
        model=None,
        rng=random.Random(seed),
    )


class WorkerTests(unittest.TestCase):
    def test_disabled_skips(self) -> None:
        world = _FakeWorld([_FakeItem(1, "tea_pot", "tea pot")])
        worker = _worker(world=world, kv=_FakeKV(), enabled=False)
        self.assertTrue(worker.run().get("skipped"))

    def test_min_gap_blocks(self) -> None:
        kv = _FakeKV()
        kv.store[KV_LAST_EVOLVED_AT] = datetime.now(timezone.utc).isoformat()
        world = _FakeWorld([_FakeItem(1, "tea_pot", "tea pot")])
        worker = _worker(world=world, kv=kv)
        self.assertEqual(worker.run().get("reason"), "min_gap")

    def test_force_bypasses_gap_and_evolves(self) -> None:
        kv = _FakeKV()
        kv.store[KV_LAST_EVOLVED_AT] = datetime.now(timezone.utc).isoformat()
        patches: list[dict[str, Any]] = []
        world = _FakeWorld([_FakeItem(1, "tea_pot", "tea pot")])
        worker = _worker(world=world, kv=kv, notify=patches.append)
        worker._force = True
        r = worker.run()
        self.assertTrue(r.get("evolved"))
        self.assertTrue(any("item" in p for p in patches))
        # Gate stamp was advanced.
        self.assertIn(KV_LAST_EVOLVED_AT, kv.store)

    def test_book_finish_emits_seed(self) -> None:
        kv = _FakeKV()
        book = _FakeItem(
            1, "scifi_paperback", "Old Book",
            state={"title": "Old Book", "progress": 4, "total": 5},
        )
        # A full cookie jar keeps cookies out of the candidate set so the
        # book is the only applicable transition this run.
        jar = _FakeItem(
            2, "cookie_jar", "cookies", quantity=3, consumable=True,
            state={"flavor": "chocolate chip"},
        )
        world = _FakeWorld([book, jar])
        worker = _worker(world=world, kv=kv)
        worker._force = True
        r = worker.run()
        self.assertEqual(r.get("kind"), "book")
        self.assertEqual(r.get("finished"), "Old Book")
        ring = load_idle_seeds(kv.kv_get)
        self.assertTrue(ring)
        self.assertEqual(ring[-1]["key"], "room_evolution")
        self.assertIn("Old Book", ring[-1]["seed"])

    def test_cookie_refill_when_low(self) -> None:
        kv = _FakeKV()
        jar = _FakeItem(
            1, "cookie_jar", "cookies", quantity=0, consumable=True,
            state={"flavor": "chocolate chip"},
        )
        world = _FakeWorld([jar])
        worker = _worker(world=world, kv=kv)
        worker._force = True
        r = worker.run()
        self.assertEqual(r.get("kind"), "cookies")
        self.assertEqual(jar.quantity, 3)

    def test_cookie_recreated_when_missing(self) -> None:
        kv = _FakeKV()
        # No cookie jar present at all → worker re-creates it.
        world = _FakeWorld([])
        worker = _worker(world=world, kv=kv)
        worker._force = True
        r = worker.run()
        self.assertEqual(r.get("kind"), "cookies")
        self.assertTrue(world.added)
        self.assertEqual(world.added[0]["slug"], "cookie_jar")
        self.assertEqual(world.added[0]["given_by"], "aiko")


if __name__ == "__main__":
    unittest.main()

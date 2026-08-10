"""Tests for :class:`app.core.world.idle_activity_worker.IdleAwayActivityWorker`.

Exercises activity selection (tied to a fake world inventory), the world
mutation it triggers (``set_state`` / ``consume_item`` / ``update_item``),
the kv journal ring, and the pacing gates (cooldown, daily cap, enabled
switch, garden-visit guard). All fakes — no real WorldStore, LLM, or DB.
The worker composes its line via the deterministic fallback
(``ollama=None``) so assertions don't depend on a model.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.world import day_intention
from app.core.world.idle_activity_worker import (
    AWAY_ACTIVITIES_JOURNAL_KEY,
    EFFECT_POUR_TEA,
    EFFECT_WATER_PLANT,
    IdleAwayActivityWorker,
    load_idle_seeds,
    load_journal,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeItem:
    def __init__(
        self,
        id_: int,
        name: str,
        *,
        kind: str = "object",
        consumable: bool = False,
        quantity: int = 1,
        location_id: int | None = None,
        slug: str = "",
        state: dict[str, Any] | None = None,
    ) -> None:
        self.id = id_
        self.name = name
        self.kind = kind
        self.consumable = consumable
        self.quantity = quantity
        self.location_id = location_id
        self.slug = slug or name.lower().replace(" ", "_")
        self.state: dict[str, Any] = state if state is not None else {}
        self.description = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "quantity": self.quantity}


class _FakeLoc:
    def __init__(self, id_: int, name: str, slug: str = "") -> None:
        self.id = id_
        self.name = name
        self.slug = slug or name.lower().replace(" ", "_")


class _FakeRoomState:
    def __init__(
        self, posture: str, activity: str, location_id: int | None = None,
    ) -> None:
        self.posture = posture
        self.activity = activity
        self.location_id = location_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "posture": self.posture,
            "activity": self.activity,
            "location_id": self.location_id,
        }


class _FakeWorldStore:
    def __init__(
        self,
        *,
        items: list[_FakeItem] | None = None,
        locations: list[_FakeLoc] | None = None,
    ) -> None:
        self._items = items if items is not None else []
        self._locations = (
            locations
            if locations is not None
            else [_FakeLoc(1, "the desk", "desk")]
        )
        self.set_state_calls: list[dict[str, Any]] = []
        self.consumed: list[int] = []
        self.moved: list[tuple[int, int]] = []
        self.watered: list[int] = []

    def list_items(self) -> list[_FakeItem]:
        return list(self._items)

    def list_locations(self) -> list[_FakeLoc]:
        return list(self._locations)

    def set_state(
        self,
        *,
        posture: str,
        activity: str,
        location_id: int | None = None,
    ) -> _FakeRoomState:
        self.set_state_calls.append(
            {
                "posture": posture,
                "activity": activity,
                "location_id": location_id,
            }
        )
        return _FakeRoomState(posture, activity, location_id)

    def consume_item(self, item_id: int, *, amount: int = 1):
        self.consumed.append(item_id)
        item = next((i for i in self._items if i.id == item_id), None)
        if item is None:
            return None, 0
        item.quantity -= amount
        if item.quantity <= 0:
            self._items = [i for i in self._items if i.id != item_id]
            return None, amount
        return item, amount

    def get_item(self, item_id: int) -> _FakeItem | None:
        return next((i for i in self._items if i.id == item_id), None)

    def update_item(
        self,
        item_id: int,
        *,
        location_id: int | None = None,
        name: str | None = None,
        description: str | None = None,
        state: dict[str, Any] | None = None,
        quantity: int | None = None,
    ):
        item = self.get_item(item_id)
        if location_id is not None:
            self.moved.append((item_id, location_id))
            if item is not None:
                item.location_id = location_id
        if item is None:
            return None
        if name is not None:
            item.name = name
        if description is not None:
            item.description = description
        if state is not None:
            item.state = dict(state)
        if quantity is not None:
            item.quantity = quantity
        return item

    def water_plant(self, item_id: int, *, now: Any = None) -> _FakeItem | None:
        item = self.get_item(item_id)
        if item is None or item.kind != "plant":
            return None
        self.watered.append(item_id)
        item.state = {**item.state, "days_dry": 0.0}
        return item


def _make_worker(
    *,
    world: _FakeWorldStore,
    kv: _FakeKV,
    enabled: bool = True,
    cooldown: float = 5400.0,
    daily_cap: int = 6,
    seed: int = 0,
    notify: Any = None,
    intentional_hold_seconds: float = 0.0,
    outings_enabled: bool = True,
    outing_cooldown_seconds: float = 6.0 * 3600,
    outing_daily_cap: int = 2,
    period: str | None = None,
    episode_ratio: float = 0.0,
    episode_min_gap_seconds: float = 0.0,
    day_intention: bool = False,
    hobby: str | None = None,
    pursuit_notes: Any = None,
) -> IdleAwayActivityWorker:
    return IdleAwayActivityWorker(
        world_store=world,
        kv_get=kv.get,
        kv_set=kv.set,
        user_display_name_provider=lambda: "Jacob",
        enabled_provider=lambda: enabled,
        notify=notify,
        ollama=None,  # deterministic fallback
        model=None,
        interval_seconds=1200.0,
        cooldown_seconds=cooldown,
        daily_cap=daily_cap,
        journal_max=8,
        intentional_hold_seconds=intentional_hold_seconds,
        outings_enabled_provider=lambda: outings_enabled,
        outing_cooldown_seconds=outing_cooldown_seconds,
        outing_daily_cap=outing_daily_cap,
        circadian_period_provider=(
            (lambda: period) if period is not None else None
        ),
        episode_ratio=episode_ratio,
        episode_min_gap_seconds=episode_min_gap_seconds,
        day_intention_enabled=day_intention,
        hobby_provider=(lambda: hobby) if hobby is not None else None,
        pursuit_notes=pursuit_notes,
        rng=random.Random(seed),
    )


class ActivitySelectionTests(unittest.TestCase):
    def test_forced_snack_consumes_food_and_journals(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    7, "cookies", kind="food", consumable=True, quantity=2
                )
            ]
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("snack")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["key"], "snack")
        self.assertIn(7, world.consumed)
        journal = load_journal(kv.get)
        self.assertEqual(len(journal), 1)
        self.assertIn("cookies", journal[0]["summary"])

    def test_forced_move_cat_moves_item(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[_FakeItem(3, "the cat", kind="pet", location_id=1)],
            locations=[_FakeLoc(1, "the desk"), _FakeLoc(2, "the bed")],
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("move_cat")
        result = worker.run()
        self.assertEqual(result["key"], "move_cat")
        self.assertEqual(len(world.moved), 1)
        self.assertEqual(world.moved[0][0], 3)

    def test_beat_moves_aiko_to_matching_location(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            locations=[
                _FakeLoc(1, "the desk", "desk"),
                _FakeLoc(2, "the window seat", "window_seat"),
            ],
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("look_outside")
        worker.run()
        # set_state was called with the window-seat location id.
        self.assertTrue(world.set_state_calls)
        last = world.set_state_calls[-1]
        self.assertEqual(last["location_id"], 2)

    def test_wander_always_available_with_empty_room(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(items=[], locations=[])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        # set_state always called -> world mutated.
        self.assertTrue(world.set_state_calls)

    def test_world_mutation_broadcasts(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        patches: list[dict[str, Any]] = []
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, notify=patches.append
        )
        worker.force_activity("doodle")
        worker.run()
        self.assertTrue(any("state" in p for p in patches))


class JournalTests(unittest.TestCase):
    def test_journal_ring_trims_to_max(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, daily_cap=999)
        for _ in range(12):
            worker.force_activity("doodle")
            worker.run()
        journal = load_journal(kv.get)
        self.assertEqual(len(journal), 8)  # journal_max

    def test_load_journal_handles_garbage(self) -> None:
        kv = _FakeKV()
        kv.set(AWAY_ACTIVITIES_JOURNAL_KEY, "not json")
        self.assertEqual(load_journal(kv.get), [])


class GateTests(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, enabled=False)
        result = worker.run()
        self.assertTrue(result.get("disabled"))
        self.assertFalse(world.set_state_calls)

    def test_cooldown_blocks(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("away_activity.last_fired_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=5400.0)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_cooldown"))

    def test_daily_cap_blocks(self) -> None:
        kv = _FakeKV()
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("away_activity.day", today)
        kv.set("away_activity.day_count", "6")
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, daily_cap=6)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_daily_cap"))

    def test_garden_visit_outstanding_defers(self) -> None:
        kv = _FakeKV()
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        kv.set("garden_visit.return_at", future.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_garden_visit"))

    def test_intentional_hold_defers(self) -> None:
        kv = _FakeKV()
        # Brain/user placed Aiko 1 min ago; hold window is 2h.
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("world.intentional_state_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, intentional_hold_seconds=7200.0
        )
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_intentional_hold"))
        self.assertFalse(world.set_state_calls)

    def test_intentional_hold_expired_allows_beat(self) -> None:
        kv = _FakeKV()
        # Placed 3h ago; outside the 2h hold window -> worker free again.
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        kv.set("world.intentional_state_at", old.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, intentional_hold_seconds=7200.0
        )
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertTrue(world.set_state_calls)

    def test_intentional_hold_disabled_ignores_stamp(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        kv.set("world.intentional_state_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, intentional_hold_seconds=0.0
        )
        result = worker.run()
        self.assertEqual(result["fired"], 1)


class ItemEffectTests(unittest.TestCase):
    """K91 pass 2 — a beat leaves a trace on the thing it used."""

    def _book(self) -> _FakeItem:
        return _FakeItem(
            4,
            "The Glasshouse Letters",
            kind="book",
            slug="scifi_paperback",
            state={
                "title": "The Glasshouse Letters",
                "blurb": "two botanists",
                "progress": 3,
                "total": 16,
                "status": "reading",
            },
        )

    def _pot(self, fullness: str = "full") -> _FakeItem:
        return _FakeItem(
            9,
            "tea pot",
            kind="gadget",
            slug="tea_pot",
            state={"fullness": fullness, "flavor": "genmaicha"},
        )

    def test_reading_advances_the_book(self) -> None:
        kv = _FakeKV()
        book = self._book()
        world = _FakeWorldStore(items=[book])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("read_book")
        result = worker.run()
        self.assertEqual(result["key"], "read_book")
        self.assertEqual(book.state["progress"], 4)
        self.assertEqual(result["item_effect"]["progress"], 4)

    def test_two_reading_beats_report_different_places(self) -> None:
        kv = _FakeKV()
        book = self._book()
        world = _FakeWorldStore(items=[book])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, daily_cap=99)
        worker.force_activity("read_book")
        worker.run()
        worker.force_activity("read_book")
        worker.run()
        journal = load_journal(kv.get)
        self.assertEqual(len(journal), 2)
        self.assertNotEqual(journal[0]["summary"], journal[1]["summary"])
        self.assertIn("three chapters in", journal[0]["summary"])
        self.assertIn("four chapters in", journal[1]["summary"])

    def test_finishing_a_book_seeds_a_cue_and_starts_a_new_one(self) -> None:
        kv = _FakeKV()
        book = self._book()
        book.state["progress"] = 15  # one chapter from the end of 16
        world = _FakeWorldStore(items=[book])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("read_book")
        result = worker.run()
        self.assertEqual(
            result["item_effect"]["finished"], "The Glasshouse Letters"
        )
        self.assertNotEqual(book.state["title"], "The Glasshouse Letters")
        self.assertEqual(book.state["progress"], 0)
        seeds = load_idle_seeds(kv.get)
        self.assertTrue(seeds)
        self.assertIn("Glasshouse", seeds[-1]["seed"])

    def test_pouring_tea_empties_the_pot_one_step(self) -> None:
        kv = _FakeKV()
        pot = self._pot("full")
        world = _FakeWorldStore(items=[pot])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("tea")
        result = worker.run()
        self.assertEqual(result["key"], "tea")
        self.assertEqual(pot.state["fullness"], "half")
        self.assertIn("genmaicha", result["summary"])

    def test_empty_pot_offers_no_tea_beat(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(items=[self._pot("empty")])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("tea")
        result = worker.run()
        # The forced key produced no candidate, so some other beat ran.
        self.assertEqual(result["fired"], 1)
        self.assertNotEqual(result["key"], "tea")

    def test_snack_names_the_last_cookie(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    7, "cookies", kind="food", consumable=True, quantity=1,
                    state={"flavor": "chocolate chip"},
                )
            ]
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("snack")
        result = worker.run()
        self.assertIn("last of the", result["summary"])

    def test_lunch_prefers_produce_over_the_biscuit_tin(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    7, "cookies", kind="food", consumable=True, quantity=8,
                    slug="cookie_jar",
                ),
                _FakeItem(
                    8, "ripe tomatoes", kind="food", consumable=True,
                    quantity=4, slug="tomatoes",
                ),
            ]
        )
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, period="midday",
        )
        worker.force_activity("snack")
        result = worker.run()
        self.assertIn("lunch", result["summary"])
        self.assertIn("tomatoes", result["summary"])
        self.assertIn(8, world.consumed)

    def test_a_late_night_beat_raids_the_treats(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    7, "cookies", kind="food", consumable=True, quantity=8,
                    slug="cookie_jar",
                ),
                _FakeItem(
                    8, "ripe tomatoes", kind="food", consumable=True,
                    quantity=4, slug="tomatoes",
                ),
            ]
        )
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, period="late_night",
        )
        worker.force_activity("snack")
        result = worker.run()
        self.assertIn("midnight snack", result["summary"])
        self.assertIn(7, world.consumed)

    def test_effect_failure_does_not_lose_the_beat(self) -> None:
        kv = _FakeKV()
        book = self._book()
        world = _FakeWorldStore(items=[book])

        def boom(*_args: Any, **_kwargs: Any):
            raise RuntimeError("store down")

        world.update_item = boom  # type: ignore[method-assign]
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("read_book")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertNotIn("item_effect", result)
        self.assertEqual(len(load_journal(kv.get)), 1)


class EpisodeTests(unittest.TestCase):
    """K91 pass 3 — a long quiet stretch plays out as a sequence."""

    def _furnished(self) -> _FakeWorldStore:
        return _FakeWorldStore(
            items=[
                _FakeItem(
                    9, "tea pot", kind="gadget", slug="tea_pot",
                    state={"fullness": "full", "flavor": "genmaicha"},
                ),
                _FakeItem(
                    4, "The Glasshouse Letters", kind="book",
                    slug="scifi_paperback",
                    state={
                        "title": "The Glasshouse Letters",
                        "progress": 3,
                        "total": 16,
                    },
                ),
            ],
            locations=[
                _FakeLoc(1, "the desk", "desk"),
                _FakeLoc(2, "the kitchenette", "kitchenette"),
                _FakeLoc(3, "the beanbag", "beanbag"),
            ],
        )

    def test_a_long_gap_chains_beats_into_one_entry(self) -> None:
        kv = _FakeKV()
        world = self._furnished()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, episode_ratio=1.0,
        )
        worker.force_activity("tea")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertGreaterEqual(len(result["episode"]), 2)
        self.assertEqual(result["episode"][0], "tea")
        # One journal entry for the whole episode, carrying the chain.
        journal = load_journal(kv.get)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["keys"], result["episode"])
        self.assertIn(", then ", journal[0]["summary"])

    def test_every_beat_in_the_chain_applies_its_effect(self) -> None:
        kv = _FakeKV()
        world = self._furnished()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, episode_ratio=1.0, seed=3,
        )
        worker.force_activity("tea")
        result = worker.run()
        chain = result["episode"]
        pot = world.get_item(9)
        book = world.get_item(4)
        assert pot is not None and book is not None
        self.assertEqual(pot.state["fullness"], "half")
        if "read_book" in chain:
            self.assertEqual(book.state["progress"], 4)

    def test_a_recent_beat_stays_a_single_postcard(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        kv.set("away_activity.last_fired_at", recent.isoformat())
        world = self._furnished()
        worker = _make_worker(
            world=world,
            kv=kv,
            cooldown=0.0,
            episode_ratio=1.0,
            episode_min_gap_seconds=10800.0,
        )
        worker.force_activity("tea")
        result = worker.run()
        self.assertNotIn("episode", result)
        self.assertEqual(len(load_journal(kv.get)), 1)
        self.assertNotIn("keys", load_journal(kv.get)[0])

    def test_episodes_off_by_ratio_keeps_single_beats(self) -> None:
        kv = _FakeKV()
        world = self._furnished()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, episode_ratio=0.0,
        )
        worker.force_activity("tea")
        result = worker.run()
        self.assertNotIn("episode", result)

    def test_an_episode_costs_one_beat_against_the_daily_cap(self) -> None:
        kv = _FakeKV()
        world = self._furnished()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, episode_ratio=1.0, daily_cap=1,
        )
        worker.force_activity("tea")
        first = worker.run()
        self.assertEqual(first["fired"], 1)
        second = worker.run()
        self.assertTrue(second.get("skipped_daily_cap"))

    def test_recency_reads_every_beat_of_an_episode(self) -> None:
        kv = _FakeKV()
        world = self._furnished()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, episode_ratio=1.0,
        )
        worker.force_activity("tea")
        result = worker.run()
        self.assertEqual(worker._recent_keys(), result["episode"])

    def _composed(self):
        """A beat composed outside the candidate pool.

        Both the H14 LLM path and the day-intention path build one of
        these directly, so a plan whose key the room does not afford is a
        normal thing to reach ``_plan_episode``.
        """
        from app.core.world.idle_activity_worker import ActivityPlan

        return ActivityPlan(
            key="snack",
            posture="leaning",
            activity="snacking",
            summary="I raided the cupboard for something sweet",
            precomposed=True,
        )

    def test_a_beat_outside_the_pool_still_leads_its_own_chain(self) -> None:
        """Filtering the chain against the pool dropped the chosen beat."""
        kv = _FakeKV()
        worker = _make_worker(
            world=self._furnished(), kv=kv, cooldown=0.0, episode_ratio=1.0,
        )
        snapshot = worker._build_candidates("Jacob", _utc_now())
        composed = self._composed()
        self.assertNotIn("snack", snapshot.candidates)
        chain = worker._plan_episode(composed, snapshot, _utc_now())
        self.assertIs(chain[0], composed)

    def test_an_unaffording_room_yields_the_one_beat_not_none(self) -> None:
        """The observed crash: an empty chain, then IndexError on chain[0]."""
        import dataclasses

        kv = _FakeKV()
        worker = _make_worker(
            world=self._furnished(), kv=kv, cooldown=0.0, episode_ratio=1.0,
        )
        bare = dataclasses.replace(
            worker._build_candidates("Jacob", _utc_now()), candidates={},
        )
        composed = self._composed()
        chain = worker._plan_episode(composed, bare, _utc_now())
        self.assertEqual(chain, [composed])


class _Notes:
    """Captures pursuit-note writes without a memory layer."""

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


class PursuitNoteTests(unittest.TestCase):
    """K85b — the beats worth keeping past the eight-entry ring."""

    def test_a_beat_that_changed_the_room_is_kept(self) -> None:
        kv = _FakeKV()
        notes = _Notes()
        book = _FakeItem(
            4, "The Glasshouse Letters", kind="book", slug="scifi_paperback",
            state={"title": "The Glasshouse Letters", "progress": 3,
                   "total": 16},
        )
        worker = _make_worker(
            world=_FakeWorldStore(items=[book]), kv=kv, cooldown=0.0,
            pursuit_notes=notes,
        )
        worker.force_activity("read_book")
        result = worker.run()
        self.assertEqual(len(notes.written), 1)
        entry = notes.written[0]
        self.assertEqual(entry["source"], "away_beat")
        self.assertEqual(entry["topic"], "read_book")
        self.assertEqual(entry["content"], result["summary"])
        self.assertEqual(entry["extra"]["changed"], ["advance_book"])
        self.assertEqual(result["pursuit_note_id"], 1)

    def test_a_beat_that_left_no_trace_is_not(self) -> None:
        kv = _FakeKV()
        notes = _Notes()
        worker = _make_worker(
            world=_FakeWorldStore(items=[], locations=[]), kv=kv,
            cooldown=0.0, pursuit_notes=notes,
        )
        worker.force_activity("doodle")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(notes.written, [])
        self.assertNotIn("pursuit_note_id", result)
        # It still belongs in the ring -- it happened.
        self.assertEqual(len(load_journal(kv.get)), 1)

    def test_an_episode_is_kept_whole(self) -> None:
        kv = _FakeKV()
        notes = _Notes()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    9, "tea pot", kind="gadget", slug="tea_pot",
                    state={"fullness": "full", "flavor": "genmaicha"},
                ),
                _FakeItem(
                    4, "The Glasshouse Letters", kind="book",
                    slug="scifi_paperback",
                    state={"title": "The Glasshouse Letters", "progress": 3,
                           "total": 16},
                ),
            ],
            locations=[
                _FakeLoc(1, "the desk", "desk"),
                _FakeLoc(2, "the kitchenette", "kitchenette"),
                _FakeLoc(3, "the beanbag", "beanbag"),
            ],
        )
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, episode_ratio=1.0,
            pursuit_notes=notes,
        )
        worker.force_activity("tea")
        result = worker.run()
        self.assertEqual(len(notes.written), 1)
        self.assertEqual(
            notes.written[0]["extra"]["keys"], result["episode"],
        )

    def test_no_writer_is_not_an_error(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=_FakeWorldStore(), kv=kv, cooldown=0.0,
        )
        worker.force_activity("doodle")
        self.assertEqual(worker.run()["fired"], 1)


class DayIntentionTests(unittest.TestCase):
    """K91 pass 4 — the day has something she meant to get to."""

    def _book_world(self, progress: int = 14) -> _FakeWorldStore:
        return _FakeWorldStore(
            items=[
                _FakeItem(
                    4, "The Glasshouse Letters", kind="book",
                    slug="scifi_paperback",
                    state={
                        "title": "The Glasshouse Letters",
                        "progress": progress,
                        "total": 16,
                    },
                )
            ],
            locations=[_FakeLoc(1, "the desk", "desk")],
        )

    def test_first_beat_of_the_day_sets_an_intention(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=self._book_world(), kv=kv, cooldown=0.0, day_intention=True,
        )
        worker.run()
        stored = day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY))
        assert stored is not None
        self.assertEqual(stored.text, "finish The Glasshouse Letters")
        self.assertEqual(stored.beat_key, "read_book")

    def test_the_intention_is_not_re_picked_within_a_day(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=self._book_world(),
            kv=kv,
            cooldown=0.0,
            daily_cap=99,
            day_intention=True,
        )
        worker.run()
        first = kv.get(day_intention.DAY_INTENTION_KEY)
        worker.force_activity("doodle")
        worker.run()
        self.assertEqual(kv.get(day_intention.DAY_INTENTION_KEY), first)

    def test_yesterdays_intention_is_replaced(self) -> None:
        kv = _FakeKV()
        stale = day_intention.DayIntention(
            day="2001-01-01", text="something ancient", beat_key="doodle",
        )
        kv.set(day_intention.DAY_INTENTION_KEY, day_intention.dump(stale))
        worker = _make_worker(
            world=self._book_world(), kv=kv, cooldown=0.0, day_intention=True,
        )
        worker.run()
        stored = day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY))
        assert stored is not None
        self.assertNotEqual(stored.text, "something ancient")
        self.assertEqual(stored.day, day_intention.local_day(_utc_now()))

    def test_satisfying_the_intention_says_so_once(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=self._book_world(),
            kv=kv,
            cooldown=0.0,
            daily_cap=99,
            day_intention=True,
        )
        worker.force_activity("read_book")
        result = worker.run()
        self.assertTrue(result.get("closed_intention"))
        self.assertIn(" — ", result["summary"])
        stored = day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY))
        assert stored is not None
        self.assertTrue(stored.satisfied)
        # A second reading beat doesn't re-close it.
        worker.force_activity("read_book")
        again = worker.run()
        self.assertFalse(again.get("closed_intention"))

    def test_an_unrelated_beat_leaves_the_intention_open(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=self._book_world(), kv=kv, cooldown=0.0, day_intention=True,
        )
        worker.force_activity("doodle")
        result = worker.run()
        self.assertFalse(result.get("closed_intention"))
        stored = day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY))
        assert stored is not None
        self.assertFalse(stored.satisfied)

    def test_a_garden_intention_is_left_for_the_garden_worker(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    13, "lettuce", kind="plant",
                    state={
                        "stage": "growing",
                        "last_watered_at": (
                            _utc_now() - timedelta(days=4)
                        ).isoformat(),
                    },
                )
            ],
            locations=[_FakeLoc(1, "the desk", "desk")],
        )
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, day_intention=True,
        )
        result = worker.run()
        stored = day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY))
        assert stored is not None
        self.assertEqual(stored.beat_key, "garden")
        # No idle beat can service it, so it stays open.
        self.assertFalse(stored.satisfied)
        self.assertFalse(result.get("closed_intention"))

    def test_disabled_switch_writes_no_intention(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=self._book_world(), kv=kv, cooldown=0.0, day_intention=False,
        )
        worker.force_activity("read_book")
        result = worker.run()
        self.assertIsNone(kv.get(day_intention.DAY_INTENTION_KEY))
        self.assertFalse(result.get("closed_intention"))

    def test_hobby_feeds_the_intention_when_the_room_is_content(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=_FakeWorldStore(),
            kv=kv,
            cooldown=0.0,
            day_intention=True,
            hobby="mapping the constellations",
        )
        worker.run()
        stored = day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY))
        assert stored is not None
        self.assertIn("constellations", stored.text)

    def test_debug_state_reports_the_intention(self) -> None:
        kv = _FakeKV()
        worker = _make_worker(
            world=self._book_world(), kv=kv, cooldown=0.0, day_intention=True,
        )
        worker.run()
        state = worker.day_intention_debug_state()
        self.assertTrue(state["enabled"])
        self.assertEqual(
            state["intention"]["text"], "finish The Glasshouse Letters"
        )


class NamedEffectResolutionTests(unittest.TestCase):
    """The H14 ``changed_item`` name is validated, never trusted."""

    def _worker(self) -> IdleAwayActivityWorker:
        return _make_worker(world=_FakeWorldStore(), kv=_FakeKV(), cooldown=0.0)

    def test_plant_resolves_to_watering(self) -> None:
        plant = _FakeItem(13, "lavender pot", kind="plant")
        effect = self._worker()._resolve_named_effect("lavender pot", [plant])
        assert effect is not None
        self.assertEqual(effect.action, EFFECT_WATER_PLANT)
        self.assertEqual(effect.item_id, 13)

    def test_tea_pot_resolves_by_slug(self) -> None:
        pot = _FakeItem(9, "tea pot", kind="gadget", slug="tea_pot")
        effect = self._worker()._resolve_named_effect("Tea Pot", [pot])
        assert effect is not None
        self.assertEqual(effect.action, EFFECT_POUR_TEA)

    def test_unknown_name_is_dropped(self) -> None:
        self.assertIsNone(
            self._worker()._resolve_named_effect("a unicorn", [_FakeItem(1, "lamp")])
        )

    def test_item_without_a_transition_is_dropped(self) -> None:
        keyboard = _FakeItem(2, "retro keyboard", kind="gadget")
        self.assertIsNone(
            self._worker()._resolve_named_effect("retro keyboard", [keyboard])
        )

    def test_blank_name_is_dropped(self) -> None:
        self.assertIsNone(self._worker()._resolve_named_effect("", []))
        self.assertIsNone(self._worker()._resolve_named_effect(None, []))


class OutingTests(unittest.TestCase):
    """H22 — the rare 'I stepped out for a bit' away-beat."""

    def test_forced_outing_journals_and_stamps(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, period="afternoon")
        worker.force_activity("outing")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["key"], "outing")
        journal = load_journal(kv.get)
        self.assertEqual(journal[-1]["key"], "outing")
        # Outing watermarks were stamped so the next one is gated.
        self.assertIsNotNone(kv.get("outing.last_fired_at"))
        self.assertEqual(kv.get("outing.day_count"), "1")

    def test_outing_not_offered_when_disabled(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outings_enabled=False,
            period="afternoon",
        )
        # Even forced, a disabled outing is not added to candidates, so the
        # pick falls back to another beat (never key == "outing").
        worker.force_activity("outing")
        result = worker.run()
        self.assertNotEqual(result.get("key"), "outing")

    def test_outing_cooldown_blocks_repeat(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        kv.set("outing.last_fired_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0,
            outing_cooldown_seconds=6.0 * 3600, period="afternoon",
        )
        now = datetime.now(timezone.utc)
        self.assertFalse(worker._outing_eligible(now))

    def test_outing_daily_cap_blocks(self) -> None:
        kv = _FakeKV()
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("outing.day", today)
        kv.set("outing.day_count", "2")
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outing_daily_cap=2,
            outing_cooldown_seconds=0.0, period="afternoon",
        )
        self.assertFalse(worker._outing_eligible(datetime.now(timezone.utc)))

    def test_outing_blocked_at_night(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outing_cooldown_seconds=0.0,
            period="late_night",
        )
        self.assertFalse(worker._outing_eligible(datetime.now(timezone.utc)))

    def test_outing_eligible_in_daylight(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outing_cooldown_seconds=0.0,
            period="morning",
        )
        self.assertTrue(worker._outing_eligible(datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()

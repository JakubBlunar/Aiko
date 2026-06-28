"""Tests for H17 — idle beats feed the idea machine.

Two halves: the producer (``IdleAwayActivityWorker._maybe_emit_seed`` →
``aiko.idle_seeds`` kv ring, gated by the worker model + ratio + daily cap)
and the consumer (``_render_idle_seed_block`` cue producer — watermark +
wall-clock surfacing cooldown, NOT part of the ``_gap_cue_surfaced``
gap-return family).
"""
from __future__ import annotations

import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.world.idle_activity_worker import (
    IDLE_SEEDS_KEY,
    ActivityPlan,
    IdleAwayActivityWorker,
    load_idle_seeds,
)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeLoc:
    def __init__(self, id_: int, name: str, slug: str = "") -> None:
        self.id = id_
        self.name = name
        self.slug = slug or name.lower().replace(" ", "_")


class _FakeWorldStore:
    def list_items(self) -> list[Any]:
        return []

    def list_locations(self) -> list[Any]:
        return [_FakeLoc(1, "the desk", "desk")]


class _FakeOllama:
    """Returns a fixed seed via chat_json regardless of prompt."""

    def __init__(self, seed_text: str = "I wonder if Jacob has read it.") -> None:
        self.seed_text = seed_text
        self.calls = 0

    def chat_json(self, messages, *, model, **kwargs):
        self.calls += 1
        return json.dumps({"seed": self.seed_text}), None


def _worker(
    *,
    kv: _FakeKV,
    ollama: Any = None,
    idle_seed_ratio: float = 1.0,
    idle_seed_daily_cap: int = 3,
    seed: int = 0,
) -> IdleAwayActivityWorker:
    return IdleAwayActivityWorker(
        world_store=_FakeWorldStore(),
        kv_get=kv.get,
        kv_set=kv.set,
        user_display_name_provider=lambda: "Jacob",
        enabled_provider=lambda: True,
        ollama=ollama,
        model="worker-model" if ollama is not None else None,
        interval_seconds=1200.0,
        cooldown_seconds=0.0,
        daily_cap=6,
        journal_max=8,
        idle_seed_ratio=idle_seed_ratio,
        idle_seed_daily_cap=idle_seed_daily_cap,
        rng=random.Random(seed),
    )


def _plan() -> ActivityPlan:
    return ActivityPlan(
        key="read_book",
        posture="curled_up",
        activity="reading_a_novel",
        summary="read a few chapters of a novel",
    )


class ProducerTests(unittest.TestCase):
    def test_no_seed_without_model(self) -> None:
        kv = _FakeKV()
        worker = _worker(kv=kv, ollama=None)
        out = worker._maybe_emit_seed(
            datetime.now(timezone.utc), "Jacob", _plan(), "read a novel"
        )
        self.assertIsNone(out)
        self.assertEqual(load_idle_seeds(kv.get), [])

    def test_seed_emitted_and_ringed(self) -> None:
        kv = _FakeKV()
        worker = _worker(kv=kv, ollama=_FakeOllama())
        out = worker._maybe_emit_seed(
            datetime.now(timezone.utc), "Jacob", _plan(), "read a novel"
        )
        self.assertIsNotNone(out)
        ring = load_idle_seeds(kv.get)
        self.assertEqual(len(ring), 1)
        self.assertEqual(ring[0]["activity"], "reading_a_novel")
        self.assertTrue(ring[0]["seed"])

    def test_ratio_zero_never_emits(self) -> None:
        kv = _FakeKV()
        worker = _worker(kv=kv, ollama=_FakeOllama(), idle_seed_ratio=0.0)
        out = worker._maybe_emit_seed(
            datetime.now(timezone.utc), "Jacob", _plan(), "read a novel"
        )
        self.assertIsNone(out)

    def test_daily_cap_blocks_further_seeds(self) -> None:
        kv = _FakeKV()
        worker = _worker(
            kv=kv, ollama=_FakeOllama(), idle_seed_daily_cap=1
        )
        now = datetime.now(timezone.utc)
        first = worker._maybe_emit_seed(now, "Jacob", _plan(), "read a novel")
        self.assertIsNotNone(first)
        second = worker._maybe_emit_seed(now, "Jacob", _plan(), "read a novel")
        self.assertIsNone(second)
        self.assertEqual(len(load_idle_seeds(kv.get)), 1)

    def test_ring_trims_to_max(self) -> None:
        kv = _FakeKV()
        worker = _worker(
            kv=kv, ollama=_FakeOllama(), idle_seed_daily_cap=100
        )
        worker._idle_seed_max_ring = 3
        now = datetime.now(timezone.utc)
        for _ in range(5):
            worker._maybe_emit_seed(now, "Jacob", _plan(), "read a novel")
        self.assertEqual(len(load_idle_seeds(kv.get)), 3)


# ── consumer ──────────────────────────────────────────────────────────


class _FakeChatDb:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(idle_seed_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        seeds: list[dict[str, Any]] | None = None,
        force_next: bool = False,
        agent_settings: SimpleNamespace | None = None,
        cooldown: int = 1800,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent_settings or _agent())
        self._memory_settings = SimpleNamespace(
            idle_seed_surface_cooldown_seconds=cooldown,
        )
        self._chat_db = _FakeChatDb()
        if seeds:
            self._chat_db.store[IDLE_SEEDS_KEY] = json.dumps(seeds)
        self._idle_seed_force_next = force_next
        self._gap_cue_surfaced = False
        self.user_display_name = "Jacob"


def _seed_row(
    at: str = "2026-06-13T18:55:00+00:00",
    activity: str = "reading_a_novel",
    seed: str = "I wonder if Jacob would like this book.",
) -> dict[str, Any]:
    return {"at": at, "activity": activity, "key": "read_book", "seed": seed}


class ConsumerSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(
            seeds=[_seed_row()],
            agent_settings=_agent(idle_seed_enabled=False),
        )
        self.assertEqual(host._render_idle_seed_block(), "")

    def test_empty_ring_silent(self) -> None:
        host = _Host(seeds=[])
        self.assertEqual(host._render_idle_seed_block(), "")


class ConsumerSurfacingTests(unittest.TestCase):
    def test_fires_and_advances_watermark(self) -> None:
        host = _Host(seeds=[_seed_row()])
        out = host._render_idle_seed_block()
        self.assertTrue(out.startswith("Earlier"))
        self.assertIn("reading a novel", out)
        self.assertIn("I wonder if Jacob would like this book.", out)
        self.assertEqual(
            host._chat_db.store.get("idle_seed.surfaced_at"), _seed_row()["at"]
        )
        self.assertIn("idle_seed.surfaced_clock", host._chat_db.store)

    def test_already_surfaced_is_silent(self) -> None:
        host = _Host(seeds=[_seed_row()])
        host._chat_db.store["idle_seed.surfaced_at"] = _seed_row()["at"]
        self.assertEqual(host._render_idle_seed_block(), "")

    def test_cooldown_blocks_second_surface(self) -> None:
        # A fresh seed exists but we just surfaced one moments ago.
        host = _Host(seeds=[_seed_row(at="2026-06-14T10:00:00+00:00")])
        host._chat_db.store["idle_seed.surfaced_clock"] = datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
        self.assertEqual(host._render_idle_seed_block(), "")

    def test_cooldown_elapsed_allows_surface(self) -> None:
        host = _Host(seeds=[_seed_row()])
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        host._chat_db.store["idle_seed.surfaced_clock"] = old.isoformat(
            timespec="seconds"
        )
        out = host._render_idle_seed_block()
        self.assertTrue(out.startswith("Earlier"))

    def test_does_not_touch_gap_cue_flag(self) -> None:
        host = _Host(seeds=[_seed_row()])
        host._gap_cue_surfaced = True
        out = host._render_idle_seed_block()
        self.assertTrue(out)
        self.assertTrue(host._gap_cue_surfaced)


class ConsumerForceTests(unittest.TestCase):
    def test_force_next_bypasses_watermark_and_cooldown(self) -> None:
        host = _Host(seeds=[_seed_row()], force_next=True)
        host._chat_db.store["idle_seed.surfaced_at"] = _seed_row()["at"]
        host._chat_db.store["idle_seed.surfaced_clock"] = datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
        out = host._render_idle_seed_block()
        self.assertTrue(out.startswith("Earlier"))
        self.assertFalse(host._idle_seed_force_next)


if __name__ == "__main__":
    unittest.main()

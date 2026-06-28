"""Tests for H19 — hobbies & ongoing personal projects.

Three layers: the pure :mod:`app.core.world.hobby` math (catalogue pick,
progress line, milestone / rotation predicates), the
:class:`app.core.proactive.hobby_worker.HobbyWorker` state machine
(start → advance → milestone seed → rotate, plus the wall-clock advance
pacing), and the standing ``_render_hobby_block`` provider.
"""
from __future__ import annotations

import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.proactive.hobby_worker import (
    KV_CURRENT_HOBBY,
    HobbyWorker,
    load_hobby,
)
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.world import hobby as hobby_mod
from app.core.world.idle_activity_worker import load_idle_seeds


class _FakeChatDb:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeOllama:
    def __init__(self, seed_text: str = "I keep thinking about that twist.") -> None:
        self.seed_text = seed_text

    def chat_json(self, messages, *, model, **kwargs):
        return json.dumps({"seed": self.seed_text}), None


def _mem(**overrides: Any) -> SimpleNamespace:
    base = dict(
        hobby_worker_interval_seconds=3600,
        hobby_advance_min_hours=6.0,
        hobby_milestone_every=3,
        hobby_max_advances=12,
        idle_seed_max_ring=6,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _worker(
    *,
    db: _FakeChatDb,
    mem: SimpleNamespace | None = None,
    enabled: bool = True,
    ollama: Any = None,
    seed: int = 0,
) -> HobbyWorker:
    return HobbyWorker(
        chat_db=db,
        agent_settings=SimpleNamespace(hobby_worker_enabled=enabled),
        memory_settings=mem or _mem(),
        user_display_name_provider=lambda: "Jacob",
        ollama=ollama,
        model="worker-model" if ollama is not None else None,
        rng=random.Random(seed),
    )


# ── pure math ─────────────────────────────────────────────────────────


class HobbyMathTests(unittest.TestCase):
    def test_pick_excludes_key(self) -> None:
        rng = random.Random(1)
        for _ in range(20):
            tpl = hobby_mod.pick_hobby(rng, exclude=("scifi_series",))
            self.assertNotEqual(tpl.key, "scifi_series")

    def test_render_line_just_started(self) -> None:
        self.assertIn("just started", hobby_mod.render_hobby_line("x", 0, "chapter"))

    def test_render_line_singular_plural(self) -> None:
        self.assertEqual(
            hobby_mod.render_hobby_line("a series", 1, "chapter"),
            "a series (1 chapter in)",
        )
        self.assertEqual(
            hobby_mod.render_hobby_line("a series", 4, "chapter"),
            "a series (4 chapters in)",
        )

    def test_should_rotate(self) -> None:
        self.assertTrue(hobby_mod.should_rotate(progress=12, advances=12, max_advances=12))
        self.assertFalse(hobby_mod.should_rotate(progress=5, advances=5, max_advances=12))
        # 0 disables rotation.
        self.assertFalse(hobby_mod.should_rotate(progress=99, advances=99, max_advances=0))

    def test_is_milestone(self) -> None:
        self.assertTrue(hobby_mod.is_milestone(advances=3, every=3))
        self.assertTrue(hobby_mod.is_milestone(advances=6, every=3))
        self.assertFalse(hobby_mod.is_milestone(advances=2, every=3))
        self.assertFalse(hobby_mod.is_milestone(advances=0, every=3))
        # 0 disables milestones.
        self.assertFalse(hobby_mod.is_milestone(advances=3, every=0))


# ── worker state machine ──────────────────────────────────────────────


class HobbyWorkerTests(unittest.TestCase):
    def test_first_run_starts_hobby(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db)
        result = worker.run()
        self.assertTrue(result.get("started"))
        state = load_hobby(db.kv_get)
        self.assertIsNotNone(state)
        self.assertEqual(state["progress"], 0)
        self.assertEqual(state["advances"], 0)

    def test_advance_paced_by_wall_clock(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db)
        worker.run()  # start
        # Immediately running again should NOT advance (just started, but
        # last_advanced_at is None → first advance allowed). Advance once.
        r1 = worker.run()
        self.assertTrue(r1.get("advanced"))
        # A second immediate run is blocked by the 6h pacing floor.
        r2 = worker.run()
        self.assertTrue(r2.get("waiting"))

    def test_force_advance_bypasses_pacing(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db)
        worker.run()  # start
        worker._force_advance = True
        worker.run()
        worker._force_advance = True
        r = worker.run()
        self.assertTrue(r.get("advanced"))
        self.assertEqual(load_hobby(db.kv_get)["progress"], 2)

    def test_milestone_emits_seed(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db, mem=_mem(hobby_milestone_every=2), ollama=_FakeOllama())
        worker.run()  # start
        # Force three advances; milestone at advances==2 should emit a seed.
        for _ in range(3):
            worker._force_advance = True
            worker.run()
        ring = load_idle_seeds(db.kv_get)
        self.assertTrue(ring)
        self.assertEqual(ring[-1]["key"], "hobby")
        self.assertTrue(ring[-1]["seed"])

    def test_no_seed_without_model(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db, mem=_mem(hobby_milestone_every=1), ollama=None)
        worker.run()  # start
        worker._force_advance = True
        worker.run()  # advances==1, milestone, but no model → no seed
        self.assertEqual(load_idle_seeds(db.kv_get), [])

    def test_rotation_changes_hobby(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db, mem=_mem(hobby_max_advances=2), ollama=_FakeOllama())
        worker.run()  # start
        first_key = load_hobby(db.kv_get)["key"]
        worker._force_advance = True
        worker.run()
        worker._force_advance = True
        worker.run()  # advances==2 → next run rotates
        r = worker.run()
        self.assertTrue(r.get("rotated"))
        new_key = load_hobby(db.kv_get)["key"]
        self.assertNotEqual(new_key, first_key)
        self.assertEqual(load_hobby(db.kv_get)["progress"], 0)

    def test_force_rotate(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db, ollama=_FakeOllama())
        worker.run()  # start
        worker._force_rotate = True
        r = worker.run()
        self.assertTrue(r.get("rotated"))

    def test_disabled_skips(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db, enabled=False)
        r = worker.run()
        self.assertTrue(r.get("skipped"))
        self.assertIsNone(load_hobby(db.kv_get))


# ── provider ──────────────────────────────────────────────────────────


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(hobby_worker_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        state: dict[str, Any] | None = None,
        agent_settings: SimpleNamespace | None = None,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent_settings or _agent())
        self._chat_db = _FakeChatDb()
        if state is not None:
            self._chat_db.store[KV_CURRENT_HOBBY] = json.dumps(state)


class HobbyProviderTests(unittest.TestCase):
    def test_empty_when_no_hobby(self) -> None:
        host = _Host()
        self.assertEqual(host._render_hobby_block(), "")

    def test_disabled_returns_empty(self) -> None:
        host = _Host(
            state={"label": "x", "progress": 3, "unit": "chapter"},
            agent_settings=_agent(hobby_worker_enabled=False),
        )
        self.assertEqual(host._render_hobby_block(), "")

    def test_renders_progress_line(self) -> None:
        host = _Host(
            state={
                "label": "working through a sci-fi series",
                "progress": 5,
                "unit": "chapter",
            }
        )
        out = host._render_hobby_block()
        self.assertIn("working through a sci-fi series", out)
        self.assertIn("5 chapters in", out)


if __name__ == "__main__":
    unittest.main()

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
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.core.proactive.hobby_worker import (
    KV_CURRENT_HOBBY,
    HobbyWorker,
    load_hobby,
    load_hobby_history,
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
    def __init__(
        self,
        seed_text: str = "I keep thinking about that twist.",
        proposal: dict[str, Any] | None = None,
    ) -> None:
        self.seed_text = seed_text
        self.proposal = proposal if proposal is not None else {
            "key": "skyline",
            "kind": "making",
            "unit": "sketch",
            "artifact": "the skyline from the window",
            "artifact_detail": "rooftops that keep fighting the perspective",
            "takeaway_hint": "whether the vanishing point finally behaved",
        }
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, messages, *, model, **kwargs):
        surface = kwargs.get("surface") or ""
        self.calls.append({"surface": surface, "messages": messages})
        if surface == "hobby_next":
            return json.dumps(self.proposal), None
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
    world_store: Any = None,
) -> HobbyWorker:
    return HobbyWorker(
        chat_db=db,
        agent_settings=SimpleNamespace(hobby_worker_enabled=enabled),
        memory_settings=mem or _mem(),
        user_display_name_provider=lambda: "Jacob",
        ollama=ollama,
        model="worker-model" if ollama is not None else None,
        world_store=world_store,
        rng=random.Random(seed),
    )


def _write_hobby(db: _FakeChatDb, **fields: Any) -> dict[str, Any]:
    state = {
        "key": "scifi_series",
        "label": "working through The Glasshouse Letters",
        "kind": "reading",
        "unit": "chapter",
        "artifact": "The Glasshouse Letters",
        "artifact_detail": "an epistolary novel about two botanists and a war",
        "progress": 0,
        "advances": 0,
        "started_at": "2026-07-01T10:00:00+00:00",
        "last_advanced_at": None,
    }
    state.update(fields)
    db.store[KV_CURRENT_HOBBY] = json.dumps(state)
    return state


class _FakeItem:
    def __init__(
        self,
        *,
        id_: int = 1,
        slug: str = "scifi_paperback",
        name: str = "The Glasshouse Letters",
        kind: str = "book",
        state: dict[str, Any] | None = None,
    ) -> None:
        self.id = id_
        self.slug = slug
        self.name = name
        self.kind = kind
        self.description = ""
        self.state = state or {
            "title": name,
            "blurb": "an epistolary novel about two botanists and a war",
            "progress": 5,
            "total": 12,
            "status": "reading",
        }


class _FakeWorld:
    def __init__(self, items: list[_FakeItem] | None = None) -> None:
        self._items = list(items or [_FakeItem()])

    def list_items(self, *, kind: str | None = None) -> list[_FakeItem]:
        items = list(self._items)
        if kind is not None:
            items = [i for i in items if i.kind == kind]
        return items

    def update_item(self, item_id: int, **kwargs: Any) -> _FakeItem | None:
        item = next((i for i in self._items if i.id == int(item_id)), None)
        if item is None:
            return None
        if kwargs.get("name"):
            item.name = str(kwargs["name"])
        if kwargs.get("description") is not None:
            item.description = str(kwargs["description"])
        if kwargs.get("state") is not None:
            item.state = dict(kwargs["state"])
        return item


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

    def test_standing_label_names_the_artifact(self) -> None:
        self.assertEqual(
            hobby_mod.standing_label("reading", "The Glasshouse Letters", "x"),
            "working through The Glasshouse Letters",
        )

    def test_render_line_rewrites_a_genre_label_when_artifact_is_set(self) -> None:
        line = hobby_mod.render_hobby_line(
            "working through a sci-fi series",
            5,
            "chapter",
            artifact="The Glasshouse Letters",
            kind="reading",
        )
        self.assertIn("The Glasshouse Letters", line)
        self.assertNotIn("sci-fi series", line)
        self.assertIn("5 chapters in", line)

    def test_prompt_progress_for_reading_uses_the_room_book(self) -> None:
        progress, unit = hobby_mod.prompt_progress(
            {"kind": "reading", "progress": 99, "unit": "chapter"},
            {"progress": 5, "total": 12},
        )
        self.assertEqual(progress, 5)
        self.assertEqual(unit, "chapter")

    def test_admit_rejects_missing_artifact(self) -> None:
        self.assertIsNone(hobby_mod.admit_proposal(
            {"kind": "making", "artifact": ""}, leaving_kind="reading",
        ))

    def test_admit_rejects_genre_only_artifact(self) -> None:
        for body in ("a sci-fi book", "sketching", "music", "reading"):
            with self.subTest(artifact=body):
                self.assertIsNone(hobby_mod.admit_proposal(
                    {"kind": "making", "artifact": body},
                    leaving_kind="reading",
                ))

    def test_admit_rejects_same_kind_as_the_wrapping_hobby(self) -> None:
        self.assertIsNone(hobby_mod.admit_proposal(
            {
                "kind": "reading",
                "artifact": "Eleven Doors",
                "artifact_detail": "a twisty thriller",
            },
            leaving_kind="reading",
        ))

    def test_admit_rejects_a_recent_artifact(self) -> None:
        self.assertIsNone(hobby_mod.admit_proposal(
            {
                "kind": "making",
                "artifact": "the skyline from the window",
            },
            leaving_kind="reading",
            recent_artifacts=("the skyline from the window",),
        ))

    def test_admit_accepts_a_named_other_kind(self) -> None:
        proposal = hobby_mod.admit_proposal(
            {
                "kind": "making",
                "artifact": "the skyline from the window",
                "artifact_detail": "rooftops",
            },
            leaving_kind="reading",
        )
        assert proposal is not None
        self.assertEqual(proposal.kind, "making")
        self.assertEqual(proposal.artifact, "the skyline from the window")
        self.assertIn("skyline", proposal.label)

    def test_pick_hobby_excludes_kind(self) -> None:
        rng = random.Random(2)
        for _ in range(20):
            tpl = hobby_mod.pick_hobby(rng, exclude_kinds=("reading",))
            self.assertNotEqual(tpl.kind, "reading")

    def test_seed_proposal_carries_an_artifact(self) -> None:
        rng = random.Random(0)
        tpl = hobby_mod.pick_hobby(rng)
        proposal = hobby_mod.proposal_from_template(tpl, rng)
        self.assertTrue(proposal.artifact)
        self.assertFalse(hobby_mod.is_genre_artifact(proposal.artifact))


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
        self.assertTrue(state.get("artifact"))
        self.assertFalse(hobby_mod.is_genre_artifact(str(state["artifact"])))

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
        ollama = _FakeOllama()
        worker = _worker(db=db, mem=_mem(hobby_milestone_every=2), ollama=ollama)
        worker.run()  # start
        # Force three advances; milestone at advances==2 should emit a seed.
        for _ in range(3):
            worker._force_advance = True
            worker.run()
        ring = load_idle_seeds(db.kv_get)
        self.assertTrue(ring)
        self.assertEqual(ring[-1]["key"], "hobby")
        self.assertTrue(ring[-1]["seed"])
        self.assertTrue(any(c["surface"] == "hobby_seed" for c in ollama.calls))

    def test_milestone_context_names_the_artifact(self) -> None:
        db = _FakeChatDb()
        ollama = _FakeOllama()
        worker = _worker(db=db, mem=_mem(hobby_milestone_every=1), ollama=ollama)
        _write_hobby(db, progress=2, advances=0)
        worker._force_advance = True
        worker.run()
        seed_calls = [c for c in ollama.calls if c["surface"] == "hobby_seed"]
        self.assertTrue(seed_calls)
        blob = " ".join(
            m["content"] for m in seed_calls[0]["messages"]
        )
        self.assertIn("The Glasshouse Letters", blob)
        self.assertIn("epistolary", blob)
        self.assertNotIn("a twist or character in the series", blob)

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

    def test_llm_proposal_of_other_kind_becomes_the_only_hobby(self) -> None:
        db = _FakeChatDb()
        proposal = {
            "key": "skyline",
            "kind": "making",
            "unit": "sketch",
            "artifact": "the skyline from the window",
            "artifact_detail": "rooftops that keep fighting",
            "takeaway_hint": "the vanishing point",
        }
        worker = _worker(
            db=db,
            mem=_mem(hobby_max_advances=1),
            ollama=_FakeOllama(proposal=proposal),
        )
        _write_hobby(db, advances=1, progress=1)
        r = worker.run()
        self.assertTrue(r.get("rotated"))
        state = load_hobby(db.kv_get)
        assert state is not None
        self.assertEqual(state["kind"], "making")
        self.assertEqual(state["artifact"], "the skyline from the window")
        self.assertEqual(state["progress"], 0)
        history = load_hobby_history(db.kv_get)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["artifact"], "The Glasshouse Letters")
        self.assertNotIn("The Glasshouse Letters", state["label"])

    def test_seed_fallback_after_reading_wrap_is_a_different_kind(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db, mem=_mem(hobby_max_advances=1), ollama=None)
        _write_hobby(db, advances=1, progress=4)
        r = worker.run()
        self.assertTrue(r.get("rotated"))
        state = load_hobby(db.kv_get)
        assert state is not None
        self.assertNotEqual(state["kind"], "reading")
        self.assertTrue(state.get("artifact"))
        self.assertFalse(hobby_mod.is_genre_artifact(str(state["artifact"])))

    def test_junk_llm_proposal_falls_back_to_a_named_seed(self) -> None:
        db = _FakeChatDb()
        worker = _worker(
            db=db,
            mem=_mem(hobby_max_advances=1),
            ollama=_FakeOllama(proposal={"kind": "reading", "artifact": "a sci-fi book"}),
        )
        _write_hobby(db, advances=1, progress=1)
        r = worker.run()
        self.assertTrue(r.get("rotated"))
        state = load_hobby(db.kv_get)
        assert state is not None
        self.assertNotEqual(state["kind"], "reading")
        self.assertTrue(state.get("artifact"))

    def test_rotate_off_reading_does_not_wipe_the_paperback(self) -> None:
        db = _FakeChatDb()
        world = _FakeWorld()
        book = world.list_items()[0]
        self.assertEqual(book.state["progress"], 5)
        worker = _worker(
            db=db, world_store=world, mem=_mem(hobby_max_advances=1), ollama=None,
        )
        _write_hobby(db, advances=1, progress=4)
        worker.run()
        self.assertEqual(book.state["progress"], 5)
        self.assertEqual(book.state["title"], "The Glasshouse Letters")

    def test_invented_reading_stamps_a_new_title_and_resets_progress(self) -> None:
        db = _FakeChatDb()
        world = _FakeWorld()
        proposal = {
            "key": "eleven_doors",
            "kind": "reading",
            "unit": "chapter",
            "artifact": "Eleven Doors",
            "artifact_detail": "a twisty thriller where every chapter is a different room",
            "takeaway_hint": "which room she just walked into",
        }
        worker = _worker(
            db=db,
            world_store=world,
            mem=_mem(hobby_max_advances=1),
            ollama=_FakeOllama(proposal=proposal),
        )
        _write_hobby(
            db,
            key="sketchbook",
            kind="making",
            label="working on the skyline from the window",
            artifact="the skyline from the window",
            unit="sketch",
            advances=1,
            progress=3,
        )
        worker.run()
        book = world.list_items()[0]
        self.assertEqual(book.name, "Eleven Doors")
        self.assertEqual(book.state["title"], "Eleven Doors")
        self.assertEqual(book.state["progress"], 0)
        state = load_hobby(db.kv_get)
        assert state is not None
        self.assertEqual(state["artifact"], "Eleven Doors")

    def test_returning_to_the_same_title_keeps_chapters(self) -> None:
        db = _FakeChatDb()
        world = _FakeWorld()
        proposal = {
            "key": "scifi_series",
            "kind": "reading",
            "unit": "chapter",
            "artifact": "The Glasshouse Letters",
            "artifact_detail": "an epistolary novel about two botanists and a war",
            "takeaway_hint": "the next letter",
        }
        worker = _worker(
            db=db,
            world_store=world,
            mem=_mem(hobby_max_advances=1),
            ollama=_FakeOllama(proposal=proposal),
        )
        _write_hobby(
            db,
            key="sketchbook",
            kind="making",
            label="working on the skyline from the window",
            artifact="the skyline from the window",
            unit="sketch",
            advances=1,
            progress=3,
        )
        worker.run()
        book = world.list_items()[0]
        self.assertEqual(book.state["title"], "The Glasshouse Letters")
        self.assertEqual(book.state["progress"], 5)

    def test_start_binds_reading_to_the_room_book(self) -> None:
        db = _FakeChatDb()
        world = _FakeWorld([_FakeItem(
            name="The Quantum Garden",
            state={
                "title": "The Quantum Garden",
                "blurb": "a slow-burn sci-fi",
                "progress": 3,
                "total": 12,
                "status": "reading",
            },
        )])
        worker = _worker(db=db, world_store=world)
        tpl = hobby_mod.template_for("scifi_series")
        assert tpl is not None
        original = hobby_mod.pick_hobby
        hobby_mod.pick_hobby = lambda *_a, **_k: tpl  # type: ignore[assignment]
        try:
            worker.run()
        finally:
            hobby_mod.pick_hobby = original  # type: ignore[assignment]
        state = load_hobby(db.kv_get)
        assert state is not None
        self.assertEqual(state["kind"], "reading")
        self.assertEqual(state["artifact"], "The Quantum Garden")
        self.assertEqual(world.list_items()[0].state["progress"], 3)

    def test_history_is_not_a_second_standing_hobby(self) -> None:
        db = _FakeChatDb()
        worker = _worker(
            db=db, mem=_mem(hobby_max_advances=1), ollama=_FakeOllama(),
        )
        _write_hobby(db, advances=1, progress=1)
        worker.run()
        history = load_hobby_history(db.kv_get)
        self.assertEqual(len(history), 1)
        current = load_hobby(db.kv_get)
        assert current is not None
        self.assertNotEqual(current.get("artifact"), history[0].get("artifact"))
        self.assertEqual(current.get("kind"), "making")


class HobbyDemandTests(unittest.TestCase):
    """The P44 probe: which transition is due, and will it cost a GPU."""

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_cold_install_wants_to_start(self) -> None:
        db = _FakeChatDb()
        worker = _worker(db=db)
        signal = worker.demand(now=self._now(), last_run_at=None)
        self.assertEqual(signal.reason, "start")
        self.assertEqual(signal.pressure, 1.0)
        self.assertFalse(signal.needs_llm)

    def test_pacing_floor_vetoes(self) -> None:
        """Between advances there is nothing a run could accomplish."""
        db = _FakeChatDb()
        worker = _worker(db=db)
        worker.run()  # start
        worker.run()  # first advance
        now = self._now()
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "pacing")

    def test_needs_llm_only_when_a_seed_is_composed(self) -> None:
        """An ordinary advance is a kv write; a rotation writes prose."""
        db = _FakeChatDb()
        worker = _worker(db=db, mem=_mem(hobby_max_advances=2),
                         ollama=_FakeOllama())
        worker.run()  # start
        worker._force_advance = True
        worker.run()
        worker._force_advance = True
        worker.run()  # advances==2 → a rotation is now due

        signal = worker.demand(now=self._now(), last_run_at=None)
        self.assertEqual(signal.reason, "rotate")
        self.assertTrue(signal.needs_llm)

    def test_probe_does_not_consume_force_flags(self) -> None:
        """Spending a one-shot on a probe would lose the forced run."""
        db = _FakeChatDb()
        worker = _worker(db=db)
        worker.run()  # start
        worker.run()  # advance, so pacing now blocks
        worker._force_advance = True

        signal = worker.demand(now=self._now(), last_run_at=None)
        self.assertEqual(signal.reason, "advance")
        self.assertTrue(worker._force_advance)

        self.assertTrue(worker.run().get("advanced"))
        self.assertFalse(worker._force_advance)

    def test_probe_never_starts_a_hobby(self) -> None:
        """``_start_hobby`` rolls the RNG and writes — run-only work."""
        db = _FakeChatDb()
        worker = _worker(db=db)
        worker.demand(now=self._now(), last_run_at=None)
        worker.is_ready(now=self._now(), last_run_at=None)
        self.assertEqual(db.store, {})
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
        world_store: Any = None,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent_settings or _agent())
        self._chat_db = _FakeChatDb()
        self._world_store = world_store
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

    def test_reading_hobby_names_the_room_book_not_the_genre(self) -> None:
        host = _Host(
            state={
                "label": "working through a sci-fi series",
                "kind": "reading",
                "progress": 99,
                "unit": "chapter",
                "artifact": "The Glasshouse Letters",
            },
            world_store=_FakeWorld(),
        )
        out = host._render_hobby_block()
        self.assertIn("The Glasshouse Letters", out)
        self.assertNotIn("sci-fi series", out)
        self.assertIn("5 chapters in", out)

    def test_handling_section_is_registered(self) -> None:
        from app.core.session.prompt_assembler import PromptAssembler
        from app.core.session.prompt_support import HANDLING_SECTIONS

        self.assertEqual(
            HANDLING_SECTIONS["hobby_block"],
            ("What you've been up to lately:",),
        )
        names = set(PromptAssembler.assemble_with_budget.__code__.co_varnames)
        self.assertIn("hobby_block", names)

    def test_hobby_block_is_not_a_steer(self) -> None:
        from app.core.conversation.stance import _OFFERS

        offered = {name for names in _OFFERS.values() for name in names}
        self.assertNotIn("hobby_block", offered)


if __name__ == "__main__":
    unittest.main()

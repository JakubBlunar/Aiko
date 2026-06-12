"""Tests for the K52 wants ledger — pure math, feeder worker, and
prompt-assembler slot wiring.

The pure module (``app/core/conversation/wants_ledger.py``) carries
all lifecycle math; the worker tests use tiny in-memory fakes for the
kv store / memory store / goal store; the slot tests mirror the K15
``VulnerabilityBudgetProviderSlotTests`` shape.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.conversation import wants_ledger as wl
from app.core.conversation.wants_ledger_worker import WantsLedgerWorker


_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _state_with(*wants: wl.Want) -> wl.LedgerState:
    return wl.LedgerState(wants=tuple(wants))


def _want(
    text: str = "ask Jacob about the espresso machine",
    *,
    pressure: float = 0.2,
    created_at: datetime = _NOW,
    source_ref: str = "manual:x",
) -> wl.Want:
    iso = created_at.isoformat()
    return wl.Want(
        id="w" + source_ref[-4:],
        text=text,
        kind="ask",
        source="manual",
        source_ref=source_ref,
        created_at=iso,
        pressure=pressure,
        last_growth_at=iso,
    )


class SerializationTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        state, added = wl.add_want(
            wl.LedgerState(),
            text="tell Jacob about the novel you read",
            kind="share",
            source="manual",
            source_ref="manual:1",
            now=_NOW,
        )
        self.assertTrue(added)
        state = wl.mark_acted(
            wl.add_want(
                state,
                text="ask about the garden project",
                kind="ask",
                source="manual",
                source_ref="manual:2",
                now=_NOW,
            )[0],
            state.wants[0].id if state.wants else "",
            _NOW,
        )
        blob = wl.serialize(state)
        back = wl.deserialize(blob)
        self.assertEqual(len(back.wants), len(state.wants))
        self.assertEqual(back.acted_map(), state.acted_map())

    def test_corrupt_input_returns_empty(self) -> None:
        self.assertEqual(wl.deserialize(None).wants, ())
        self.assertEqual(wl.deserialize("").wants, ())
        self.assertEqual(wl.deserialize("not json").wants, ())
        self.assertEqual(wl.deserialize("[1,2]").wants, ())

    def test_deserialize_clamps_pressure(self) -> None:
        blob = json.dumps({
            "wants": [{
                "id": "a", "text": "x y z", "kind": "ask",
                "source": "manual", "source_ref": "m:1",
                "created_at": _NOW.isoformat(),
                "pressure": 7.5, "last_growth_at": _NOW.isoformat(),
            }],
            "recently_acted": {},
        })
        state = wl.deserialize(blob)
        self.assertEqual(state.wants[0].pressure, 1.0)


class AddWantTests(unittest.TestCase):
    def test_adds(self) -> None:
        state, added = wl.add_want(
            wl.LedgerState(),
            text="ask Jacob about the marathon training",
            kind="ask", source="manual", source_ref="m:1", now=_NOW,
        )
        self.assertTrue(added)
        self.assertEqual(len(state.wants), 1)
        self.assertEqual(state.wants[0].pressure, 0.15)

    def test_refuses_empty_text(self) -> None:
        _, added = wl.add_want(
            wl.LedgerState(),
            text="   ", kind="ask", source="manual",
            source_ref="m:1", now=_NOW,
        )
        self.assertFalse(added)

    def test_refuses_duplicate_source_ref(self) -> None:
        state = _state_with(_want(source_ref="seed:5"))
        _, added = wl.add_want(
            state,
            text="completely different topic entirely",
            kind="ask", source="curiosity_seed",
            source_ref="seed:5", now=_NOW,
        )
        self.assertFalse(added)

    def test_refuses_recently_acted_ref(self) -> None:
        state = wl.LedgerState(
            recently_acted=(("seed:5", _NOW.isoformat()),),
        )
        _, added = wl.add_want(
            state,
            text="ask about the violin lessons",
            kind="ask", source="curiosity_seed",
            source_ref="seed:5", now=_NOW,
        )
        self.assertFalse(added)

    def test_refuses_overlapping_text(self) -> None:
        state = _state_with(
            _want("ask Jacob about the espresso machine delivery"),
        )
        _, added = wl.add_want(
            state,
            text="ask whether the espresso machine delivery arrived",
            kind="ask", source="manual", source_ref="m:2", now=_NOW,
        )
        self.assertFalse(added)

    def test_refuses_at_cap(self) -> None:
        state = wl.LedgerState()
        topics = (
            "ask about the violin recital",
            "share the meteor shower story",
            "steer toward the jazz chord goal",
        )
        for i, topic in enumerate(topics):
            state, _ = wl.add_want(
                state,
                text=topic,
                kind="ask", source="manual",
                source_ref=f"m:{i}", now=_NOW,
            )
        self.assertEqual(len(state.wants), 3)
        _, added = wl.add_want(
            state,
            text="yet another brand new fresh idea",
            kind="ask", source="manual", source_ref="m:9",
            now=_NOW, cap=3,
        )
        self.assertFalse(added)

    def test_unknown_kind_normalises_to_ask(self) -> None:
        state, _ = wl.add_want(
            wl.LedgerState(),
            text="mention the meteor shower tonight",
            kind="demand", source="manual", source_ref="m:1", now=_NOW,
        )
        self.assertEqual(state.wants[0].kind, "ask")


class GrowthTests(unittest.TestCase):
    def test_pressure_grows_with_elapsed_days(self) -> None:
        state = _state_with(_want(pressure=0.15, created_at=_NOW))
        later = _NOW + timedelta(days=2)
        grown = wl.apply_growth(
            state, later,
            growth_per_day=0.25, max_age_days=14.0,
            reentry_cooldown_days=5.0,
        )
        self.assertAlmostEqual(grown.wants[0].pressure, 0.65, places=2)

    def test_pressure_clamps_at_one(self) -> None:
        state = _state_with(_want(pressure=0.9, created_at=_NOW))
        grown = wl.apply_growth(
            state, _NOW + timedelta(days=5),
            growth_per_day=0.25, max_age_days=14.0,
            reentry_cooldown_days=5.0,
        )
        self.assertEqual(grown.wants[0].pressure, 1.0)

    def test_stale_want_expires(self) -> None:
        state = _state_with(_want(created_at=_NOW - timedelta(days=20)))
        grown = wl.apply_growth(
            state, _NOW,
            growth_per_day=0.25, max_age_days=14.0,
            reentry_cooldown_days=5.0,
        )
        self.assertEqual(grown.wants, ())

    def test_cooldown_sweeps_after_window(self) -> None:
        state = wl.LedgerState(recently_acted=(
            ("old:1", (_NOW - timedelta(days=8)).isoformat()),
            ("new:1", (_NOW - timedelta(days=1)).isoformat()),
        ))
        grown = wl.apply_growth(
            state, _NOW,
            growth_per_day=0.25, max_age_days=14.0,
            reentry_cooldown_days=5.0,
        )
        self.assertEqual(grown.acted_map().keys(), {"new:1"})

    def test_no_growth_when_disabled(self) -> None:
        state = _state_with(_want(pressure=0.2))
        grown = wl.apply_growth(
            state, _NOW + timedelta(days=3),
            growth_per_day=0.0, max_age_days=14.0,
            reentry_cooldown_days=5.0,
        )
        self.assertEqual(grown.wants[0].pressure, 0.2)


class ActedTests(unittest.TestCase):
    def test_mark_acted_removes_and_cools(self) -> None:
        want = _want(source_ref="seed:7")
        state = _state_with(want)
        after = wl.mark_acted(state, want.id, _NOW)
        self.assertEqual(after.wants, ())
        self.assertIn("seed:7", after.acted_map())

    def test_mark_acted_unknown_id_noop(self) -> None:
        state = _state_with(_want())
        after = wl.mark_acted(state, "nope", _NOW)
        self.assertEqual(len(after.wants), 1)

    def test_detect_acted_on_overlap(self) -> None:
        want = _want("ask Jacob about the espresso machine delivery")
        state = _state_with(want)
        hits = wl.detect_acted(
            state,
            "oh hey -- did that espresso machine delivery ever show up?",
        )
        self.assertEqual(hits, [want.id])

    def test_detect_acted_short_want_adapts_threshold(self) -> None:
        # Only two content words ("violin", "lessons") -- the required
        # overlap clamps down to 2 so the want can still match.
        want = _want("ask about the violin lessons")
        state = _state_with(want)
        hits = wl.detect_acted(state, "how are the violin lessons going?")
        self.assertEqual(hits, [want.id])

    def test_detect_acted_no_overlap_silent(self) -> None:
        state = _state_with(_want("ask about the espresso machine"))
        self.assertEqual(
            wl.detect_acted(state, "the weather is nice today"), [],
        )


class RenderTests(unittest.TestCase):
    def test_empty_ledger_silent(self) -> None:
        self.assertEqual(wl.render_block(wl.LedgerState(), _NOW), "")

    def test_soft_band_lists_wants(self) -> None:
        state = _state_with(
            _want("ask about the garden", pressure=0.3, source_ref="a:1"),
            _want("share the novel thing", pressure=0.2, source_ref="b:2"),
        )
        block = wl.render_block(
            state, _NOW, user_display_name="Jacob",
            imperative_threshold=0.7,
        )
        self.assertIn("Things you've been wanting", block)
        self.assertIn("ask about the garden", block)
        self.assertIn("Jacob", block)
        self.assertNotIn("THIS conversation", block)

    def test_imperative_band_single_directive(self) -> None:
        state = _state_with(
            _want(
                "ask about the garden",
                pressure=0.85,
                created_at=_NOW - timedelta(days=3),
            ),
            _want("share the novel thing", pressure=0.2, source_ref="b:2"),
        )
        block = wl.render_block(
            state, _NOW, user_display_name="Jacob",
            imperative_threshold=0.7,
        )
        self.assertIn("THIS conversation", block)
        self.assertIn("ask about the garden", block)
        self.assertIn("3 days", block)
        self.assertNotIn("share the novel thing", block)

    def test_soft_band_caps_at_two(self) -> None:
        state = _state_with(
            _want("topic alpha entirely", pressure=0.5, source_ref="a"),
            _want("topic bravo entirely", pressure=0.4, source_ref="b"),
            _want("topic charlie entirely", pressure=0.3, source_ref="c"),
        )
        block = wl.render_block(state, _NOW, imperative_threshold=0.9)
        self.assertIn("alpha", block)
        self.assertIn("bravo", block)
        self.assertNotIn("charlie", block)


# ── feeder worker ───────────────────────────────────────────────────


class _KvStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class _FakeMemory:
    """Duck-typed seed row + memory store."""

    class Row:
        def __init__(self, id: int, content: str, consumed: bool = False) -> None:
            self.id = id
            self.content = content
            self.metadata = {"topic": content} | (
                {"consumed_at": "x"} if consumed else {}
            )
            self.tier = "scratchpad"
            self.created_at = "2026-06-01T00:00:00+00:00"

    def __init__(self, rows: list["Row"]) -> None:
        self._rows = rows

    def iter_by_kind(self, kind: str) -> list["Row"]:
        assert kind == "curiosity_seed"
        return list(self._rows)


class _FakeGoals:
    class Row:
        def __init__(self, id: int, content: str) -> None:
            self.id = id
            self.content = content

    def __init__(self, rows: list["Row"]) -> None:
        self._rows = rows

    def list_active(self) -> list["Row"]:
        return list(self._rows)


class WorkerTests(unittest.TestCase):
    def _worker(self, kv: _KvStore, **kwargs) -> WantsLedgerWorker:
        return WantsLedgerWorker(
            kv_get=kv.get,
            kv_set=kv.set,
            user_display_name_provider=lambda: "Jacob",
            **kwargs,
        )

    def test_ingests_seeds_and_goals(self) -> None:
        kv = _KvStore()
        worker = self._worker(
            kv,
            memory_store=_FakeMemory([
                _FakeMemory.Row(1, "whether he plays an instrument"),
            ]),
            goal_store=_FakeGoals([
                _FakeGoals.Row(9, "Learn to recognise jazz chords"),
            ]),
        )
        stats = worker.run()
        self.assertEqual(stats["added"], 2)
        state = wl.deserialize(kv.get(wl.KV_WANTS_LEDGER))
        refs = {w.source_ref for w in state.wants}
        self.assertEqual(refs, {"seed:1", "goal:9"})

    def test_second_run_dedupes(self) -> None:
        kv = _KvStore()
        worker = self._worker(
            kv,
            memory_store=_FakeMemory([
                _FakeMemory.Row(1, "whether he plays an instrument"),
            ]),
        )
        worker.run()
        stats = worker.run()
        self.assertEqual(stats["added"], 0)
        state = wl.deserialize(kv.get(wl.KV_WANTS_LEDGER))
        self.assertEqual(len(state.wants), 1)

    def test_skips_consumed_seeds(self) -> None:
        kv = _KvStore()
        worker = self._worker(
            kv,
            memory_store=_FakeMemory([
                _FakeMemory.Row(1, "old topic", consumed=True),
            ]),
        )
        stats = worker.run()
        self.assertEqual(stats["added"], 0)

    def test_ingests_forward_curiosity_ring(self) -> None:
        kv = _KvStore()
        from app.core.proactive.forward_curiosity_worker import (
            FORWARD_CURIOSITY_JOURNAL_KEY,
        )

        kv.set(FORWARD_CURIOSITY_JOURNAL_KEY, json.dumps([
            {
                "at": "2026-06-10T10:00:00",
                "question": "how the marathon training is going",
                "source": "memory",
                "source_id": "mem:4",
            },
        ]))
        worker = self._worker(kv)
        stats = worker.run()
        self.assertEqual(stats["added"], 1)
        state = wl.deserialize(kv.get(wl.KV_WANTS_LEDGER))
        self.assertEqual(state.wants[0].source_ref, "fc:mem:4")
        self.assertIn("Jacob", state.wants[0].text)

    def test_disabled_skips(self) -> None:
        kv = _KvStore()
        worker = self._worker(kv, enabled_provider=lambda: False)
        stats = worker.run()
        self.assertTrue(stats.get("disabled"))

    def test_growth_applies_each_tick(self) -> None:
        kv = _KvStore()
        old = _NOW - timedelta(days=2)
        kv.set(wl.KV_WANTS_LEDGER, wl.serialize(
            _state_with(_want(pressure=0.15, created_at=old)),
        ))
        worker = self._worker(kv, growth_per_day=0.25)
        worker.run()
        state = wl.deserialize(kv.get(wl.KV_WANTS_LEDGER))
        self.assertGreater(state.wants[0].pressure, 0.5)


# ── assembler slot ──────────────────────────────────────────────────


class WantsProviderSlotTests(unittest.TestCase):
    """K52 wants block lands in the system prompt, leads the
    "things on Aiko's mind" cluster (before curiosity seeds), and is
    dropped under ``aggressive=True`` alongside the seeds."""

    _CUE = (
        "Something you've been wanting: ask Jacob about the garden -- "
        "this has been on your mind for about 3 days."
    )

    def _assemble(self, *, aggressive: bool = False, **providers):
        from app.core.infra.chat_database import ChatDatabase
        from app.core.session.prompt_assembler import PromptAssembler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        persona = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        persona.write("P")
        persona.close()
        assembler = PromptAssembler(
            db, persona_path=Path(persona.name), recent_window=20,
        )
        db.add_message(
            session_id="w1", role="user", content="hi", token_count=2,
        )
        assembler.set_inner_life_providers(**providers)
        messages, _ = assembler.assemble_with_budget(
            "w1", "hello",
            context_window=4096, response_budget=256,
            aggressive=aggressive,
        )
        return messages[0]["content"]

    def test_block_lands_in_system_prompt(self) -> None:
        content = self._assemble(wants=lambda: self._CUE)
        self.assertIn(self._CUE, content)

    def test_block_precedes_curiosity_seeds(self) -> None:
        seeds = "Quiet curiosity (only if a soft pivot lands naturally):"
        content = self._assemble(
            wants=lambda: self._CUE,
            curiosity_seeds=lambda: seeds,
        )
        self.assertLess(content.index(self._CUE), content.index(seeds))

    def test_dropped_under_aggressive(self) -> None:
        content = self._assemble(wants=lambda: self._CUE, aggressive=True)
        self.assertNotIn(self._CUE, content)


if __name__ == "__main__":
    unittest.main()

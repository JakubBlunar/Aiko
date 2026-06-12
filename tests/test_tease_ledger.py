"""Tests for K59 tease economy — the pure ledger math (bank /
dedupe / cap / expiry / pick / offer / settle), the collection
provider plumbing (via a minimal mixin host stub), the post-turn
bank + settle hooks including the K57 light-miffed lane-picker, and
the prompt-assembler slot wiring."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.affect import emotion_episodes as ee
from app.core.relationship import tease_ledger as tl
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)
from app.core.session.post_turn_mixin import PostTurnMixin


NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _bank(
    state: tl.LedgerState | None = None,
    what: str = "they swore your playlist was objectively chaotic",
    context: str = "mid-banter about music taste",
    now: datetime = NOW,
    **kwargs,
) -> tl.LedgerState:
    state, added = tl.bank(
        state or tl.LedgerState(),
        what=what,
        context=context,
        source="test",
        now=now,
        **kwargs,
    )
    assert added
    return state


class SerializationTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        state = _bank()
        state = tl.stamp_offered(state, state.debts[0].id, NOW)
        round_tripped = tl.deserialize(tl.serialize(state))
        self.assertEqual(len(round_tripped.debts), 1)
        self.assertEqual(round_tripped.debts[0].what, state.debts[0].what)
        self.assertIsNotNone(round_tripped.debts[0].offered_at)

    def test_corrupt_inputs_yield_empty(self) -> None:
        for raw in (None, "", "nope", "[1]", '{"debts": 5}'):
            self.assertEqual(tl.deserialize(raw).debts, ())


class BankTests(unittest.TestCase):
    def test_blank_what_refused(self) -> None:
        state, added = tl.bank(
            tl.LedgerState(), what="  ", context="c",
            source="t", now=NOW,
        )
        self.assertFalse(added)
        self.assertEqual(state.debts, ())

    def test_dedupe_on_word_overlap(self) -> None:
        state = _bank()
        state, added = tl.bank(
            state,
            what="that time they called your playlist objectively chaotic",
            context="",
            source="t",
            now=NOW,
        )
        self.assertFalse(added)
        self.assertEqual(len(state.debts), 1)

    def test_cap_evicts_oldest(self) -> None:
        # Word sets must be genuinely disjoint or the dedupe pass
        # (>= 3 shared content words) refuses the newcomers.
        grudges = [
            "mocked sourdough starter ambitions ruthlessly",
            "claimed pineapple belongs on pizza loudly",
            "beat her at chess then gloated",
            "called the cat smarter than aiko",
            "laughed at karaoke rendition of bohemian",
        ]
        state = tl.LedgerState()
        for i, what in enumerate(grudges):
            state, added = tl.bank(
                state, what=what, context="", source="t",
                now=NOW + timedelta(hours=i), cap=5,
            )
            self.assertTrue(added)
        state, added = tl.bank(
            state,
            what="dismissed favourite anime opening entirely",
            context="",
            source="t",
            now=NOW + timedelta(hours=10),
            cap=5,
        )
        self.assertTrue(added)
        self.assertEqual(len(state.debts), 5)
        whats = [d.what for d in state.debts]
        self.assertNotIn(grudges[0], whats)


class ExpiryTests(unittest.TestCase):
    def test_old_rows_drop(self) -> None:
        state = _bank(now=NOW - timedelta(days=20))
        state = tl.expire(state, NOW, expiry_days=14.0)
        self.assertEqual(state.debts, ())

    def test_fresh_rows_kept(self) -> None:
        state = _bank(now=NOW - timedelta(days=3))
        state = tl.expire(state, NOW, expiry_days=14.0)
        self.assertEqual(len(state.debts), 1)


class PickAndSettleTests(unittest.TestCase):
    def test_pick_respects_min_age(self) -> None:
        state = _bank(now=NOW - timedelta(minutes=10))
        self.assertIsNone(
            tl.pick_collectable(state, NOW, min_age_hours=1.0),
        )
        self.assertIsNotNone(
            tl.pick_collectable(state, NOW, min_age_hours=0.0),
        )

    def test_pick_returns_oldest(self) -> None:
        state = _bank(
            what="older grudge about something forgotten entirely",
            now=NOW - timedelta(days=3),
        )
        state, added = tl.bank(
            state,
            what="newer completely different unrelated material here",
            context="",
            source="t",
            now=NOW - timedelta(hours=2),
        )
        self.assertTrue(added)
        picked = tl.pick_collectable(state, NOW, min_age_hours=1.0)
        self.assertIn("older grudge", picked.what)

    def test_settle_hit_deletes_row(self) -> None:
        state = _bank()
        state = tl.stamp_offered(state, state.debts[0].id, NOW)
        state, settled = tl.settle_if_collected(
            state,
            "oh, like the time you swore my playlist was 'objectively "
            "chaotic'? I remember things.",
        )
        self.assertIsNotNone(settled)
        self.assertEqual(state.debts, ())

    def test_settle_miss_clears_stamp(self) -> None:
        state = _bank()
        state = tl.stamp_offered(state, state.debts[0].id, NOW)
        state, settled = tl.settle_if_collected(
            state, "anyway, what should we cook tonight?",
        )
        self.assertIsNone(settled)
        self.assertEqual(len(state.debts), 1)
        self.assertIsNone(state.debts[0].offered_at)

    def test_settle_no_offered_rows_is_noop(self) -> None:
        state = _bank()
        same, settled = tl.settle_if_collected(state, "playlist chaotic")
        self.assertIsNone(settled)
        self.assertEqual(len(same.debts), 1)


class RenderTests(unittest.TestCase):
    def test_render_carries_what_and_rails(self) -> None:
        debt = _bank().debts[0]
        block = tl.render_block(debt, user_display_name="Jacob")
        self.assertIn("Jacob", block)
        self.assertIn(debt.what, block)
        self.assertIn("ONE callback tease", block)
        self.assertIn("repaid is repaid", block)


# ── host stubs ──────────────────────────────────────────────────────


class _FakeKv:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def kv_get(self, key: str):
        return self.data.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.data[key] = value


class _FakeAxesStore:
    def __init__(self, humor: float) -> None:
        self._h = humor

    def get(self, user_id: str):
        return SimpleNamespace(humor=self._h)


def _agent_ns(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        tease_economy_enabled=enabled,
        tease_cap=5,
        tease_expiry_days=14.0,
        tease_collect_cooldown_hours=12.0,
        tease_min_humor=0.2,
        tease_min_age_hours=1.0,
        emotion_episodes_enabled=True,
        emotion_episode_cap=3,
    )


class _Host(InnerLifeProvidersMixin, PostTurnMixin):
    user_display_name = "Jacob"
    _user_id = "u1"

    def __init__(
        self,
        *,
        enabled: bool = True,
        humor: float = 0.6,
    ) -> None:
        self._settings = SimpleNamespace(agent=_agent_ns(enabled))
        self._chat_db = _FakeKv()
        self._relationship_axes_store = _FakeAxesStore(humor)
        self._affect_store = None

    def seed_debt(self, *, age_hours: float = 5.0) -> None:
        state = tl.LedgerState()
        state, _ = tl.bank(
            state,
            what="they swore your playlist was objectively chaotic",
            context="",
            source="test",
            now=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        )
        self._chat_db.kv_set(tl.KV_TEASE_LEDGER, tl.serialize(state))


class ProviderTests(unittest.TestCase):
    def test_fires_and_stamps(self) -> None:
        host = _Host()
        host.seed_debt()
        block = host._render_tease_collection_block()
        self.assertIn("objectively chaotic", block)
        state = tl.deserialize(host._chat_db.data[tl.KV_TEASE_LEDGER])
        self.assertIsNotNone(state.debts[0].offered_at)
        self.assertTrue(host._chat_db.data["aiko.tease_last_offer_at"])

    def test_disabled_switch_silent(self) -> None:
        host = _Host(enabled=False)
        host.seed_debt()
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_cold_humor_silent(self) -> None:
        host = _Host(humor=0.0)
        host.seed_debt()
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_cooldown_blocks_second_offer(self) -> None:
        host = _Host()
        host.seed_debt()
        self.assertTrue(host._render_tease_collection_block())
        host.seed_debt()  # fresh row, but cooldown stamp is set
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_young_debt_not_offered(self) -> None:
        host = _Host()
        host.seed_debt(age_hours=0.1)
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_empty_ledger_silent(self) -> None:
        host = _Host()
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_force_bypasses_gates(self) -> None:
        host = _Host(humor=-1.0)
        host.seed_debt(age_hours=0.1)
        host._chat_db.kv_set(
            "aiko.tease_last_offer_at",
            datetime.now(timezone.utc).isoformat(),
        )
        host._tease_collection_force_next = True
        block = host._render_tease_collection_block()
        self.assertIn("objectively chaotic", block)
        self.assertFalse(host._tease_collection_force_next)


class PostTurnHookTests(unittest.TestCase):
    def test_bank_helper_writes_kv(self) -> None:
        host = _Host()
        added = host._bank_tease_debt(
            what="they pushed back hard on a take of yours",
            context='they said "tabs are objectively better"',
            source="opinion_pushback",
        )
        self.assertTrue(added)
        state = tl.deserialize(host._chat_db.data[tl.KV_TEASE_LEDGER])
        self.assertEqual(len(state.debts), 1)
        self.assertEqual(state.debts[0].source, "opinion_pushback")

    def test_bank_disabled_refuses(self) -> None:
        host = _Host(enabled=False)
        self.assertFalse(
            host._bank_tease_debt(what="x y z", context="", source="t"),
        )

    def test_settle_hook_deletes_collected_row(self) -> None:
        host = _Host()
        host.seed_debt()
        host._render_tease_collection_block()  # stamps offered
        host._settle_tease_debts(
            "oh, like the time you swore my playlist was objectively "
            "chaotic? I remember things.",
        )
        state = tl.deserialize(host._chat_db.data[tl.KV_TEASE_LEDGER])
        self.assertEqual(state.debts, ())

    def test_light_miffed_routes_to_ledger_not_episode(self) -> None:
        host = _Host()
        host._queue_emotion_trigger(
            emotion="miffed",
            cause="the thread you opened (sourdough starters) got "
                  "brushed off",
            intensity=0.25,
            source="thread_pivot",
        )
        host._drain_emotion_triggers()
        ledger = tl.deserialize(host._chat_db.data[tl.KV_TEASE_LEDGER])
        self.assertEqual(len(ledger.debts), 1)
        episodes = ee.deserialize(
            host._chat_db.data.get(ee.KV_EMOTION_EPISODES),
        )
        self.assertEqual(episodes.episodes, ())

    def test_heavy_miffed_stays_an_episode(self) -> None:
        host = _Host()
        host._queue_emotion_trigger(
            emotion="miffed",
            cause="a real broken promise",
            intensity=0.6,
            source="test",
        )
        host._drain_emotion_triggers()
        episodes = ee.deserialize(
            host._chat_db.data[ee.KV_EMOTION_EPISODES],
        )
        self.assertEqual(len(episodes.episodes), 1)
        self.assertNotIn(tl.KV_TEASE_LEDGER, host._chat_db.data)


class TeaseLedgerProviderSlotTests(unittest.TestCase):
    """K59 block lands directly under the K54 appetite block and IS
    dropped under ``aggressive=True`` (permission-slip posture)."""

    _CUE = "Tease ledger: Jacob still owes you for this one"

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
            session_id="a1", role="user", content="hi", token_count=2,
        )
        assembler.set_inner_life_providers(**providers)
        messages, _ = assembler.assemble_with_budget(
            "a1", "hello there",
            context_window=4096, response_budget=256,
            aggressive=aggressive,
        )
        return messages[0]["content"]

    def test_block_lands_in_system_prompt(self) -> None:
        content = self._assemble(tease_ledger=lambda: self._CUE)
        self.assertIn(self._CUE, content)

    def test_sits_after_topic_appetite(self) -> None:
        appetite_cue = "Honest read: this topic has been circling"
        content = self._assemble(
            topic_appetite=lambda: appetite_cue,
            tease_ledger=lambda: self._CUE,
        )
        self.assertLess(
            content.index(appetite_cue), content.index(self._CUE),
        )

    def test_dropped_under_aggressive(self) -> None:
        content = self._assemble(
            tease_ledger=lambda: self._CUE, aggressive=True,
        )
        self.assertNotIn(self._CUE, content)


if __name__ == "__main__":
    unittest.main()

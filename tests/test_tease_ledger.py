"""Tests for K59 tease economy — the mock-grudge ledger.

The ledger is cue-pool rows now (``cue_type="tease_ledger"``), which
deleted most of what this file used to cover: expiry, capping, the
offer stamp and the collection match are the pool's, tested once in
``test_cue_pool_consumption.py`` for every type at once.

What is left is what is still K59's own:

* The pure module — how a debt is named (:func:`subject_for`, load-
  bearing because ``what`` is a constant on the K29 lane) and when two
  grudges are the same one (:func:`is_duplicate`).
* Banking, through a mixin host with a real :class:`CueStore`: the
  near-duplicate refusal, and the hour the row spends sealed so a debt
  cannot be collected in the sitting it was banked.
* The collection provider: the two gates that stayed out of the policy
  (humor floor, J11-tilted cooldown) and the oldest-first pick, which
  is the one place the pool's own ordering is wrong for a cue.
* The K57 lane-picker, which routes a light miffed here instead of
  spawning a sulk.
* The prompt-assembler slot.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.core.affect import emotion_episodes as ee
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.relationship import tease_ledger as tl
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)
from app.core.session.post_turn_mixin import PostTurnMixin


CHAOTIC = "your playlist is objectively chaotic"


# ── Pure: naming a debt ──────────────────────────────────────────────


class SubjectTests(unittest.TestCase):
    """``what`` is generic on the K29 lane, so the quote has to win."""

    def test_the_quote_wins_over_the_generic_what(self) -> None:
        self.assertEqual(
            tl.subject_for(
                what="they pushed back hard on a take of yours",
                context=CHAOTIC,
            ),
            CHAOTIC,
        )

    def test_what_is_used_when_there_is_no_context(self) -> None:
        """The K57 lane, where ``what`` is the trigger's own cause."""
        self.assertEqual(
            tl.subject_for(what="the sourdough thread got brushed off",
                           context=""),
            "the sourdough thread got brushed off",
        )

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(
            tl.subject_for(what="", context="  tabs   are\nbetter "),
            "tabs are better",
        )

    def test_nothing_in_nothing_out(self) -> None:
        self.assertEqual(tl.subject_for(what="", context=""), "")


class DuplicateTests(unittest.TestCase):
    def test_a_reworded_grudge_is_the_same_grudge(self) -> None:
        self.assertTrue(
            tl.is_duplicate(
                "that playlist of yours is objectively chaotic",
                {CHAOTIC},
            )
        )

    def test_a_different_grudge_gets_in(self) -> None:
        self.assertFalse(
            tl.is_duplicate("tabs beat spaces every time", {CHAOTIC})
        )

    def test_an_empty_shelf_refuses_nothing(self) -> None:
        self.assertFalse(tl.is_duplicate(CHAOTIC, set()))

    def test_a_subject_with_no_content_words_is_not_a_duplicate(self) -> None:
        """Otherwise "he did" would collide with everything."""
        self.assertFalse(tl.is_duplicate("he did", {CHAOTIC}))


class RenderTests(unittest.TestCase):
    def test_the_cue_line_carries_the_name_and_the_grudge(self) -> None:
        block = tl.render_block(
            what="they swore your playlist was objectively chaotic",
            context="mid-banter about music taste",
            user_display_name="Jacob",
        )
        self.assertIn("Jacob", block)
        self.assertIn("objectively chaotic", block)
        self.assertIn("mid-banter", block)

    def test_the_rails_are_not_in_the_cue_line(self) -> None:
        """They live in the hoisted handling note now, and shipping
        them twice would be the note arriving in duplicate."""
        block = tl.render_block(what="x", user_display_name="J")
        self.assertNotIn("callback tease", block)
        self.assertNotIn("needling", block)


# ── host stub ────────────────────────────────────────────────────────


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


def _agent_ns(enabled: bool = True, **kw) -> SimpleNamespace:
    return SimpleNamespace(
        tease_economy_enabled=enabled,
        tease_collect_cooldown_hours=kw.get("cooldown", 12.0),
        tease_min_humor=0.2,
        tease_min_age_hours=kw.get("min_age", 1.0),
        emotion_episodes_enabled=True,
        emotion_episode_cap=3,
    )


class _Host(InnerLifeProvidersMixin, PostTurnMixin, CuePoolMixin):
    user_display_name = "Jacob"
    _user_id = "u1"

    def __init__(
        self,
        store: CueStore,
        *,
        enabled: bool = True,
        humor: float = 0.6,
        **agent_kw,
    ) -> None:
        self._settings = SimpleNamespace(agent=_agent_ns(enabled, **agent_kw))
        self._chat_db = _FakeKv()
        self._relationship_axes_store = _FakeAxesStore(humor)
        self._affect_store = None
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = None

    def bank(self, subject: str, *, source: str = "test") -> bool:
        return self._bank_tease_debt(
            what=f"they said {subject}", context="", source=source,
            subject=subject,
        )


class _PoolFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _host(self, **kw) -> _Host:
        # Zero min-age by default: the seal is covered on its own, and
        # every other test would otherwise have to backdate a row.
        kw.setdefault("min_age", 0.0)
        return _Host(self.store, **kw)

    def _rows(self):
        return self.store.list_for_user(cue_type="tease_ledger")

    def _ripen(self, subject: str, *, hours_ago: float) -> None:
        """Backdate a banked row so oldest-first has something to sort."""
        row = next(r for r in self._rows() if r.subject == subject)
        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        self.store._conn().execute(
            "UPDATE cue_pool SET created_at = ? WHERE id = ?",
            (when.isoformat(), row.id),
        )
        self.store._conn().commit()


class BankTests(_PoolFixture):
    def test_a_debt_lands_as_a_pending_row(self) -> None:
        self.assertTrue(self._host().bank(CHAOTIC, source="opinion_pushback"))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject, CHAOTIC)
        self.assertEqual(rows[0].state, "pending")
        self.assertEqual(rows[0].payload["source"], "opinion_pushback")
        self.assertIn("Jacob", rows[0].text)

    def test_the_quote_is_the_subject_not_the_generic_what(self) -> None:
        """Two pushbacks in a row must be two debts.

        Keyed on ``what`` they would share a subject, and ``add``
        supersedes on subject -- so the second would silently retire the
        first and the ledger could never hold more than one.
        """
        host = self._host()
        host._bank_tease_debt(
            what="they pushed back hard on a take of yours",
            context='they said "tabs beat spaces"',
            source="opinion_pushback",
            subject="tabs beat spaces",
        )
        host._bank_tease_debt(
            what="they pushed back hard on a take of yours",
            context=f'they said "{CHAOTIC}"',
            source="opinion_pushback",
            subject=CHAOTIC,
        )
        live = [r for r in self._rows() if r.state == "pending"]
        self.assertEqual(len(live), 2)

    def test_a_reworded_grudge_is_refused(self) -> None:
        host = self._host()
        self.assertTrue(host.bank(CHAOTIC))
        self.assertFalse(
            host.bank("that playlist of yours is objectively chaotic")
        )
        self.assertEqual(len(self._rows()), 1)

    def test_a_collected_grudge_is_not_re_banked(self) -> None:
        """``recent_subjects`` spans terminal states, which is what
        makes "repaid is repaid" survive the row not being deleted."""
        host = self._host()
        host.bank(CHAOTIC)
        self.store.mark_used(self._rows()[0].id, evidence="test")
        self.assertFalse(host.bank(CHAOTIC))

    def test_a_fresh_debt_is_sealed_for_an_hour(self) -> None:
        """A debt banked and collected in one sitting is a comeback."""
        host = self._host(min_age=1.0)
        self.assertTrue(host.bank(CHAOTIC))
        self.assertEqual(self.store.count_pending("tease_ledger"), 0)
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_the_seal_lifts(self) -> None:
        host = self._host(min_age=1.0)
        host.bank(CHAOTIC)
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        self.assertEqual(
            len(self.store.pending("tease_ledger", now=later)), 1,
        )

    def test_the_master_switch_refuses(self) -> None:
        self.assertFalse(self._host(enabled=False).bank(CHAOTIC))
        self.assertEqual(self._rows(), [])

    def test_a_blank_grudge_refuses(self) -> None:
        host = self._host()
        self.assertFalse(
            host._bank_tease_debt(what="", context="", source="t")
        )


class CollectionTests(_PoolFixture):
    def test_it_offers_the_debt_and_marks_it_surfaced(self) -> None:
        host = self._host()
        host.bank(CHAOTIC)
        block = host._render_tease_collection_block()
        self.assertIn("objectively chaotic", block)
        self.assertEqual(self._rows()[0].state, "surfaced")
        self.assertEqual(self._rows()[0].surfaced_count, 1)

    def test_the_oldest_grudge_is_collected_first(self) -> None:
        """The gap is the joke, so this type overrides the pool's
        newest-first default through ``pick_order``."""
        host = self._host()
        host.bank(CHAOTIC)
        host.bank("tabs beat spaces")
        self._ripen(CHAOTIC, hours_ago=200.0)
        self.assertIn("chaotic", host._render_tease_collection_block())

    def test_an_empty_shelf_is_silent(self) -> None:
        self.assertEqual(self._host()._render_tease_collection_block(), "")

    def test_the_master_switch_is_silent(self) -> None:
        host = self._host(enabled=False)
        self._host().bank(CHAOTIC)
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_cold_humor_is_silent(self) -> None:
        host = self._host(humor=0.0)
        host.bank(CHAOTIC)
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_the_cooldown_blocks_a_second_offer(self) -> None:
        host = self._host()
        host.bank(CHAOTIC)
        self.assertTrue(host._render_tease_collection_block())
        host.bank("tabs beat spaces")
        self.assertEqual(host._render_tease_collection_block(), "")

    def test_the_cooldown_reads_the_pool_not_a_kv_stamp(self) -> None:
        """It used to keep ``aiko.tease_last_offer_at`` beside the
        ledger; the row's own ``last_surfaced_at`` is the same fact."""
        host = self._host()
        host.bank(CHAOTIC)
        host._render_tease_collection_block()
        self.assertNotIn("aiko.tease_last_offer_at", host._chat_db.data)
        self.assertIsNotNone(self.store.last_surfaced_at("tease_ledger"))

    def test_force_bypasses_humor_and_cooldown(self) -> None:
        host = self._host(humor=-1.0)
        host.bank(CHAOTIC)
        host._render_tease_collection_block()  # spends the cooldown
        host.debug_overrides.arm("tease_collection_force_next")
        self.assertIn(
            "chaotic", host._render_tease_collection_block(),
        )
        self.assertFalse(
            host.debug_overrides.peek("tease_collection_force_next")
        )


class LanePickerTests(_PoolFixture):
    """K57 routes a light miffed to comedy rather than to a sulk."""

    def test_light_miffed_banks_a_debt(self) -> None:
        host = self._host()
        host._queue_emotion_trigger(
            emotion="miffed",
            cause="the thread you opened (sourdough starters) got "
                  "brushed off",
            intensity=0.25,
            source="thread_pivot",
        )
        host._drain_emotion_triggers()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("sourdough", rows[0].subject)
        self.assertEqual(rows[0].payload["source"], "light_offence")
        episodes = ee.deserialize(
            host._chat_db.data.get(ee.KV_EMOTION_EPISODES),
        )
        self.assertEqual(episodes.episodes, ())

    def test_heavy_miffed_stays_an_episode(self) -> None:
        host = self._host()
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
        self.assertEqual(self._rows(), [])


class DiscardTests(_PoolFixture):
    def test_clearing_expires_rather_than_deletes(self) -> None:
        host = self._host()
        host.bank(CHAOTIC)
        self.assertEqual(host.discard_cues("tease_ledger"), 1)
        self.assertEqual(self._rows()[0].state, "expired")

    def test_a_cleared_grudge_does_not_come_back(self) -> None:
        host = self._host()
        host.bank(CHAOTIC)
        host.discard_cues("tease_ledger")
        self.assertFalse(host.bank(CHAOTIC))


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

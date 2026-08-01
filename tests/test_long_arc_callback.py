"""Tests for the K63 long-arc callback ("weeks ago you said…") feature.

Three layers:

* Pure module (:mod:`app.core.conversation.long_arc_callback`) — the
  ``select`` picker, the ``render_block`` cue copy, the don't-repeat ring
  helpers, the ``still_relevant`` retry gate, and the
  ``candidates_from_hits`` projection.
* Provider (:meth:`InnerLifePart3Mixin._render_long_arc_callback_block`)
  through a minimal mixin host with a fake retriever, a kv-backed
  chat_db and a real :class:`CueStore` — the cap / cadence / min-words
  gates, the don't-repeat ring, the master switch, the force-next
  bypass, and the surface-time ledger row that makes an ignored callback
  retryable instead of spent.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from app.core.conversation import long_arc_callback as lac
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import STATE_PENDING, CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


def _iso(days_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat(timespec="seconds")


def _cand(mid: int, *, cosine: float, age_days: float, content: str = "x") -> lac.AgedCandidate:
    return lac.AgedCandidate(
        memory_id=mid,
        content=content or "snippet",
        kind="fact",
        created_at=_iso(age_days),
        cosine=cosine,
        age_days=age_days,
    )


# ── Pure: select ─────────────────────────────────────────────────────


class SelectTests(unittest.TestCase):
    def test_picks_highest_cosine(self) -> None:
        pick = lac.select(
            [_cand(1, cosine=0.6, age_days=30), _cand(2, cosine=0.8, age_days=25)]
        )
        self.assertIsNotNone(pick)
        self.assertEqual(pick.memory_id, 2)

    def test_tie_breaks_to_oldest(self) -> None:
        pick = lac.select(
            [_cand(1, cosine=0.7, age_days=30), _cand(2, cosine=0.7, age_days=90)]
        )
        self.assertEqual(pick.memory_id, 2)

    def test_excludes_recent_ids(self) -> None:
        pick = lac.select(
            [_cand(1, cosine=0.9, age_days=30), _cand(2, cosine=0.6, age_days=30)],
            exclude_ids=[1],
        )
        self.assertEqual(pick.memory_id, 2)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(lac.select([]))
        self.assertIsNone(lac.select([_cand(1, cosine=0.9, age_days=30)], exclude_ids=[1]))

    def test_skips_blank_content(self) -> None:
        blank = lac.AgedCandidate(3, "  ", "fact", _iso(40), 0.95, 40.0)
        pick = lac.select([blank, _cand(1, cosine=0.5, age_days=30)])
        self.assertEqual(pick.memory_id, 1)


# ── Pure: render ─────────────────────────────────────────────────────


class RenderTests(unittest.TestCase):
    def test_contains_name_and_snippet(self) -> None:
        block = lac.render_block(
            _cand(1, cosine=0.7, age_days=30, content="your dad's old workshop"),
            user_display_name="Jacob",
        )
        self.assertIn("Jacob", block)
        self.assertIn("your dad's old workshop", block)
        self.assertIn("tentative", block.lower())

    def test_month_anchor_for_old_memory(self) -> None:
        # 100 days old -> a "back in <Month>" anchor is added.
        block = lac.render_block(
            _cand(1, cosine=0.7, age_days=100, content="z"),
            user_display_name="J",
        )
        self.assertIn("back in", block)

    def test_no_month_anchor_for_recent(self) -> None:
        block = lac.render_block(
            _cand(1, cosine=0.7, age_days=22, content="z"),
            user_display_name="J",
        )
        self.assertNotIn("back in", block)

    def test_blank_snippet_returns_empty(self) -> None:
        cand = lac.AgedCandidate(1, "   ", "fact", _iso(40), 0.7, 40.0)
        self.assertEqual(lac.render_block(cand), "")


# ── Pure: kv helpers ─────────────────────────────────────────────────


class _KV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class KvTests(unittest.TestCase):
    def test_recent_ids_ring_roundtrip_and_cap(self) -> None:
        kv = _KV()
        for i in range(lac.RECENT_IDS_MAX + 5):
            lac.append_recent_id(kv.get, kv.set, i, max_entries=lac.RECENT_IDS_MAX)
        ring = lac.load_recent_ids(kv.get)
        self.assertEqual(len(ring), lac.RECENT_IDS_MAX)
        # Oldest trimmed; newest kept.
        self.assertIn(lac.RECENT_IDS_MAX + 4, ring)
        self.assertNotIn(0, ring)

    def test_append_dedups(self) -> None:
        kv = _KV()
        lac.append_recent_id(kv.get, kv.set, 7)
        lac.append_recent_id(kv.get, kv.set, 7)
        self.assertEqual(lac.load_recent_ids(kv.get).count(7), 1)


# ── Pure: still_relevant (the retry gate) ────────────────────────────


class StillRelevantTests(unittest.TestCase):
    def test_same_thread_passes(self) -> None:
        payload = {"snippet": "his dad taught him woodworking in the garage"}
        self.assertTrue(
            lac.still_relevant(
                payload, "been doing woodworking out in the garage again"
            )
        )

    def test_different_topic_fails(self) -> None:
        payload = {"snippet": "his dad taught him woodworking in the garage"}
        self.assertFalse(
            lac.still_relevant(payload, "anyway the deployment finally went out")
        )

    def test_one_shared_word_is_not_enough(self) -> None:
        """A brushing mention is not the same thread.

        Two words rather than one because the failure this gate exists
        to prevent -- replaying an old memory at a conversation that has
        moved on -- is the user-visible one, while being too strict just
        leaves the callback where it already was.
        """
        payload = {"snippet": "his dad taught him woodworking in the garage"}
        self.assertFalse(
            lac.still_relevant(payload, "parked the car in the garage")
        )

    def test_missing_snippet_never_retries(self) -> None:
        self.assertFalse(lac.still_relevant({}, "woodworking with dad"))


# ── Pure: candidates_from_hits ───────────────────────────────────────


def _hit(
    mid: int, *, kind: str, age_days: float, score: float, content: str = "c",
) -> SimpleNamespace:
    return SimpleNamespace(
        score=score,
        record=SimpleNamespace(
            id=str(mid), kind=kind, created_at=_iso(age_days), content=content
        ),
    )


class CandidatesFromHitsTests(unittest.TestCase):
    def test_age_floor_filters(self) -> None:
        now = datetime.now(timezone.utc)
        hits = [
            _hit(1, kind="fact", age_days=10, score=0.7),
            _hit(2, kind="fact", age_days=30, score=0.7),
        ]
        cands = lac.candidates_from_hits(hits, now=now, min_age_days=21)
        self.assertEqual([c.memory_id for c in cands], [2])

    def test_kind_filter(self) -> None:
        now = datetime.now(timezone.utc)
        hits = [
            _hit(1, kind="self", age_days=40, score=0.7),
            _hit(2, kind="fact", age_days=40, score=0.7),
        ]
        cands = lac.candidates_from_hits(
            hits, now=now, min_age_days=21, allowed_kinds=lac.ALLOWED_KINDS
        )
        self.assertEqual([c.memory_id for c in cands], [2])

    def test_blank_and_bad_id_skipped(self) -> None:
        now = datetime.now(timezone.utc)
        bad = SimpleNamespace(
            score=0.9,
            record=SimpleNamespace(
                id="abc", kind="fact", created_at=_iso(40), content="c",
            ),
        )
        blank = _hit(2, kind="fact", age_days=40, score=0.7, content="  ")
        good = _hit(3, kind="fact", age_days=40, score=0.7)
        cands = lac.candidates_from_hits([bad, blank, good], now=now, min_age_days=21)
        self.assertEqual([c.memory_id for c in cands], [3])


# ── Provider ─────────────────────────────────────────────────────────


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(long_arc_callback_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _mem(**overrides: Any) -> SimpleNamespace:
    base = dict(
        long_arc_callback_min_age_days=21,
        long_arc_callback_min_cosine=0.55,
        long_arc_callback_per_session_cap=1,
        long_arc_callback_min_user_words=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeRetriever:
    def __init__(self, candidates: list[lac.AgedCandidate]) -> None:
        self._candidates = candidates
        self.calls = 0

    def aged_callback_candidate(self, query_text: str, **kwargs: Any) -> list:
        self.calls += 1
        return list(self._candidates)


class _Host(InnerLifeProvidersMixin, CuePoolMixin):
    def __init__(
        self,
        store: CueStore,
        *,
        candidates: list[lac.AgedCandidate] | None = None,
        agent: SimpleNamespace | None = None,
        mem: SimpleNamespace | None = None,
        session_count: int = 0,
        force_next: bool = False,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent or _agent())
        self._memory_settings = mem or _mem()
        self._rag_retriever = _FakeRetriever(
            candidates if candidates is not None else [_cand(1, cosine=0.7, age_days=40)]
        )
        kv = _KV()
        self._chat_db = SimpleNamespace(kv_get=kv.get, kv_set=kv.set)
        self._kv = kv
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = None
        self._long_arc_callback_session_count = session_count
        self.debug_overrides.arm("long_arc_callback_force_next", force_next)
        self._last_long_arc_callback: Any = None
        self.user_display_name = "Jacob"


LONG_MSG = "so tell me more about that woodworking thing you do"
WOODWORK = "his dad taught him woodworking in the garage every summer"


class _ProviderFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _host(self, **kwargs: Any) -> _Host:
        return _Host(self.store, **kwargs)

    def _rows(self) -> list:
        return self.store.list_for_user(cue_type="long_arc_callback")


class ProviderTests(_ProviderFixture):
    def test_fires_and_arms_gates(self) -> None:
        host = self._host()
        block = host._render_long_arc_callback_block(LONG_MSG)
        self.assertNotEqual(block, "")
        self.assertIn("Jacob", block)
        self.assertEqual(host._long_arc_callback_session_count, 1)
        self.assertIsNotNone(host._last_long_arc_callback)
        self.assertIn(1, lac.load_recent_ids(host._kv.get))

    def test_master_switch_off(self) -> None:
        host = self._host(agent=_agent(long_arc_callback_enabled=False))
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_per_session_cap(self) -> None:
        host = self._host(session_count=1)
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_short_turn_skipped(self) -> None:
        host = self._host()
        self.assertEqual(host._render_long_arc_callback_block("hey"), "")
        # No embed/search attempted.
        self.assertEqual(host._rag_retriever.calls, 0)

    def test_cadence_blocks_a_second_new_callback(self) -> None:
        self.assertNotEqual(
            self._host()._render_long_arc_callback_block(LONG_MSG), "",
        )
        # A fresh session clears the per-session cap, but the six-hour
        # spacing recorded on the ledger row does not clear with it.
        host = self._host(candidates=[_cand(9, cosine=0.7, age_days=40)])
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_no_candidates_silent(self) -> None:
        host = self._host(candidates=[])
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")
        # Nothing armed on a miss.
        self.assertEqual(host._long_arc_callback_session_count, 0)
        self.assertEqual(self._rows(), [])

    def test_recent_id_excluded(self) -> None:
        host = self._host(candidates=[_cand(5, cosine=0.7, age_days=40)])
        lac.append_recent_id(host._kv.get, host._kv.set, 5)
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_force_next_bypasses_cap_and_cadence(self) -> None:
        self._host()._render_long_arc_callback_block(LONG_MSG)
        host = self._host(
            candidates=[_cand(9, cosine=0.7, age_days=40)],
            session_count=5,
            force_next=True,
        )
        block = host._render_long_arc_callback_block("hi")  # short too
        self.assertNotEqual(block, "")
        self.assertFalse(host.debug_overrides.peek("long_arc_callback_force_next"))

    def test_force_next_consumed_on_miss(self) -> None:
        host = self._host(candidates=[], force_next=True)
        self.assertEqual(host._render_long_arc_callback_block("hi"), "")
        self.assertFalse(host.debug_overrides.peek("long_arc_callback_force_next"))


# ── the surface-time ledger ──────────────────────────────────────────


class LedgerTests(_ProviderFixture):
    """An ignored callback is held, not spent.

    The old provider burned the pick into the don't-repeat ring and
    started the six-hour clock the instant the block rendered, so a
    callback Aiko never picked up was gone for good. Now the row it
    writes is the retry.
    """

    def _fire(self, **kwargs: Any) -> _Host:
        host = self._host(
            candidates=[_cand(1, cosine=0.7, age_days=40, content=WOODWORK)],
            **kwargs,
        )
        self.assertNotEqual(host._render_long_arc_callback_block(LONG_MSG), "")
        return host

    def test_firing_writes_a_surfaced_row(self) -> None:
        self._fire()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "surfaced")
        self.assertEqual(rows[0].payload.get("memory_id"), 1)

    def test_an_ignored_callback_comes_back_on_the_same_thread(self) -> None:
        host = self._fire()
        row = self._rows()[0]
        self.store.release(row.id, evidence="test")
        self.assertEqual(self._rows()[0].state, STATE_PENDING)

        # New session, cadence still unspent, and the retriever has
        # nothing -- so anything that renders came from the pool.
        later = self._host(candidates=[])
        block = later._render_long_arc_callback_block(
            "still on the woodworking thing -- is that garage set up properly?"
        )
        self.assertIn("woodworking", block)
        self.assertEqual(later._rag_retriever.calls, 0)
        self.assertEqual(self._rows()[0].surfaced_count, 2)

    def test_a_released_callback_stays_put_once_the_topic_moves_on(self) -> None:
        self._fire()
        row = self._rows()[0]
        self.store.release(row.id, evidence="test")

        later = self._host(candidates=[])
        self.assertEqual(
            later._render_long_arc_callback_block(
                "anyway the deployment finally went out this morning"
            ),
            "",
        )
        self.assertEqual(self._rows()[0].state, STATE_PENDING)

    def test_a_retry_ignores_the_per_session_cap(self) -> None:
        """The cap governs opening callbacks, not finishing one."""
        self._fire()
        self.store.release(self._rows()[0].id, evidence="test")

        later = self._host(candidates=[], session_count=99)
        self.assertNotEqual(
            later._render_long_arc_callback_block(
                "still on the woodworking thing -- is that garage any good?"
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()

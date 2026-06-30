"""Tests for the K63 long-arc callback ("weeks ago you said…") feature.

Three layers:

* Pure module (:mod:`app.core.conversation.long_arc_callback`) — the
  ``select`` picker, the ``render_block`` cue copy, the kv cooldown +
  don't-repeat ring helpers, and the ``candidates_from_hits`` projection.
* Provider (:meth:`InnerLifePart3Mixin._render_long_arc_callback_block`)
  through a minimal mixin host with a fake retriever + kv-backed chat_db —
  the cap / cooldown / min-words gates, the don't-repeat ring, the
  master switch, and the force-next bypass.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.conversation import long_arc_callback as lac
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
    def test_cooldown_elapsed_when_unset(self) -> None:
        kv = _KV()
        self.assertTrue(
            lac.cooldown_elapsed(kv.get, now=datetime.now(timezone.utc), cooldown_hours=6.0)
        )

    def test_cooldown_blocks_then_passes(self) -> None:
        kv = _KV()
        now = datetime.now(timezone.utc)
        lac.mark_fired(kv.set, now=now)
        self.assertFalse(
            lac.cooldown_elapsed(kv.get, now=now + timedelta(hours=1), cooldown_hours=6.0)
        )
        self.assertTrue(
            lac.cooldown_elapsed(kv.get, now=now + timedelta(hours=7), cooldown_hours=6.0)
        )

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


# ── Pure: candidates_from_hits ───────────────────────────────────────


def _hit(mid: int, *, kind: str, age_days: float, score: float, content: str = "c") -> SimpleNamespace:
    return SimpleNamespace(
        score=score,
        record=SimpleNamespace(
            id=str(mid), kind=kind, created_at=_iso(age_days), content=content
        ),
    )


class CandidatesFromHitsTests(unittest.TestCase):
    def test_age_floor_filters(self) -> None:
        now = datetime.now(timezone.utc)
        hits = [_hit(1, kind="fact", age_days=10, score=0.7), _hit(2, kind="fact", age_days=30, score=0.7)]
        cands = lac.candidates_from_hits(hits, now=now, min_age_days=21)
        self.assertEqual([c.memory_id for c in cands], [2])

    def test_kind_filter(self) -> None:
        now = datetime.now(timezone.utc)
        hits = [_hit(1, kind="self", age_days=40, score=0.7), _hit(2, kind="fact", age_days=40, score=0.7)]
        cands = lac.candidates_from_hits(
            hits, now=now, min_age_days=21, allowed_kinds=lac.ALLOWED_KINDS
        )
        self.assertEqual([c.memory_id for c in cands], [2])

    def test_blank_and_bad_id_skipped(self) -> None:
        now = datetime.now(timezone.utc)
        bad = SimpleNamespace(score=0.9, record=SimpleNamespace(id="abc", kind="fact", created_at=_iso(40), content="c"))
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
        long_arc_callback_cooldown_hours=6.0,
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


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
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
        self._long_arc_callback_session_count = session_count
        self._long_arc_callback_force_next = force_next
        self._last_long_arc_callback: Any = None
        self.user_display_name = "Jacob"


LONG_MSG = "so tell me more about that woodworking thing you do"


class ProviderTests(unittest.TestCase):
    def test_fires_and_arms_gates(self) -> None:
        host = _Host()
        block = host._render_long_arc_callback_block(LONG_MSG)
        self.assertNotEqual(block, "")
        self.assertIn("Jacob", block)
        self.assertEqual(host._long_arc_callback_session_count, 1)
        self.assertIsNotNone(host._last_long_arc_callback)
        # Cooldown stamped + id ringed.
        self.assertIn(1, lac.load_recent_ids(host._kv.get))
        self.assertTrue(host._kv.get(lac.KV_LAST_FIRED_AT))

    def test_master_switch_off(self) -> None:
        host = _Host(agent=_agent(long_arc_callback_enabled=False))
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_per_session_cap(self) -> None:
        host = _Host(session_count=1)
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_short_turn_skipped(self) -> None:
        host = _Host()
        self.assertEqual(host._render_long_arc_callback_block("hey"), "")
        # No embed/search attempted.
        self.assertEqual(host._rag_retriever.calls, 0)

    def test_cooldown_blocks(self) -> None:
        host = _Host()
        lac.mark_fired(host._kv.set, now=datetime.now(timezone.utc))
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_no_candidates_silent(self) -> None:
        host = _Host(candidates=[])
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")
        # Nothing armed on a miss.
        self.assertEqual(host._long_arc_callback_session_count, 0)
        self.assertFalse(host._kv.get(lac.KV_LAST_FIRED_AT))

    def test_recent_id_excluded(self) -> None:
        host = _Host(candidates=[_cand(5, cosine=0.7, age_days=40)])
        lac.append_recent_id(host._kv.get, host._kv.set, 5)
        self.assertEqual(host._render_long_arc_callback_block(LONG_MSG), "")

    def test_force_next_bypasses_cap_and_cooldown(self) -> None:
        host = _Host(session_count=5, force_next=True)
        lac.mark_fired(host._kv.set, now=datetime.now(timezone.utc))
        block = host._render_long_arc_callback_block("hi")  # short too
        self.assertNotEqual(block, "")
        self.assertFalse(host._long_arc_callback_force_next)

    def test_force_next_consumed_on_miss(self) -> None:
        host = _Host(candidates=[], force_next=True)
        self.assertEqual(host._render_long_arc_callback_block("hi"), "")
        self.assertFalse(host._long_arc_callback_force_next)


if __name__ == "__main__":
    unittest.main()

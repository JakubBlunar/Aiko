"""Tests for F10h — topic temperature / per-cluster affect.

Covers the pure scoring module
(:mod:`app.core.conversation.topic_temperature`) and the inner-life
consumer
(:meth:`InnerLifePart2Mixin._render_topic_temperature_block`).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any

import json

from app.core.conversation.topic_temperature import (
    KV_MOOD_ORIGIN,
    ClusterTemperature,
    MomentCandidate,
    pick_origin,
    render_block,
    score_cluster,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── pure module ──────────────────────────────────────────────────────────


class ScoreClusterTests(unittest.TestCase):
    def test_empty_is_silent(self) -> None:
        temp = score_cluster([])
        self.assertIsNone(temp.dominant)
        self.assertEqual(temp.moment_count, 0)

    def test_general_only_is_silent(self) -> None:
        temp = score_cluster(["general", "general"])
        self.assertIsNone(temp.dominant)
        self.assertEqual(temp.moment_count, 2)
        self.assertEqual(temp.warmth, 0.0)
        self.assertEqual(temp.tenderness, 0.0)

    def test_warm_dominant(self) -> None:
        temp = score_cluster(["warm", "milestone", "playful"])
        self.assertEqual(temp.dominant, "warm")
        self.assertGreaterEqual(temp.warmth, 0.5)
        self.assertEqual(temp.tenderness, 0.0)

    def test_tender_dominant(self) -> None:
        temp = score_cluster(["vulnerable", "comfort"])
        self.assertEqual(temp.dominant, "tender")
        self.assertGreaterEqual(temp.tenderness, 0.5)

    def test_tender_wins_ties(self) -> None:
        # Equal-ish pull both poles; tenderness wins when >= warmth.
        temp = score_cluster(
            ["warm", "tender"], threshold=0.0,
        )
        self.assertEqual(temp.dominant, "tender")

    def test_threshold_gates(self) -> None:
        # A single weak warm beat doesn't clear the default 0.5.
        temp = score_cluster(["silly"], threshold=0.5)
        self.assertIsNone(temp.dominant)
        # ...but a zero threshold (force path) lets any signal fire.
        temp2 = score_cluster(["silly"], threshold=0.0)
        self.assertEqual(temp2.dominant, "warm")

    def test_saturation_caps_at_one(self) -> None:
        temp = score_cluster(["warm"] * 20)
        self.assertEqual(temp.warmth, 1.0)

    def test_unknown_vibe_ignored(self) -> None:
        temp = score_cluster(["banana", "warm"], threshold=0.0)
        # Only "warm" counts toward the score; "banana" adds 0.
        self.assertEqual(temp.dominant, "warm")
        self.assertEqual(temp.moment_count, 2)


class RenderBlockTests(unittest.TestCase):
    def test_warm_line(self) -> None:
        out = render_block(
            ClusterTemperature(0.9, 0.0, "warm", 3), "guitar", "Jacob",
        )
        self.assertIn("warm spot", out)
        self.assertIn("guitar", out)
        self.assertIn("Jacob", out)

    def test_tender_line(self) -> None:
        out = render_block(
            ClusterTemperature(0.0, 0.9, "tender", 2), "his dad", "Jacob",
        )
        self.assertIn("tender ground", out)
        self.assertIn("gently", out)

    def test_none_dominant_is_blank(self) -> None:
        self.assertEqual(
            render_block(ClusterTemperature(0.0, 0.0, None, 0), "x", "Jacob"),
            "",
        )


# ── provider fakes ───────────────────────────────────────────────────────


@dataclass
class _FakeMem:
    kind: str
    metadata: dict[str, Any] = field(default_factory=dict)


class _FakeEmbedder:
    def embed(self, text: str):
        return [1.0, 0.0, 0.0]


class _FakeGraph:
    persistent = True

    def __init__(self, *, match=None, members=None, vibes_by_id=None) -> None:
        self._match = match  # (cluster_id, label, sim) or None
        self._members = members or []
        self._vibes_by_id = vibes_by_id or {}
        self.best_calls: list[dict] = []

    def best_clusters_for(self, qvec, *, top_n=1, min_sim=0.0):
        self.best_calls.append({"top_n": top_n, "min_sim": min_sim})
        return [self._match] if self._match else []

    def cluster_member_ids(self, cluster_id):
        return list(self._members)


class _FakeStore:
    def __init__(self, vibes_by_id: dict[int, str]) -> None:
        self._vibes_by_id = vibes_by_id

    def get(self, mid):
        if mid not in self._vibes_by_id:
            return None
        return _FakeMem(kind="shared_moment", metadata={"vibe": self._vibes_by_id[mid]})


class _Agent:
    topic_temperature_enabled = True


class _MemSettings:
    topic_temperature_min_sim = 0.45
    topic_temperature_threshold = 0.5
    topic_temperature_cooldown_turns = 6


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self, graph, store) -> None:
        self._settings = _Settings()
        self._memory_settings = _MemSettings()
        self._topic_graph = graph
        self._embedder = _FakeEmbedder()
        self._memory_store = store

    @property
    def user_display_name(self) -> str:
        return "Jacob"


class ProviderTests(unittest.TestCase):
    def _host(self, *, match, members, vibes) -> _Host:
        return _Host(
            _FakeGraph(match=match, members=members),
            _FakeStore(vibes),
        )

    def test_warm_cluster_surfaces(self) -> None:
        host = self._host(
            match=(1, "guitar", 0.8),
            members=[10, 11, 12],
            vibes={10: "warm", 11: "milestone", 12: "playful"},
        )
        out = host._render_topic_temperature_block("tell me about guitar")
        self.assertIn("warm spot", out)
        self.assertIn("guitar", out)

    def test_tender_cluster_surfaces(self) -> None:
        host = self._host(
            match=(2, "his dad", 0.7),
            members=[20, 21],
            vibes={20: "vulnerable", 21: "comfort"},
        )
        out = host._render_topic_temperature_block("thinking about my dad")
        self.assertIn("tender ground", out)

    def test_no_cluster_match_blank(self) -> None:
        host = self._host(match=None, members=[], vibes={})
        self.assertEqual(
            host._render_topic_temperature_block("random text here"), ""
        )

    def test_no_shared_moments_blank(self) -> None:
        # Cluster matches but its members carry no vibes (not moments).
        host = _Host(
            _FakeGraph(match=(3, "weather", 0.8), members=[30]),
            _FakeStore({}),  # get(30) -> None
        )
        self.assertEqual(
            host._render_topic_temperature_block("about the weather"), ""
        )

    def test_disabled_blank(self) -> None:
        host = self._host(
            match=(1, "guitar", 0.8), members=[10], vibes={10: "warm"},
        )
        host._settings.agent.topic_temperature_enabled = False
        self.assertEqual(
            host._render_topic_temperature_block("guitar stuff"), ""
        )

    def test_short_text_blank(self) -> None:
        host = self._host(
            match=(1, "guitar", 0.8), members=[10], vibes={10: "warm"},
        )
        self.assertEqual(host._render_topic_temperature_block("hi"), "")

    def test_cooldown_suppresses_next_turn(self) -> None:
        host = self._host(
            match=(1, "guitar", 0.8),
            members=[10, 11, 12],
            vibes={10: "warm", 11: "milestone", 12: "warm"},
        )
        first = host._render_topic_temperature_block("guitar please")
        self.assertTrue(first)
        # Cooldown armed -> immediately blank on the next call.
        second = host._render_topic_temperature_block("guitar again")
        self.assertEqual(second, "")
        self.assertEqual(host._topic_temperature_cooldown, 5)

    def test_force_bypasses_cooldown_and_threshold(self) -> None:
        host = self._host(
            match=(1, "guitar", 0.2),  # below default min_sim
            members=[10],
            vibes={10: "silly"},  # weak warm, below default threshold
        )
        host._topic_temperature_cooldown = 4
        host._topic_temperature_force_next = True
        out = host._render_topic_temperature_block("guitar stuff here")
        self.assertIn("warm spot", out)
        # Force consumed; min_sim dropped to 0 on the bypass call.
        self.assertFalse(host._topic_temperature_force_next)
        self.assertEqual(host._topic_temperature_last["dominant"], "warm")


# ── H8: origin selection + clause (pure) ─────────────────────────────────


class PickOriginTests(unittest.TestCase):
    def _c(self, mid, vibe, what="m", created="2026-01-01") -> MomentCandidate:
        return MomentCandidate(
            moment_id=mid, vibe=vibe, what=what, when="", created_at=created,
        )

    def test_empty_is_none(self) -> None:
        self.assertIsNone(pick_origin([], "tender"))

    def test_no_pole_match_is_none(self) -> None:
        # Warm-only candidates can't explain a tender pole.
        cands = [self._c(1, "warm"), self._c(2, "playful")]
        self.assertIsNone(pick_origin(cands, "tender"))

    def test_picks_highest_weight_for_pole(self) -> None:
        cands = [
            self._c(1, "comfort"),     # tender 1.1
            self._c(2, "vulnerable"),  # tender 1.4 -> winner
            self._c(3, "warm"),        # not tender
        ]
        best = pick_origin(cands, "tender")
        self.assertIsNotNone(best)
        self.assertEqual(best.moment_id, 2)

    def test_recency_breaks_tie(self) -> None:
        cands = [
            self._c(1, "warm", created="2026-01-01"),
            self._c(2, "warm", created="2026-06-01"),  # newer wins
        ]
        best = pick_origin(cands, "warm")
        self.assertEqual(best.moment_id, 2)


class OriginRenderTests(unittest.TestCase):
    def test_tender_appends_origin_clause(self) -> None:
        out = render_block(
            ClusterTemperature(0.0, 0.9, "tender", 2),
            "his dad",
            "Jacob",
            origin_what="you told me you lost your dad last year",
        )
        self.assertIn("tender ground", out)
        self.assertIn("ever since", out)
        self.assertIn("lost your dad", out)

    def test_warm_appends_origin_clause(self) -> None:
        out = render_block(
            ClusterTemperature(0.9, 0.0, "warm", 2),
            "guitar",
            "Jacob",
            origin_what="we jammed together for the first time",
        )
        self.assertIn("warm spot", out)
        self.assertIn("traces back", out)
        self.assertIn("jammed", out)

    def test_no_origin_is_bare_line(self) -> None:
        out = render_block(
            ClusterTemperature(0.0, 0.9, "tender", 2), "his dad", "Jacob",
        )
        self.assertIn("tender ground", out)
        self.assertNotIn("ever since", out)

    def test_blank_origin_is_bare_line(self) -> None:
        out = render_block(
            ClusterTemperature(0.9, 0.0, "warm", 2),
            "guitar",
            "Jacob",
            origin_what="   ",
        )
        self.assertNotIn("traces back", out)


# ── H8: provider stamping + read-back ────────────────────────────────────


@dataclass
class _RichMem:
    kind: str
    id: int
    content: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


class _RichStore:
    def __init__(self, mems: dict[int, _RichMem]) -> None:
        self._mems = mems

    def get(self, mid):
        return self._mems.get(mid)


class _FakeChatDb:
    def __init__(self) -> None:
        self._kv: dict[str, str] = {}

    def kv_get(self, key: str):
        return self._kv.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self._kv[key] = value


class OriginProviderTests(unittest.TestCase):
    def _host(self, *, match, mems, chat_db) -> _Host:
        host = _Host(
            _FakeGraph(match=match, members=list(mems.keys())),
            _RichStore(mems),
        )
        host._chat_db = chat_db
        host._settings.agent.topic_mood_origin_enabled = True
        return host

    def _moment(self, mid, vibe, what) -> _RichMem:
        return _RichMem(
            kind="shared_moment",
            id=mid,
            content=what,
            created_at="2026-01-0%d" % mid,
            metadata={"vibe": vibe, "what": what, "when": "2026-01-01"},
        )

    def test_stamps_and_surfaces_origin(self) -> None:
        db = _FakeChatDb()
        mems = {
            20: self._moment(20, "vulnerable", "you opened up about your dad"),
            21: self._moment(21, "comfort", "I sat with you that night"),
        }
        host = self._host(match=(7, "his dad", 0.8), mems=mems, chat_db=db)
        out = host._render_topic_temperature_block("thinking about my dad")
        self.assertIn("tender ground", out)
        self.assertIn("ever since", out)
        self.assertIn("opened up about your dad", out)
        # Origin persisted to kv keyed by cluster id.
        stored = json.loads(db.kv_get(KV_MOOD_ORIGIN))
        self.assertIn("7", stored)
        self.assertEqual(stored["7"]["pole"], "tender")
        self.assertEqual(stored["7"]["moment_id"], 20)

    def test_origin_is_stable_across_fires(self) -> None:
        db = _FakeChatDb()
        mems = {
            20: self._moment(20, "vulnerable", "you opened up about your dad"),
        }
        host = self._host(match=(7, "his dad", 0.8), mems=mems, chat_db=db)
        host._render_topic_temperature_block("about my dad please")
        host._topic_temperature_cooldown = 0  # clear cooldown
        # A new vulnerable moment appears, but the origin stays the first.
        host._memory_store._mems[22] = self._moment(
            22, "vulnerable", "a totally different admission"
        )
        host._topic_graph._members = [20, 22]
        out = host._render_topic_temperature_block("dad again here")
        self.assertIn("opened up about your dad", out)
        self.assertNotIn("totally different admission", out)

    def test_disabled_origin_gives_bare_line(self) -> None:
        db = _FakeChatDb()
        mems = {
            20: self._moment(20, "vulnerable", "you opened up about your dad"),
        }
        host = self._host(match=(7, "his dad", 0.8), mems=mems, chat_db=db)
        host._settings.agent.topic_mood_origin_enabled = False
        out = host._render_topic_temperature_block("thinking about my dad")
        self.assertIn("tender ground", out)
        self.assertNotIn("ever since", out)
        self.assertIsNone(db.kv_get(KV_MOOD_ORIGIN))

    def test_pole_flip_restamps(self) -> None:
        db = _FakeChatDb()
        # Pre-seed a stale warm origin for the cluster.
        db.kv_set(
            KV_MOOD_ORIGIN,
            json.dumps(
                {"7": {"pole": "warm", "what": "old warm thing",
                       "when": "", "moment_id": 1, "stamped_at": "x"}}
            ),
        )
        mems = {
            20: self._moment(20, "vulnerable", "you opened up about your dad"),
        }
        host = self._host(match=(7, "his dad", 0.8), mems=mems, chat_db=db)
        out = host._render_topic_temperature_block("thinking about my dad")
        self.assertIn("tender ground", out)
        self.assertIn("opened up about your dad", out)
        stored = json.loads(db.kv_get(KV_MOOD_ORIGIN))
        self.assertEqual(stored["7"]["pole"], "tender")


if __name__ == "__main__":
    unittest.main()

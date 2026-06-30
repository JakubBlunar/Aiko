"""Tests for K66 — earned familiarity ("well-trodden ground between us").

Covers the pure scoring module
(:mod:`app.core.conversation.earned_familiarity`) and the inner-life
consumer
(:meth:`InnerLifePart2Mixin._render_earned_familiarity_block`).
"""
from __future__ import annotations

import unittest

from app.core.conversation.earned_familiarity import (
    FamiliarityRead,
    render_block,
    score_familiarity,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── pure module ──────────────────────────────────────────────────────────


class ScoreFamiliarityTests(unittest.TestCase):
    def test_deep_cluster(self) -> None:
        read = score_familiarity(14)
        self.assertEqual(read.band, "deep")
        self.assertEqual(read.size, 14)

    def test_above_threshold_is_deep(self) -> None:
        self.assertEqual(score_familiarity(40).band, "deep")

    def test_below_threshold_silent(self) -> None:
        self.assertIsNone(score_familiarity(13).band)
        self.assertIsNone(score_familiarity(0).band)

    def test_custom_threshold(self) -> None:
        self.assertIsNone(score_familiarity(8, deep_threshold=20).band)
        self.assertEqual(score_familiarity(25, deep_threshold=20).band, "deep")

    def test_negative_size_clamped(self) -> None:
        read = score_familiarity(-5)
        self.assertEqual(read.size, 0)
        self.assertIsNone(read.band)

    def test_force_threshold_one_forces_deep(self) -> None:
        # The MCP force path drops the threshold to 1.
        self.assertEqual(score_familiarity(3, deep_threshold=1).band, "deep")


class RenderBlockTests(unittest.TestCase):
    def test_deep_line(self) -> None:
        out = render_block(FamiliarityRead(20, "deep"), "his training", "Jacob")
        self.assertIn("well-worn ground", out)
        self.assertIn("Jacob", out)
        self.assertIn("shorthand", out)

    def test_deep_line_forbids_counting(self) -> None:
        # The copy must teach her NOT to quantify the history out loud.
        out = render_block(FamiliarityRead(20, "deep"), "x", "Jacob")
        self.assertIn("Never count it out loud", out)

    def test_none_band_blank(self) -> None:
        self.assertEqual(
            render_block(FamiliarityRead(5, None), "x", "Jacob"), ""
        )

    def test_blank_label_falls_back(self) -> None:
        out = render_block(FamiliarityRead(20, "deep"), "", "Jacob")
        self.assertIn("this topic", out)


# ── provider fakes ───────────────────────────────────────────────────────


class _FakeEmbedder:
    def embed(self, text: str):
        return [1.0, 0.0, 0.0]


class _FakeGraph:
    persistent = True

    def __init__(self, *, match=None, member_ids=None) -> None:
        self._match = match  # (cluster_id, label, sim) or None
        self._member_ids = member_ids or []
        self.best_calls: list[dict] = []

    def best_clusters_for(self, qvec, *, top_n=1, min_sim=0.0):
        self.best_calls.append({"top_n": top_n, "min_sim": min_sim})
        return [self._match] if self._match else []

    def cluster_member_ids(self, cluster_id):
        return list(self._member_ids)


class _Agent:
    earned_familiarity_enabled = True


class _MemSettings:
    earned_familiarity_min_sim = 0.45
    earned_familiarity_deep_threshold = 14
    earned_familiarity_cooldown_turns = 12


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self, graph) -> None:
        self._settings = _Settings()
        self._memory_settings = _MemSettings()
        self._topic_graph = graph
        self._embedder = _FakeEmbedder()

    @property
    def user_display_name(self) -> str:
        return "Jacob"


class ProviderTests(unittest.TestCase):
    def _host(self, *, match, size) -> _Host:
        return _Host(
            _FakeGraph(match=match, member_ids=list(range(size)))
        )

    def test_deep_surfaces(self) -> None:
        host = self._host(match=(1, "his training", 0.8), size=20)
        out = host._render_earned_familiarity_block(
            "tell me about your training block"
        )
        self.assertIn("well-worn ground", out)

    def test_shallow_is_blank(self) -> None:
        host = self._host(match=(2, "cooking", 0.8), size=6)
        self.assertEqual(
            host._render_earned_familiarity_block("about cooking tonight"), ""
        )

    def test_no_match_blank(self) -> None:
        host = self._host(match=None, size=0)
        self.assertEqual(
            host._render_earned_familiarity_block("random text here"), ""
        )

    def test_disabled_blank(self) -> None:
        host = self._host(match=(1, "training", 0.8), size=20)
        host._settings.agent.earned_familiarity_enabled = False
        self.assertEqual(
            host._render_earned_familiarity_block("training stuff"), ""
        )

    def test_short_text_blank(self) -> None:
        host = self._host(match=(1, "training", 0.8), size=20)
        self.assertEqual(host._render_earned_familiarity_block("hi"), "")

    def test_cooldown_suppresses_next_turn(self) -> None:
        host = self._host(match=(1, "training", 0.8), size=20)
        first = host._render_earned_familiarity_block("about training here")
        self.assertTrue(first)
        second = host._render_earned_familiarity_block("more training talk")
        self.assertEqual(second, "")
        self.assertEqual(host._earned_familiarity_cooldown, 11)
        self.assertEqual(host._earned_familiarity_last["band"], "deep")

    def test_force_bypasses_cooldown_and_min_sim(self) -> None:
        # Shallow cluster that would normally be silent; force → deep.
        host = self._host(match=(1, "cooking", 0.1), size=3)
        host._earned_familiarity_cooldown = 4
        host._earned_familiarity_force_next = True
        out = host._render_earned_familiarity_block("cooking dinner tonight")
        self.assertTrue(out)
        self.assertFalse(host._earned_familiarity_force_next)
        # min_sim dropped to 0 on the forced call.
        self.assertEqual(host._topic_graph.best_calls[-1]["min_sim"], 0.0)
        self.assertEqual(host._earned_familiarity_last["band"], "deep")


if __name__ == "__main__":
    unittest.main()

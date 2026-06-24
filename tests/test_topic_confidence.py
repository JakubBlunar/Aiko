"""Tests for F10i — per-topic confidence self-model.

Covers the pure scoring module
(:mod:`app.core.conversation.topic_confidence`) and the inner-life
consumer
(:meth:`InnerLifePart2Mixin._render_topic_confidence_block`).
"""
from __future__ import annotations

import unittest
from typing import Any

from app.core.conversation.topic_confidence import (
    ClusterConfidence,
    render_block,
    score_confidence,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── pure module ──────────────────────────────────────────────────────────


class ScoreConfidenceTests(unittest.TestCase):
    def test_thin_cluster(self) -> None:
        # Small cluster, no learned facts -> low confidence -> thin.
        conf = score_confidence(3, 0)
        self.assertEqual(conf.band, "thin")
        self.assertLessEqual(conf.confidence, 0.25)

    def test_rich_cluster_familiar(self) -> None:
        conf = score_confidence(30, 5)
        self.assertEqual(conf.band, "familiar")
        self.assertGreaterEqual(conf.confidence, 0.7)

    def test_middle_is_silent(self) -> None:
        # Mid-size, no learned facts -> sits in the silent middle.
        conf = score_confidence(6, 0)
        self.assertIsNone(conf.band)

    def test_dense_unresearched_is_not_thin(self) -> None:
        # F10f's territory: a big cluster with 0 knowledge still scores
        # mid/high here (size carries it), so it never reads as "thin".
        conf = score_confidence(20, 0)
        self.assertNotEqual(conf.band, "thin")

    def test_confidence_in_range(self) -> None:
        self.assertEqual(score_confidence(0, 0).confidence, 0.0)
        self.assertLessEqual(score_confidence(1000, 1000).confidence, 1.0)

    def test_split_thresholds_force_a_side(self) -> None:
        # The MCP force path passes equal thresholds at 0.5.
        low = score_confidence(3, 0, thin_threshold=0.5, familiar_threshold=0.5)
        self.assertEqual(low.band, "thin")
        high = score_confidence(
            30, 5, thin_threshold=0.5, familiar_threshold=0.5,
        )
        self.assertEqual(high.band, "familiar")


class RenderBlockTests(unittest.TestCase):
    def test_thin_line(self) -> None:
        out = render_block(ClusterConfidence(3, 0, 0.15, "thin"), "quantum physics", "Jacob")
        self.assertIn("thin ground", out)
        self.assertIn("Jacob", out)

    def test_familiar_line(self) -> None:
        out = render_block(
            ClusterConfidence(30, 5, 0.95, "familiar"), "his job", "Jacob",
        )
        self.assertIn("well-trodden", out)
        self.assertIn("no need to hedge", out)

    def test_none_band_blank(self) -> None:
        self.assertEqual(
            render_block(ClusterConfidence(6, 0, 0.4, None), "x", "Jacob"), ""
        )


# ── provider fakes ───────────────────────────────────────────────────────


class _FakeEmbedder:
    def embed(self, text: str):
        return [1.0, 0.0, 0.0]


class _FakeGraph:
    persistent = True

    def __init__(self, *, match=None, stats=None) -> None:
        self._match = match  # (cluster_id, label, sim) or None
        self._stats = stats  # (size, learned) or None
        self.best_calls: list[dict] = []

    def best_clusters_for(self, qvec, *, top_n=1, min_sim=0.0):
        self.best_calls.append({"top_n": top_n, "min_sim": min_sim})
        return [self._match] if self._match else []

    def cluster_knowledge_stats(self, cluster_id):
        return self._stats


class _Agent:
    topic_confidence_enabled = True


class _MemSettings:
    topic_confidence_min_sim = 0.45
    topic_confidence_thin_threshold = 0.25
    topic_confidence_familiar_threshold = 0.7
    topic_confidence_cooldown_turns = 6


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
    def _host(self, *, match, stats) -> _Host:
        return _Host(_FakeGraph(match=match, stats=stats))

    def test_thin_surfaces(self) -> None:
        host = self._host(match=(1, "quantum physics", 0.8), stats=(3, 0))
        out = host._render_topic_confidence_block("tell me about quantum physics")
        self.assertIn("thin ground", out)

    def test_familiar_surfaces(self) -> None:
        host = self._host(match=(2, "his job", 0.8), stats=(30, 5))
        out = host._render_topic_confidence_block("how's work going")
        self.assertIn("well-trodden", out)

    def test_middle_is_blank(self) -> None:
        host = self._host(match=(3, "cooking", 0.8), stats=(6, 0))
        self.assertEqual(
            host._render_topic_confidence_block("about cooking tonight"), ""
        )

    def test_no_match_blank(self) -> None:
        host = self._host(match=None, stats=None)
        self.assertEqual(
            host._render_topic_confidence_block("random text here"), ""
        )

    def test_no_stats_blank(self) -> None:
        host = self._host(match=(4, "weather", 0.8), stats=None)
        self.assertEqual(
            host._render_topic_confidence_block("about the weather"), ""
        )

    def test_disabled_blank(self) -> None:
        host = self._host(match=(1, "physics", 0.8), stats=(3, 0))
        host._settings.agent.topic_confidence_enabled = False
        self.assertEqual(
            host._render_topic_confidence_block("physics stuff"), ""
        )

    def test_short_text_blank(self) -> None:
        host = self._host(match=(1, "physics", 0.8), stats=(3, 0))
        self.assertEqual(host._render_topic_confidence_block("hi"), "")

    def test_cooldown_suppresses_next_turn(self) -> None:
        host = self._host(match=(1, "physics", 0.8), stats=(3, 0))
        first = host._render_topic_confidence_block("about physics here")
        self.assertTrue(first)
        second = host._render_topic_confidence_block("more physics talk")
        self.assertEqual(second, "")
        self.assertEqual(host._topic_confidence_cooldown, 5)

    def test_force_bypasses_cooldown_and_min_sim(self) -> None:
        # Mid cluster that would normally be silent; force splits at 0.5.
        host = self._host(match=(1, "cooking", 0.1), stats=(6, 0))
        host._topic_confidence_cooldown = 4
        host._topic_confidence_force_next = True
        out = host._render_topic_confidence_block("cooking dinner tonight")
        self.assertTrue(out)
        self.assertFalse(host._topic_confidence_force_next)
        # min_sim dropped to 0 on the forced call.
        self.assertEqual(host._topic_graph.best_calls[-1]["min_sim"], 0.0)
        self.assertIn(host._topic_confidence_last["band"], {"thin", "familiar"})


if __name__ == "__main__":
    unittest.main()

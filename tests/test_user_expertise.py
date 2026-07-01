"""Tests for K75 — user-expertise calibration.

Covers the pure estimator (:mod:`app.core.conversation.user_expertise`)
and the inner-life consumer
(:meth:`InnerLifePart2Mixin._render_user_expertise_block`).
"""
from __future__ import annotations

import unittest

from app.core.conversation import user_expertise as ue
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── pure: classify_message ───────────────────────────────────────────────


class ClassifyMessageTests(unittest.TestCase):
    def test_short_or_empty_none(self) -> None:
        self.assertIsNone(ue.classify_message(""))
        self.assertIsNone(ue.classify_message("ok"))

    def test_neutral_none(self) -> None:
        self.assertIsNone(ue.classify_message("that sounds nice, thanks"))

    def test_expert_self_report_positive(self) -> None:
        s = ue.classify_message("I built the auth layer and maintain it")
        self.assertIsNotNone(s)
        self.assertGreater(s, 0.3)

    def test_correction_positive(self) -> None:
        s = ue.classify_message(
            "actually, that's not right — the GIL only blocks CPU-bound threads"
        )
        self.assertGreater(s, 0.3)

    def test_jargon_positive(self) -> None:
        s = ue.classify_message(
            "set `wal_autocheckpoint` and run VACUUM after the migration"
        )
        self.assertGreater(s, 0.0)

    def test_novice_self_report_negative(self) -> None:
        s = ue.classify_message("I'm new to python and don't really know much")
        self.assertIsNotNone(s)
        self.assertLess(s, -0.3)

    def test_basic_question_negative(self) -> None:
        s = ue.classify_message("how do i make a loop, can you explain?")
        self.assertLess(s, 0.0)

    def test_clamped(self) -> None:
        s = ue.classify_message(
            "I'm a senior engineer, I built and maintain this; in my "
            "experience the `event_loop` and asyncio internals matter"
        )
        self.assertLessEqual(s, 1.0)
        self.assertGreaterEqual(s, -1.0)


# ── pure: update_state / band_for ────────────────────────────────────────


class UpdateStateTests(unittest.TestCase):
    def test_first_sample(self) -> None:
        st = ue.update_state(None, 0.6, now_iso="2026-01-01T00:00:00+00:00")
        self.assertEqual(st.samples, 1)
        self.assertAlmostEqual(st.score, 0.6)

    def test_ema_blend(self) -> None:
        st = ue.update_state(None, 1.0, now_iso="t")
        st = ue.update_state(st, -1.0, learning_rate=0.5, now_iso="t")
        self.assertAlmostEqual(st.score, 0.0)
        self.assertEqual(st.samples, 2)

    def test_clamped(self) -> None:
        st = ue.update_state(None, 5.0, now_iso="t")
        self.assertLessEqual(st.score, 1.0)


class BandForTests(unittest.TestCase):
    def _st(self, score, samples=4):
        return ue.ExpertiseState(score=score, samples=samples, updated_at="t")

    def test_none_below_min_samples(self) -> None:
        self.assertIsNone(ue.band_for(self._st(0.9, samples=2)))
        self.assertIsNone(ue.band_for(None))

    def test_expert(self) -> None:
        self.assertEqual(ue.band_for(self._st(0.5)), ue.BAND_EXPERT)

    def test_novice(self) -> None:
        self.assertEqual(ue.band_for(self._st(-0.5)), ue.BAND_NOVICE)

    def test_familiar_middle(self) -> None:
        self.assertEqual(ue.band_for(self._st(0.0)), ue.BAND_FAMILIAR)


class RenderBlockTests(unittest.TestCase):
    def test_expert_line(self) -> None:
        out = ue.render_block(ue.BAND_EXPERT, "python", "Jacob")
        self.assertIn("Jacob", out)
        self.assertIn("peer-to-peer", out)
        self.assertIn("python", out)

    def test_novice_line(self) -> None:
        out = ue.render_block(ue.BAND_NOVICE, "python", "Jacob")
        self.assertIn("scaffold", out)

    def test_familiar_and_none_blank(self) -> None:
        self.assertEqual(ue.render_block(ue.BAND_FAMILIAR, "x", "Jacob"), "")
        self.assertEqual(ue.render_block(None, "x", "Jacob"), "")


class KvMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict[str, str] = {}

    def test_round_trip(self) -> None:
        m = {"1": ue.ExpertiseState(0.5, 4, "t")}
        ue.save_map(self.store.__setitem__, m)
        loaded = ue.load_map(self.store.get)
        self.assertEqual(loaded["1"].samples, 4)
        self.assertAlmostEqual(loaded["1"].score, 0.5)

    def test_garbage_tolerated(self) -> None:
        self.store[ue.KV_USER_EXPERTISE] = "not json"
        self.assertEqual(ue.load_map(self.store.get), {})


# ── provider fakes ───────────────────────────────────────────────────────


class _FakeEmbedder:
    def embed(self, text: str):
        return [1.0, 0.0, 0.0]


class _FakeGraph:
    persistent = True

    def __init__(self, *, match=None) -> None:
        self._match = match
        self.best_calls: list[dict] = []

    def best_clusters_for(self, qvec, *, top_n=1, min_sim=0.0):
        self.best_calls.append({"top_n": top_n, "min_sim": min_sim})
        return [self._match] if self._match else []


class _FakeDB:
    def __init__(self, kv=None) -> None:
        self.kv = dict(kv or {})

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = value


class _Agent:
    user_expertise_enabled = True


class _MemSettings:
    user_expertise_min_sim = 0.45
    user_expertise_learning_rate = 0.25
    user_expertise_min_samples = 4
    user_expertise_novice_threshold = -0.35
    user_expertise_expert_threshold = 0.35
    user_expertise_cooldown_turns = 12


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self, graph, db) -> None:
        self._settings = _Settings()
        self._memory_settings = _MemSettings()
        self._topic_graph = graph
        self._embedder = _FakeEmbedder()
        self._chat_db = db

    @property
    def user_display_name(self) -> str:
        return "Jacob"


class ProviderTests(unittest.TestCase):
    def _host(self, *, match, score, samples=4) -> _Host:
        db = _FakeDB()
        if match is not None:
            cid = str(int(match[0]))
            ue.save_map(
                db.kv_set, {cid: ue.ExpertiseState(score, samples, "t")}
            )
        return _Host(_FakeGraph(match=match), db)

    def test_expert_surfaces(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=0.6)
        out = host._render_user_expertise_block("tell me about async python")
        self.assertIn("peer-to-peer", out)

    def test_novice_surfaces(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=-0.6)
        out = host._render_user_expertise_block("tell me about async python")
        self.assertIn("scaffold", out)

    def test_familiar_blank(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=0.0)
        self.assertEqual(
            host._render_user_expertise_block("about python here"), ""
        )

    def test_insufficient_samples_blank(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=0.9, samples=2)
        self.assertEqual(
            host._render_user_expertise_block("about python here"), ""
        )

    def test_no_match_blank(self) -> None:
        host = self._host(match=None, score=0.0)
        self.assertEqual(
            host._render_user_expertise_block("random text here"), ""
        )

    def test_disabled_blank(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=0.6)
        host._settings.agent.user_expertise_enabled = False
        self.assertEqual(
            host._render_user_expertise_block("python stuff here"), ""
        )

    def test_short_text_blank(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=0.6)
        self.assertEqual(host._render_user_expertise_block("hi"), "")

    def test_cooldown_suppresses_next_turn(self) -> None:
        host = self._host(match=(1, "python", 0.8), score=0.6)
        first = host._render_user_expertise_block("about async python here")
        self.assertTrue(first)
        second = host._render_user_expertise_block("more python talk here")
        self.assertEqual(second, "")
        self.assertEqual(host._user_expertise_cooldown, 11)
        self.assertEqual(host._user_expertise_last["band"], ue.BAND_EXPERT)

    def test_force_bypasses_cooldown_and_min_sim(self) -> None:
        # Low-sim match + few samples that would normally be silent.
        host = self._host(match=(1, "python", 0.1), score=0.6, samples=1)
        host._user_expertise_cooldown = 4
        host._user_expertise_force_next = True
        out = host._render_user_expertise_block("python dinner tonight here")
        self.assertTrue(out)
        self.assertFalse(host._user_expertise_force_next)
        self.assertEqual(host._topic_graph.best_calls[-1]["min_sim"], 0.0)


if __name__ == "__main__":
    unittest.main()

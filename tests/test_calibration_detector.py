"""Tests for the K20 metacognitive calibration detector.

Covers all four halves of the module:

  - ``detect()`` — regex bands (strong / mild / affirmation) and the
    softening cosine+hedge AND-gate. Priority order verified
    (pushback beats affirmation when both match).
  - ``apply_signal()`` — global score delta + clamp, topic slot
    allocation / merge / eviction.
  - ``decay()`` — exponential drift toward baseline with topic
    multiplier; idempotent on a fresh state.
  - ``render_inner_life_block()`` — topic-specific cue wins over
    global cue; silent when both above threshold.

Pure unit tests: no LanceDB, no SQLite, no embedder. The few
embedding-sensitive tests build unit vectors directly so the cosine
comes out at a known value.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.affect import calibration_detector
from app.core.affect.calibration_detector import (
    CalibrationSignal,
    apply_signal,
    decay,
    detect,
    render_inner_life_block,
)
from app.core.affect.calibration_store import (
    CalibrationState,
    TopicSlot,
    baseline_state,
)


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _unit_vec(seed: int, dim: int = 8) -> np.ndarray:
    """Deterministic pseudo-random unit vector."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return (v / float(np.linalg.norm(v))).astype(np.float32)


# ── detect() ────────────────────────────────────────────────────────


class DetectStrongPushbackTests(unittest.TestCase):
    def test_are_you_sure_fires_strong(self):
        sig = detect(user_text="Wait, are you sure about that?")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_strong")
        self.assertLess(sig.delta, 0)

    def test_explicit_wrong_fires_strong(self):
        sig = detect(user_text="That's not right -- the capital is Lyon.")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_strong")

    def test_let_me_double_check_fires_strong(self):
        sig = detect(user_text="hmm let me double-check that")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_strong")

    def test_actually_correction_fires_strong(self):
        sig = detect(user_text="actually, it's not Madrid")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_strong")

    def test_short_text_returns_none(self):
        sig = detect(user_text="huh?")
        self.assertIsNone(sig)

    def test_empty_text_returns_none(self):
        self.assertIsNone(detect(user_text=""))
        self.assertIsNone(detect(user_text="   "))


class DetectMildPushbackTests(unittest.TestCase):
    def test_really_question_fires_mild(self):
        sig = detect(user_text="really??")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_mild")

    def test_im_not_sure_fires_mild(self):
        sig = detect(user_text="I'm not sure about that one")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_mild")

    def test_is_that_right_fires_mild(self):
        sig = detect(user_text="is that right?")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "pushback_mild")


class DetectAffirmationTests(unittest.TestCase):
    def test_youre_right_fires_affirmation(self):
        sig = detect(user_text="huh, you're right.")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "affirmation")
        self.assertGreater(sig.delta, 0)

    def test_good_call_fires_affirmation(self):
        sig = detect(user_text="oh, good call -- I missed that.")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "affirmation")

    def test_nice_catch_fires_affirmation(self):
        sig = detect(user_text="nice catch!")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "affirmation")


class DetectSofteningTests(unittest.TestCase):
    def test_hedge_plus_high_cosine_fires_softening(self):
        v = _unit_vec(1)
        sig = detect(
            user_text="so you're saying Lyon is the capital, right?",
            user_vec=v,
            prior_assistant_vec=v,  # identical -> cosine 1.0
            softening_cosine_threshold=0.70,
        )
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.kind, "softening")
        self.assertLess(sig.delta, 0)

    def test_hedge_but_low_cosine_silent(self):
        # Orthogonal unit vectors: cosine = 0.0 → below threshold.
        v1 = np.zeros(8, dtype=np.float32); v1[0] = 1.0
        v2 = np.zeros(8, dtype=np.float32); v2[1] = 1.0
        sig = detect(
            user_text="so you're saying that and also let's discuss apples?",
            user_vec=v1,
            prior_assistant_vec=v2,
            softening_cosine_threshold=0.70,
        )
        # Hedge token matches but cosine is 0.0 < 0.70 -> softening
        # path bails, no other regex matches -> None.
        self.assertIsNone(sig)

    def test_high_cosine_no_hedge_silent(self):
        # High cosine without a hedge token must NOT fire softening.
        v = _unit_vec(3)
        sig = detect(
            user_text="that's an interesting take on the matter overall",
            user_vec=v,
            prior_assistant_vec=v,
        )
        self.assertIsNone(sig)

    def test_missing_user_vec_silent_for_softening(self):
        v = _unit_vec(4)
        # Hedge token present but no user_vec -> softening can't fire,
        # and no other regex matches -> None.
        sig = detect(
            user_text="so you're saying we should go",
            user_vec=None,
            prior_assistant_vec=v,
        )
        self.assertIsNone(sig)


class DetectPriorityOrderTests(unittest.TestCase):
    def test_pushback_beats_affirmation_when_both_match(self):
        # "you're right, but are you sure about the date?" -- the
        # mild regex "are you sure" matches first via the strong band
        # ("are you sure (about) ...") so strong wins.
        sig = detect(user_text="you're right, but are you sure about the date?")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertIn(sig.kind, {"pushback_strong", "pushback_mild"})


# ── apply_signal() ──────────────────────────────────────────────────


class ApplySignalGlobalTests(unittest.TestCase):
    def test_negative_delta_drops_global_score(self):
        state = baseline_state(baseline=0.80)
        sig = CalibrationSignal(
            kind="pushback_strong",
            delta=-0.10,
            trigger_excerpt="are you sure",
        )
        new = apply_signal(state, signal=sig, assistant_vec=None, now=_now())
        self.assertAlmostEqual(new.global_score, 0.70, places=4)
        self.assertEqual(new.last_updated_at, _now())

    def test_positive_delta_raises_global_score_with_clamp(self):
        state = CalibrationState(
            global_score=0.98,
            last_updated_at=None,
            topics=tuple(),
        )
        sig = CalibrationSignal(
            kind="affirmation", delta=+0.04, trigger_excerpt="good call",
        )
        new = apply_signal(state, signal=sig, assistant_vec=None, now=_now())
        self.assertAlmostEqual(new.global_score, 1.00, places=4)

    def test_negative_delta_clamped_at_zero(self):
        state = CalibrationState(
            global_score=0.02,
            last_updated_at=None,
            topics=tuple(),
        )
        sig = CalibrationSignal(
            kind="pushback_strong", delta=-0.10, trigger_excerpt="x",
        )
        new = apply_signal(state, signal=sig, assistant_vec=None, now=_now())
        self.assertAlmostEqual(new.global_score, 0.00, places=4)


class ApplySignalTopicsTests(unittest.TestCase):
    def test_new_slot_allocated_when_no_topics(self):
        state = baseline_state(baseline=0.80)
        sig = CalibrationSignal(
            kind="pushback_strong", delta=-0.10, trigger_excerpt="x",
        )
        v = _unit_vec(10)
        new = apply_signal(
            state,
            signal=sig,
            assistant_vec=v,
            now=_now(),
            baseline=0.80,
            max_topic_slots=8,
        )
        self.assertEqual(len(new.topics), 1)
        self.assertAlmostEqual(new.topics[0].score, 0.70, places=4)
        self.assertEqual(new.topics[0].signal_count, 1)

    def test_existing_slot_merged_when_cosine_above_threshold(self):
        v = _unit_vec(20)
        existing = TopicSlot(
            centroid=v.copy(),
            score=0.75,
            last_signal_at=_now() - timedelta(days=1),
            signal_count=2,
        )
        state = CalibrationState(
            global_score=0.80,
            last_updated_at=_now() - timedelta(days=1),
            topics=(existing,),
        )
        sig = CalibrationSignal(
            kind="pushback_mild", delta=-0.05, trigger_excerpt="x",
        )
        new = apply_signal(
            state,
            signal=sig,
            assistant_vec=v,
            now=_now(),
            topic_merge_threshold=0.78,
        )
        self.assertEqual(len(new.topics), 1)
        self.assertAlmostEqual(new.topics[0].score, 0.70, places=4)
        self.assertEqual(new.topics[0].signal_count, 3)

    def test_new_slot_allocated_when_cosine_below_threshold(self):
        v1 = _unit_vec(30)
        v2 = _unit_vec(31)
        existing = TopicSlot(
            centroid=v1, score=0.55, last_signal_at=_now(), signal_count=1,
        )
        state = CalibrationState(
            global_score=0.80,
            last_updated_at=_now(),
            topics=(existing,),
        )
        sig = CalibrationSignal(
            kind="pushback_mild", delta=-0.05, trigger_excerpt="x",
        )
        new = apply_signal(
            state,
            signal=sig,
            assistant_vec=v2,
            now=_now(),
            topic_merge_threshold=0.95,  # forces unrelated v2 to allocate
        )
        self.assertEqual(len(new.topics), 2)

    def test_eviction_at_overflow_picks_closest_to_baseline(self):
        # Make the cap=3; load 3 slots; on the 4th, evict the one
        # closest to baseline.
        slots = (
            TopicSlot(
                centroid=_unit_vec(40), score=0.30,
                last_signal_at=_now() - timedelta(days=2), signal_count=2,
            ),
            TopicSlot(
                centroid=_unit_vec(41), score=0.78,  # closest to 0.80
                last_signal_at=_now() - timedelta(days=10), signal_count=1,
            ),
            TopicSlot(
                centroid=_unit_vec(42), score=0.20,
                last_signal_at=_now() - timedelta(days=1), signal_count=3,
            ),
        )
        state = CalibrationState(
            global_score=0.80,
            last_updated_at=_now(),
            topics=slots,
        )
        sig = CalibrationSignal(
            kind="pushback_strong", delta=-0.10, trigger_excerpt="x",
        )
        new_vec = _unit_vec(43)
        new = apply_signal(
            state,
            signal=sig,
            assistant_vec=new_vec,
            now=_now(),
            baseline=0.80,
            max_topic_slots=3,
            topic_merge_threshold=0.95,
        )
        self.assertEqual(len(new.topics), 3)
        scores = sorted(s.score for s in new.topics)
        # 0.78 (closest to baseline) should have been evicted
        self.assertNotIn(0.78, [round(s, 4) for s in scores])


# ── decay() ─────────────────────────────────────────────────────────


class DecayTests(unittest.TestCase):
    def test_decay_noop_when_last_updated_none(self):
        state = baseline_state(baseline=0.80)
        new = decay(state, now=_now(), half_life_days=5.0, baseline=0.80)
        self.assertIs(new, state)

    def test_decay_pulls_toward_baseline(self):
        state = CalibrationState(
            global_score=0.40,  # below baseline 0.80
            last_updated_at=_now() - timedelta(days=5),
            topics=tuple(),
        )
        new = decay(state, now=_now(), half_life_days=5.0, baseline=0.80)
        # After one half-life: gap halves
        # gap was 0.40, new gap should be 0.20, new score 0.60.
        self.assertAlmostEqual(new.global_score, 0.60, places=2)

    def test_topic_slot_decays_slower_than_global(self):
        v = _unit_vec(50)
        slot = TopicSlot(
            centroid=v, score=0.30, last_signal_at=_now(), signal_count=1,
        )
        state = CalibrationState(
            global_score=0.40,
            last_updated_at=_now() - timedelta(days=5),
            topics=(slot,),
        )
        new = decay(
            state,
            now=_now(),
            half_life_days=5.0,
            baseline=0.80,
            topic_half_life_multiplier=2.0,
        )
        # Global: gap halves -> 0.60
        # Topic: gap goes down by sqrt(0.5) -> ~ 0.444
        # Topic should be FURTHER from baseline than global
        global_gap = abs(new.global_score - 0.80)
        topic_gap = abs(new.topics[0].score - 0.80)
        self.assertGreater(topic_gap, global_gap)

    def test_decay_clamps_to_unit_interval(self):
        state = CalibrationState(
            global_score=0.50,
            last_updated_at=_now() - timedelta(days=100),
            topics=tuple(),
        )
        new = decay(state, now=_now(), half_life_days=5.0, baseline=0.80)
        # After many half-lives global should be very close to baseline
        self.assertAlmostEqual(new.global_score, 0.80, places=2)
        self.assertGreaterEqual(new.global_score, 0.0)
        self.assertLessEqual(new.global_score, 1.0)


# ── render_inner_life_block() ───────────────────────────────────────


class RenderTests(unittest.TestCase):
    def test_silent_when_above_thresholds(self):
        state = baseline_state(baseline=0.80)
        out = render_inner_life_block(
            state, global_threshold=0.55, topic_threshold=0.50,
        )
        self.assertIsNone(out)

    def test_global_cue_below_threshold(self):
        state = CalibrationState(
            global_score=0.40,
            last_updated_at=_now(),
            topics=tuple(),
        )
        out = render_inner_life_block(
            state,
            user_display_name="Jacob",
            global_threshold=0.55,
            topic_threshold=0.50,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Jacob", out)
        self.assertIn("double-checking", out)

    def test_topic_cue_wins_when_both_fire(self):
        v = _unit_vec(60)
        slot = TopicSlot(
            centroid=v, score=0.30, last_signal_at=_now(), signal_count=4,
        )
        state = CalibrationState(
            global_score=0.40,  # global also low
            last_updated_at=_now(),
            topics=(slot,),
        )
        out = render_inner_life_block(
            state,
            user_display_name="Jacob",
            global_threshold=0.55,
            topic_threshold=0.50,
        )
        self.assertIsNotNone(out)
        assert out is not None
        # Topic copy contains "around this topic"; global copy doesn't.
        self.assertIn("around this topic", out)

    def test_topic_cue_silent_when_topic_above_threshold(self):
        v = _unit_vec(61)
        slot = TopicSlot(
            centroid=v, score=0.70, last_signal_at=_now(), signal_count=1,
        )
        state = CalibrationState(
            global_score=0.80,
            last_updated_at=_now(),
            topics=(slot,),
        )
        out = render_inner_life_block(
            state,
            global_threshold=0.55,
            topic_threshold=0.50,
        )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()

"""Tests for the AffectState updater + ambient block formatter."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.affect.affect_state import (
    AffectState,
    AffectStore,
    AffectUpdater,
    felt_phrase,
    render_ambient_block,
)
from app.core.infra.chat_database import ChatDatabase


class _TempDb:
    def __enter__(self) -> ChatDatabase:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = ChatDatabase(Path(self._tmp.name) / "test.db")
        return self.db

    def __exit__(self, *exc) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self._tmp.cleanup()
        except Exception:
            pass


class AffectStoreTests(unittest.TestCase):
    def test_get_returns_default_when_missing(self) -> None:
        with _TempDb() as db:
            store = AffectStore(db)
            state = store.get("u1")
            self.assertEqual(state.user_id, "u1")
            self.assertEqual(state.mood_label, "content")
            self.assertAlmostEqual(state.valence, 0.0)
            self.assertAlmostEqual(state.arousal, 0.4)

    def test_save_then_get_roundtrips(self) -> None:
        with _TempDb() as db:
            store = AffectStore(db)
            state = AffectState(
                user_id="u1",
                valence=0.4,
                arousal=0.7,
                mood_label="warm",
                mood_intensity=0.8,
                valence_trend_24h=0.2,
                arousal_trend_24h=-0.1,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            store.save(state)
            roundtripped = store.get("u1")
            self.assertAlmostEqual(roundtripped.valence, 0.4)
            self.assertAlmostEqual(roundtripped.arousal, 0.7)
            self.assertEqual(roundtripped.mood_label, "warm")


class AffectUpdaterTests(unittest.TestCase):
    def test_excited_reaction_pushes_valence_up(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            state = updater.apply_turn(
                "u1", reaction="excited", user_text="that was awesome",
            )
            self.assertGreater(state.valence, 0.0)
            self.assertGreater(state.arousal, 0.4)

    def test_sad_reaction_pushes_valence_down(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            state = updater.apply_turn(
                "u1", reaction="sad", user_text="i'm tired",
            )
            self.assertLess(state.valence, 0.0)

    def test_user_hint_negative_compounds_with_reaction(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            base_state = updater.apply_turn(
                "u1", reaction="neutral", user_text="hi",
            )
            db2_state = updater.apply_turn(
                "u1", reaction="neutral", user_text="i'm so tired and stressed",
            )
            self.assertLess(db2_state.valence, base_state.valence)

    def test_mood_label_updates_with_state(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            for _ in range(6):
                state = updater.apply_turn(
                    "u1", reaction="excited", user_text="amazing",
                )
            # Sustained positive arousal/valence should land us in 'playful'
            # or 'curious' or 'warm' — the exact label depends on the
            # arousal magnitude reached after smoothing.
            self.assertIn(
                state.mood_label,
                {"playful", "curious", "warm", "tender"},
            )

    def test_decay_pulls_toward_baseline(self) -> None:
        with _TempDb() as db:
            store = AffectStore(db)
            # Pre-stash a high-valence state with a stale timestamp so the
            # decay path actually has time to act.
            stale_ts = (
                datetime.now(timezone.utc) - timedelta(minutes=120)
            ).isoformat()
            state = AffectState(
                user_id="u1",
                valence=0.8,
                arousal=0.8,
                baseline_valence=0.0,
                baseline_arousal=0.4,
                mood_label="playful",
                mood_intensity=0.8,
                updated_at=stale_ts,
            )
            store.save(state)
            updater = AffectUpdater(store)
            # neutral turn -> decay dominates the impulse
            new_state = updater.apply_turn(
                "u1", reaction="neutral", user_text="",
            )
            self.assertLess(new_state.valence, 0.6)
            self.assertLess(new_state.arousal, 0.7)


class EstimateUserAffectTests(unittest.TestCase):
    """K37: cheap (valence, arousal) estimate from per-turn signals."""

    def test_no_signal_returns_none(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        self.assertIsNone(
            estimate_user_affect(mood="unknown", energy="unknown"),
        )

    def test_low_mood_is_negative_valence(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        est = estimate_user_affect(mood="low", energy="unknown")
        self.assertIsNotNone(est)
        self.assertLess(est[0], 0.0)

    def test_high_mood_is_positive_valence_and_higher_arousal(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        est = estimate_user_affect(mood="high", energy="high")
        self.assertGreater(est[0], 0.0)
        self.assertGreater(est[1], 0.4)

    def test_vent_dialogue_act_pulls_negative(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        est = estimate_user_affect(dialogue_act="vent")
        self.assertIsNotNone(est)
        self.assertLess(est[0], 0.0)

    def test_banter_dialogue_act_pulls_positive(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        est = estimate_user_affect(dialogue_act="banter")
        self.assertGreater(est[0], 0.0)

    def test_confident_tone_arousal_only(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        from types import SimpleNamespace
        tone = SimpleNamespace(confident=True, arousal_hint=0.10)
        est = estimate_user_affect(tone=tone)
        self.assertIsNotNone(est)
        self.assertGreater(est[1], 0.4)

    def test_unconfident_tone_ignored(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        from types import SimpleNamespace
        tone = SimpleNamespace(confident=False, arousal_hint=0.10)
        self.assertIsNone(estimate_user_affect(tone=tone))

    def test_ranges_clamped(self) -> None:
        from app.core.affect.affect_state import estimate_user_affect
        est = estimate_user_affect(mood="high", energy="high", dialogue_act="banter")
        self.assertGreaterEqual(est[0], -1.0)
        self.assertLessEqual(est[0], 1.0)
        self.assertGreaterEqual(est[1], 0.0)
        self.assertLessEqual(est[1], 1.0)


class ContagionTests(unittest.TestCase):
    """K37: apply_turn tilts toward user affect, capped + gated."""

    def test_disabled_by_default_when_strength_zero(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            baseline = updater.apply_turn(
                "u1", reaction="neutral", user_text="hi",
            )
            with_user = updater.apply_turn(
                "u2", reaction="neutral", user_text="hi",
                user_affect=(-0.8, 0.4),  # strength defaults to 0.0
            )
            self.assertAlmostEqual(baseline.valence, with_user.valence, places=4)

    def test_negative_user_pulls_valence_down(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            no_cont = updater.apply_turn(
                "u1", reaction="neutral", user_text="ok",
            )
            with_cont = updater.apply_turn(
                "u2", reaction="neutral", user_text="ok",
                user_affect=(-0.8, 0.3),
                contagion_strength=0.15,
                contagion_max_per_turn=0.05,
            )
            self.assertLess(with_cont.valence, no_cont.valence)

    def test_positive_user_pulls_valence_up(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            no_cont = updater.apply_turn(
                "u1", reaction="neutral", user_text="ok",
            )
            with_cont = updater.apply_turn(
                "u2", reaction="neutral", user_text="ok",
                user_affect=(0.9, 0.7),
                contagion_strength=0.15,
                contagion_max_per_turn=0.05,
            )
            self.assertGreater(with_cont.valence, no_cont.valence)

    def test_move_is_capped(self) -> None:
        from app.core.affect.affect_state import _apply_user_contagion
        # Huge gap, but cap=0.05 limits the per-axis move.
        v, a = _apply_user_contagion(
            0.0, 0.4, (-1.0, 1.0), strength=0.9, cap=0.05,
        )
        self.assertAlmostEqual(v, -0.05, places=4)
        self.assertAlmostEqual(a, 0.45, places=4)

    def test_none_user_affect_is_noop(self) -> None:
        with _TempDb() as db:
            updater = AffectUpdater(AffectStore(db))
            a = updater.apply_turn(
                "u1", reaction="neutral", user_text="ok",
            )
            b = updater.apply_turn(
                "u2", reaction="neutral", user_text="ok",
                user_affect=None, contagion_strength=0.15,
            )
            self.assertAlmostEqual(a.valence, b.valence, places=4)


class AmbientBlockTests(unittest.TestCase):
    def test_block_includes_label_and_felt_phrase(self) -> None:
        # K44: label + felt-language, never raw floats.
        state = AffectState(
            user_id="u1",
            valence=0.4,
            arousal=0.3,
            mood_label="tender",
            valence_trend_24h=0.0,
        )
        text = render_ambient_block(state)
        self.assertIn("tender", text)
        self.assertIn(felt_phrase(0.4, 0.3), text)

    def test_block_contains_no_digits(self) -> None:
        # K44 contract: no numeric coordinates land in the prompt — for
        # any combination of scalars, including trend lines.
        for valence, arousal, trend in (
            (0.8, 0.9, 0.0),
            (0.15, 0.4, 0.3),
            (0.0, 0.5, -0.3),
            (-0.3, 0.2, 0.0),
            (-0.9, 0.95, 0.2),
        ):
            state = AffectState(
                user_id="u1",
                valence=valence,
                arousal=arousal,
                mood_label="content",
                valence_trend_24h=trend,
            )
            text = render_ambient_block(state)
            self.assertFalse(
                any(ch.isdigit() for ch in text),
                msg=f"digits leaked for v={valence} a={arousal}: {text!r}",
            )

    def test_trend_line_omitted_below_threshold(self) -> None:
        state = AffectState(
            user_id="u1",
            valence=0.3,
            arousal=0.3,
            mood_label="warm",
            valence_trend_24h=0.05,  # below default threshold 0.15
        )
        text = render_ambient_block(state)
        self.assertNotIn("Lately", text)

    def test_trend_line_present_above_threshold(self) -> None:
        state = AffectState(
            user_id="u1",
            valence=0.3,
            arousal=0.3,
            mood_label="warm",
            valence_trend_24h=0.25,
        )
        text = render_ambient_block(state)
        self.assertIn("Lately", text)


class FeltPhraseTests(unittest.TestCase):
    """K44: the (valence, arousal) -> felt-language band grid."""

    def test_grid_corners(self) -> None:
        # The four circumplex corners + dead centre map to the expected
        # direct emotion words.
        self.assertEqual(
            felt_phrase(0.9, 0.9), "genuinely excited, lots of energy",
        )
        self.assertEqual(
            felt_phrase(0.9, 0.1), "happy in a quiet, settled way",
        )
        self.assertEqual(felt_phrase(-0.9, 0.9), "upset and wound up")
        self.assertEqual(felt_phrase(-0.9, 0.1), "down and low on energy")
        self.assertEqual(felt_phrase(0.0, 0.5), "pretty even")

    def test_valence_band_boundaries(self) -> None:
        # Band edges (mid arousal column): <=-0.5 very_negative,
        # <=-0.15 negative, <0.15 neutral, <0.5 positive, >=0.5
        # very_positive.
        self.assertEqual(felt_phrase(-0.5, 0.5), "heavy-hearted")
        self.assertEqual(felt_phrase(-0.49, 0.5), "a little flat")
        self.assertEqual(felt_phrase(-0.15, 0.5), "a little flat")
        self.assertEqual(felt_phrase(-0.14, 0.5), "pretty even")
        self.assertEqual(felt_phrase(0.14, 0.5), "pretty even")
        self.assertEqual(felt_phrase(0.15, 0.5), "in a good mood")
        self.assertEqual(felt_phrase(0.49, 0.5), "in a good mood")
        self.assertEqual(felt_phrase(0.5, 0.5), "really good today")

    def test_arousal_band_boundaries(self) -> None:
        # Band edges (positive valence row): <0.35 low, <=0.65 mid,
        # >0.65 high.
        self.assertEqual(felt_phrase(0.3, 0.34), "mellow and content")
        self.assertEqual(felt_phrase(0.3, 0.35), "in a good mood")
        self.assertEqual(felt_phrase(0.3, 0.65), "in a good mood")
        self.assertEqual(felt_phrase(0.3, 0.66), "upbeat, a little buzzed")

    def test_out_of_range_clamps_to_outer_bands(self) -> None:
        self.assertEqual(
            felt_phrase(5.0, 5.0), "genuinely excited, lots of energy",
        )
        self.assertEqual(felt_phrase(-5.0, -5.0), "down and low on energy")

    def test_garbage_input_falls_back_to_neutral(self) -> None:
        self.assertEqual(felt_phrase(None, None), "pretty even")  # type: ignore[arg-type]

    def test_every_cell_is_nonempty_prose_without_digits(self) -> None:
        for valence in (-0.9, -0.3, 0.0, 0.3, 0.9):
            for arousal in (0.1, 0.5, 0.9):
                phrase = felt_phrase(valence, arousal)
                self.assertTrue(phrase.strip())
                self.assertFalse(any(ch.isdigit() for ch in phrase))


if __name__ == "__main__":
    unittest.main()

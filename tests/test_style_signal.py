"""Tests for :mod:`app.core.persona.style_signal` (K13 stylometric mirror).

Pure rolling-window analyzer -- no embedder, no LLM -- so the tests just
feed scripted user-text streams and assert per-axis feature extraction,
the deviation gate, warmup, window roll, persistence round-trip,
settings-disabled path, and the lazy cross-session warm.

The deviation tests carry most of the weight. K13 previously bucketed
each axis against an absolute bar, which on a stable writer can only
emit a constant: over 2018 real user turns it rendered on 99.7% of them
and changed what it said four times in twelve weeks. What the suite has
to protect now is the opposite property -- that writing normally
produces *nothing*, and only a departure from his own baseline speaks.
"""
from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace

from app.core.persona.style_signal import (
    _STATE_VERSION,
    StyleSignal,
    StyleSignalAnalyzer,
    StyleSignalStore,
    _extract_features,
    render_inner_life_block,
)


# ── stub helpers ────────────────────────────────────────────────────


def _settings(**overrides: object) -> SimpleNamespace:
    """Compact ``AgentSettings`` stub via ``SimpleNamespace`` getattr."""
    base: dict[str, object] = dict(
        style_signal_enabled=True,
        style_signal_window=30,
        style_signal_warmup_min=8,
        style_signal_sensitivity=3.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(**overrides: object) -> StyleSignalAnalyzer:
    return StyleSignalAnalyzer(agent_settings=_settings(**overrides))


def _feed(analyzer: StyleSignalAnalyzer, text: str, n: int) -> None:
    for _ in range(n):
        analyzer.record_user_turn(text)


#: Enough turns to clear the baseline minimum with room to spare.
_BASELINE = 120

#: A settled writer: capitalised, punctuated, ~14 words, no emoticon.
_USUAL = "I think that sounds about right and we can try it later today."


def _signal(**axes: float) -> StyleSignal:
    """A signal with explicit deviations and don't-care window means."""
    return StyleSignal(
        terseness=0.5,
        punctuation=0.5,
        playfulness=0.0,
        slang=0.0,
        question=0.0,
        window_size=30,
        baseline_turns=_BASELINE,
        deviations=dict(axes),
    )


# ── feature extraction (per axis) ───────────────────────────────────


class ExtractFeaturesTests(unittest.TestCase):
    def test_short_input_is_terse(self) -> None:
        f = _extract_features("yeah for sure")
        self.assertGreater(f.terseness, 0.55)

    def test_long_input_is_chatty(self) -> None:
        f = _extract_features(" ".join(["really"] * 40))
        self.assertLess(f.terseness, 0.30)

    def test_punctuation_capitalised_with_terminator(self) -> None:
        f = _extract_features("Hello there. This is a sentence.")
        self.assertEqual(f.punctuation, 1.0)

    def test_punctuation_zero_lowercase_unterminated(self) -> None:
        f = _extract_features("hey lol no big deal")
        self.assertEqual(f.punctuation, 0.0)

    def test_punctuation_half_credit_capital_only(self) -> None:
        f = _extract_features("Hello there")
        self.assertEqual(f.punctuation, 0.5)

    def test_punctuation_half_credit_terminator_only(self) -> None:
        f = _extract_features("hello there.")
        self.assertEqual(f.punctuation, 0.5)

    def test_question_when_ends_with_question_mark(self) -> None:
        self.assertEqual(_extract_features("are you sure?").question, 1.0)

    def test_question_zero_for_statement(self) -> None:
        self.assertEqual(_extract_features("just a statement").question, 0.0)

    def test_slang_is_per_turn_incidence(self) -> None:
        """Not per *word*. As a density the axis needed 15% of every
        word in the window to be slang; the real corpus peaked at 0.9%
        and the label was unreachable by construction."""
        one_marker_in_twelve = "yeah " + " ".join(["word"] * 11)
        self.assertEqual(_extract_features(one_marker_in_twelve).slang, 1.0)

    def test_slang_zero_for_neutral_text(self) -> None:
        self.assertEqual(_extract_features("the weather is nice today").slang, 0.0)


class PlayfulnessTests(unittest.TestCase):
    """The axis was blind, not quiet.

    Zero of 2018 real user turns contained a Unicode emoji and 47.8%
    contained an ASCII emoticon, so an emoji-only detector measured
    nothing and could not have measured anything.
    """

    def test_unicode_emoji_counts(self) -> None:
        self.assertEqual(
            _extract_features("hello world \U0001F600").playfulness, 1.0,
        )

    def test_ascii_emoticons_count(self) -> None:
        for text in (
            "I am looking forward to it :p pulling you closer",
            "Aww :3 gladly",
            "sure thing :)",
            "oh no :(",
            "haha xD",
            "that's great :D",
            "love it <3",
            "yay ^_^",
        ):
            with self.subTest(text):
                self.assertEqual(_extract_features(text).playfulness, 1.0)

    def test_plain_prose_is_not_playful(self) -> None:
        self.assertEqual(_extract_features("just a normal sentence").playfulness, 0.0)

    def test_punctuation_and_paths_are_not_emoticons(self) -> None:
        """The eye character has to be unattached, or half his writing
        reads as playful: timestamps, Windows paths and URLs all put a
        colon next to a bracket-ish character."""
        for text in (
            "let's meet at 12:30 tomorrow",
            "it is in C:\\src\\app somewhere",
            "see http://example.com/page",
            "the ratio was 3:2 in the end",
            "one note: (we can revisit this)",
            "the matrix (x) is fine",
        ):
            with self.subTest(text):
                self.assertEqual(_extract_features(text).playfulness, 0.0)


# ── the deviation gate ──────────────────────────────────────────────


class DeviationLabelTests(unittest.TestCase):
    def test_a_settled_writer_says_nothing(self) -> None:
        """The whole repair in one assertion. Writing the way he always
        writes must produce no block at all."""
        analyzer = _build()
        _feed(analyzer, _USUAL, _BASELINE)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertEqual(analyzer.labels_for_signal(signal), [])

    def test_going_terse_is_noticed(self) -> None:
        # Capitalised and punctuated, and not a slang marker, so
        # terseness is the only axis that moves.
        analyzer = _build()
        _feed(analyzer, _USUAL, _BASELINE)
        _feed(analyzer, "Right.", 30)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertIn("terser than usual", analyzer.labels_for_signal(signal))

    def test_going_long_form_is_noticed(self) -> None:
        """Both directions carry information -- him opening up is as
        much a register change as him going quiet."""
        analyzer = _build()
        _feed(analyzer, "ok sure", _BASELINE)
        _feed(analyzer, " ".join(["word"] * 60), 30)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertIn(
            "more long-form than usual", analyzer.labels_for_signal(signal),
        )

    def test_dropping_his_usual_capitals_is_noticed(self) -> None:
        analyzer = _build()
        _feed(analyzer, _USUAL, _BASELINE)
        _feed(analyzer, "sounds about right and we can try it later today", 30)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertIn(
            "looser punctuation than usual", analyzer.labels_for_signal(signal),
        )

    def test_a_binary_axis_can_actually_fire(self) -> None:
        """Regression on the yardstick. Scoring a 30-sample mean against
        the *per-turn* standard deviation -- ~0.5 on a 0/1 axis -- made
        every axis unfireable; the first cut of this rewrite spoke on
        0.0% of the corpus. The denominator must be the standard error.
        """
        analyzer = _build()
        _feed(analyzer, "that sounds about right to me and I agree", _BASELINE)
        _feed(analyzer, "that sounds about right to me and I agree :D", 30)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertIn(
            "more playful markers than usual",
            analyzer.labels_for_signal(signal),
        )

    def test_silent_until_the_baseline_is_real(self) -> None:
        """Below the baseline minimum there are no deviations to bucket,
        so a fresh install says nothing rather than guessing."""
        analyzer = _build()
        _feed(analyzer, _USUAL, 20)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertEqual(signal.deviations, {})
        self.assertEqual(analyzer.labels_for_signal(signal), [])

    def test_sensitivity_setting_is_honoured(self) -> None:
        analyzer = _build(style_signal_sensitivity=99.0)
        _feed(analyzer, _USUAL, _BASELINE)
        _feed(analyzer, "ok", 30)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertEqual(analyzer.labels_for_signal(signal), [])


class LabelSelectionTests(unittest.TestCase):
    def test_strongest_deviation_comes_first(self) -> None:
        labels = _signal(question=4.0, terseness=9.0).labels()
        self.assertEqual(labels[0], "terser than usual")

    def test_capped_so_the_line_stays_a_sentence(self) -> None:
        """The corpus produces up to five simultaneous labels; a
        register nudge that lists five things is a report."""
        labels = _signal(
            terseness=9.0, punctuation=8.0, playfulness=7.0,
            slang=6.0, question=5.0,
        ).labels()
        self.assertEqual(len(labels), 2)

    def test_direction_picks_the_right_phrase(self) -> None:
        self.assertEqual(_signal(playfulness=-5.0).labels(), ["drier than usual"])
        self.assertEqual(
            _signal(playfulness=5.0).labels(),
            ["more playful markers than usual"],
        )

    def test_no_deviation_no_labels(self) -> None:
        self.assertEqual(_signal(terseness=0.4, punctuation=-1.2).labels(), [])

    def test_ties_break_deterministically(self) -> None:
        """Same state must always render the same string, or the block
        churns the prompt cache for no reason."""
        first = _signal(terseness=5.0, question=5.0).labels()
        second = _signal(question=5.0, terseness=5.0).labels()
        self.assertEqual(first, second)


class BaselineColdStartTests(unittest.TestCase):
    def test_the_baseline_reaches_the_true_mean_quickly(self) -> None:
        """An EWMA seeded at 0.0 converges far too slowly to be usable:
        at alpha=1/300 it sits at 49% of the truth after 200 turns, so
        every axis would read "higher than usual" for months. The
        effective rate is floored at 1/count to keep it an exact running
        mean until the decay horizon is reached.
        """
        analyzer = _build()
        _feed(analyzer, "Hello there.", 200)

        baseline = analyzer._baselines["punctuation"]
        self.assertAlmostEqual(baseline.mean, 1.0, places=3)

    def test_a_constant_axis_does_not_fire_on_its_own_baseline(self) -> None:
        analyzer = _build()
        _feed(analyzer, "Hello there.", 200)

        signal = analyzer.current_signal()
        assert signal is not None
        self.assertAlmostEqual(signal.deviations["punctuation"], 0.0, places=3)


# ── warmup gate ─────────────────────────────────────────────────────


class WarmupTests(unittest.TestCase):
    def test_returns_none_below_warmup(self) -> None:
        analyzer = _build(style_signal_warmup_min=8)
        _feed(analyzer, "hello there friend", 5)
        self.assertIsNone(analyzer.current_signal())

    def test_returns_signal_at_warmup(self) -> None:
        analyzer = _build(style_signal_warmup_min=8)
        _feed(analyzer, "hello there friend", 8)
        signal = analyzer.current_signal()
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.window_size, 8)

    def test_empty_input_does_not_advance_window(self) -> None:
        analyzer = _build()
        analyzer.record_user_turn("")
        analyzer.record_user_turn("   ")
        self.assertEqual(analyzer.window_size(), 0)
        self.assertEqual(analyzer.baseline_turns(), 0)


# ── window roll ─────────────────────────────────────────────────────


class WindowRollTests(unittest.TestCase):
    def test_31st_turn_evicts_oldest_in_30_window(self) -> None:
        analyzer = _build(style_signal_window=30)
        _feed(analyzer, "just a normal sentence", 30)
        self.assertEqual(analyzer.window_size(), 30)
        analyzer.record_user_turn("oh nice")
        self.assertEqual(analyzer.window_size(), 30)

    def test_window_capped_to_setting(self) -> None:
        analyzer = _build(style_signal_window=5)
        for i in range(20):
            analyzer.record_user_turn(f"turn number {i}")
        self.assertEqual(analyzer.window_size(), 5)

    def test_baseline_outlives_the_window(self) -> None:
        """"Usual" has to mean more than the last half hour, so the
        baseline keeps counting after the ring starts evicting."""
        analyzer = _build(style_signal_window=5)
        _feed(analyzer, "turn number one", 40)
        self.assertEqual(analyzer.window_size(), 5)
        self.assertEqual(analyzer.baseline_turns(), 40)

    def test_recent_word_counts_exposes_window(self) -> None:
        # K14 consumes this method; assert the order + lengths line up
        # with the rolling buffer.
        analyzer = _build(style_signal_window=4)
        analyzer.record_user_turn("one")            # 1 word
        analyzer.record_user_turn("one two")        # 2 words
        analyzer.record_user_turn("one two three")  # 3 words
        counts = analyzer.recent_word_counts()
        self.assertEqual(counts, [1, 2, 3])
        # Mutating the returned list must not affect the analyzer.
        counts.append(999)
        self.assertEqual(analyzer.recent_word_counts(), [1, 2, 3])


# ── cross-session warm ──────────────────────────────────────────────


class CrossSessionWarmTests(unittest.TestCase):
    def test_warm_from_history_only_user_rows(self) -> None:
        analyzer = _build(style_signal_warmup_min=2)
        history = [
            ("user", "yo"),
            ("assistant", "hi"),
            ("user", "you good?"),
            ("assistant", "yeah"),
            ("user", "nice"),
        ]
        analyzer.warm_from_history(history)
        # Only 3 user rows should have landed in the window.
        self.assertEqual(analyzer.window_size(), 3)

    def test_warm_is_idempotent(self) -> None:
        analyzer = _build(style_signal_warmup_min=2)
        history = [("user", "first"), ("user", "second"), ("user", "third")]
        analyzer.warm_from_history(history)
        first_size = analyzer.window_size()
        analyzer.warm_from_history(history)
        self.assertEqual(analyzer.window_size(), first_size)
        self.assertTrue(analyzer.is_warmed())

    def test_warm_matches_turn_by_turn_recording(self) -> None:
        history = [
            ("user", "casual lowercase chat"),
            ("user", "yeah lol idk maybe"),
            ("user", "just chilling here"),
            ("user", "wanna play later?"),
            ("user", "ok cool"),
            ("user", "thinking about food"),
            ("user", "imo dinner now"),
            ("user", "yo wassup"),
        ]
        warmed = _build(style_signal_warmup_min=2)
        warmed.warm_from_history(history)
        sequential = _build(style_signal_warmup_min=2)
        for _, content in history:
            sequential.record_user_turn(content)
        s1 = warmed.current_signal()
        s2 = sequential.current_signal()
        assert s1 is not None and s2 is not None
        for axis in ("terseness", "punctuation", "playfulness", "slang", "question"):
            with self.subTest(axis):
                self.assertAlmostEqual(
                    getattr(s1, axis), getattr(s2, axis), places=6,
                )


# ── persistence round-trip ──────────────────────────────────────────


class PersistenceRoundTripTests(unittest.TestCase):
    def test_to_dict_from_dict_preserves_state(self) -> None:
        analyzer = _build()
        _feed(analyzer, _USUAL, _BASELINE)
        snapshot = analyzer.current_signal()
        assert snapshot is not None

        restored = _build()
        restored.from_dict(analyzer.to_dict())
        restored_signal = restored.current_signal()
        assert restored_signal is not None
        self.assertEqual(restored.window_size(), analyzer.window_size())
        self.assertEqual(restored.baseline_turns(), analyzer.baseline_turns())
        self.assertAlmostEqual(
            restored_signal.terseness, snapshot.terseness, places=6,
        )

    def test_the_baseline_survives_a_restart(self) -> None:
        """If it did not, every restart would re-run the cold start and
        the block would spend a day calling normal writing unusual."""
        analyzer = _build()
        _feed(analyzer, _USUAL, _BASELINE)

        restored = _build()
        restored.from_dict(analyzer.to_dict())
        signal = restored.current_signal()
        assert signal is not None
        self.assertEqual(restored.labels_for_signal(signal), [])

    def test_state_from_an_older_build_is_discarded(self) -> None:
        """The old blob stored per-word densities under keys this build
        reads as incidence rates. Restoring it would seed the baseline
        with values that can never recur, so a version mismatch re-warms
        from history instead of half-loading."""
        analyzer = _build()
        analyzer.from_dict({
            "warmed": True,
            "window": [{
                "terseness": 0.9, "formality": 0.9, "emoji_density": 0.9,
                "slang_density": 0.9, "is_question": 0.9, "word_count": 3,
            }] * 10,
        })
        self.assertEqual(analyzer.window_size(), 0)
        self.assertFalse(analyzer.is_warmed())

    def test_from_dict_handles_garbage_gracefully(self) -> None:
        analyzer = _build()
        analyzer.from_dict(None)  # type: ignore[arg-type]
        analyzer.from_dict({"version": _STATE_VERSION, "window": "nope"})  # type: ignore[arg-type]
        analyzer.from_dict({"version": _STATE_VERSION, "window": ["nope", 42, None]})
        analyzer.from_dict({"version": _STATE_VERSION, "baselines": "nope"})
        self.assertEqual(analyzer.window_size(), 0)


# ── store (SQLite UPSERT round-trip) ────────────────────────────────


class StyleSignalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # Tiny in-memory DB with just the user_style_signal schema.
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS user_style_signal ("
            "user_id TEXT PRIMARY KEY, "
            "signal_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        self.conn.commit()

        class _DB:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn

            def execute_fetchone(self, sql, params=()):  # type: ignore[no-untyped-def]
                row = self._conn.execute(sql, params).fetchone()
                return tuple(row) if row is not None else None

            def execute_commit(self, sql, params=()):  # type: ignore[no-untyped-def]
                self._conn.execute(sql, params)
                self._conn.commit()

        self.db = _DB(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_load_returns_none_on_miss(self) -> None:
        store = StyleSignalStore(self.db)
        self.assertIsNone(store.load("jacob"))

    def test_upsert_then_load_round_trip(self) -> None:
        store = StyleSignalStore(self.db)
        analyzer = _build()
        _feed(analyzer, "hello world friend", 8)
        payload = analyzer.to_dict()
        store.upsert("jacob", payload)
        loaded = store.load("jacob")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.get("warmed"), payload.get("warmed"))
        self.assertEqual(
            len(loaded.get("window") or []),
            len(payload.get("window") or []),
        )

    def test_the_blob_stays_small(self) -> None:
        """The whole state is UPSERTed on every user turn, which is why
        the baseline is five mean/variance pairs and not a second
        several-hundred-entry ring."""
        import json

        analyzer = _build()
        _feed(analyzer, _USUAL, 500)
        self.assertLess(len(json.dumps(analyzer.to_dict())), 4096)

    def test_upsert_overwrites_existing_row(self) -> None:
        store = StyleSignalStore(self.db)
        store.upsert("jacob", {"window": [], "warmed": False})
        store.upsert("jacob", {"window": [], "warmed": True})
        loaded = store.load("jacob")
        assert loaded is not None
        self.assertTrue(loaded.get("warmed"))


# ── render block ────────────────────────────────────────────────────


class RenderTests(unittest.TestCase):
    def test_none_signal_returns_empty(self) -> None:
        self.assertEqual(render_inner_life_block(None, []), "")

    def test_empty_labels_returns_empty(self) -> None:
        self.assertEqual(render_inner_life_block(_signal(), []), "")

    def test_renders_one_line_with_labels(self) -> None:
        out = render_inner_life_block(
            _signal(terseness=5.0),
            ["terser than usual", "drier than usual"],
            user_display_name="Jacob",
        )
        self.assertEqual(
            out,
            "How Jacob is writing today: terser than usual, drier than usual.",
        )


# ── settings-disabled path ──────────────────────────────────────────


class SettingsDisabledTests(unittest.TestCase):
    def test_no_settings_uses_module_defaults(self) -> None:
        # Construct with no agent_settings stub at all -- module-level
        # defaults must keep the analyzer healthy (no AttributeError).
        analyzer = StyleSignalAnalyzer()
        _feed(analyzer, "hello there friend", 8)
        self.assertIsNotNone(analyzer.current_signal())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

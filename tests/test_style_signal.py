"""Tests for :mod:`app.core.style_signal` (K13 stylometric mirror).

Pure rolling-window analyzer -- no embedder, no LLM -- so the tests
just feed scripted user-text streams and assert per-axis feature
extraction, bucketing edges, warmup gate, window roll, persistence
round-trip, settings-disabled path, and the lazy cross-session warm.
"""
from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace

from app.core.style_signal import (
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
        style_signal_terse_threshold=0.55,
        style_signal_formal_threshold=0.55,
        style_signal_emoji_threshold=0.05,
        style_signal_slang_threshold=0.15,
        style_signal_question_threshold=0.40,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(**overrides: object) -> StyleSignalAnalyzer:
    return StyleSignalAnalyzer(agent_settings=_settings(**overrides))


# ── feature extraction (per axis) ───────────────────────────────────


class ExtractFeaturesTests(unittest.TestCase):
    def test_short_input_is_terse(self) -> None:
        # 3 words -> terseness ~0.73 (well above the 0.55 high band).
        f = _extract_features("yeah for sure")
        self.assertGreater(f.terseness, 0.55)

    def test_long_input_is_chatty(self) -> None:
        # 40 words -> terseness ~0.17 (well below the 0.45 low band).
        text = " ".join(["really"] * 40)
        f = _extract_features(text)
        self.assertLess(f.terseness, 0.30)

    def test_formal_capitalised_with_terminator(self) -> None:
        f = _extract_features("Hello there. This is a sentence.")
        self.assertEqual(f.formality, 1.0)

    def test_casual_lowercase_no_terminator(self) -> None:
        f = _extract_features("hey lol no big deal")
        self.assertEqual(f.formality, 0.0)

    def test_partial_formality_capital_only(self) -> None:
        f = _extract_features("Hello there")
        self.assertEqual(f.formality, 0.5)

    def test_partial_formality_terminator_only(self) -> None:
        f = _extract_features("hello there.")
        self.assertEqual(f.formality, 0.5)

    def test_emoji_density_basic(self) -> None:
        # 4 tokens (hello, world, emoji1, emoji2), 2 emojis -> density
        # = 2/4 = 0.5. Cap is 1.0; we assert above-zero behaviour.
        f = _extract_features("hello world \U0001F600 \U0001F389")
        self.assertEqual(f.emoji_density, 0.5)

    def test_emoji_density_zero_when_no_emoji(self) -> None:
        f = _extract_features("just a normal sentence")
        self.assertEqual(f.emoji_density, 0.0)

    def test_slang_density_counts_closed_list_only(self) -> None:
        # "yeah" + "lol" + "idk" out of 6 words -> 3/6 = 0.5.
        f = _extract_features("yeah lol idk maybe later today")
        self.assertGreater(f.slang_density, 0.4)

    def test_slang_density_zero_for_neutral_text(self) -> None:
        f = _extract_features("the weather is nice today")
        self.assertEqual(f.slang_density, 0.0)

    def test_is_question_when_ends_with_question_mark(self) -> None:
        f = _extract_features("are you sure?")
        self.assertEqual(f.is_question, 1.0)

    def test_is_question_zero_for_statement(self) -> None:
        f = _extract_features("just a statement")
        self.assertEqual(f.is_question, 0.0)


# ── bucketing edges ─────────────────────────────────────────────────


class BucketingTests(unittest.TestCase):
    def test_terse_label_when_above_threshold(self) -> None:
        signal = StyleSignal(
            terseness=0.60,
            formality=0.5,
            emoji_density=0.0,
            slang_density=0.0,
            question_rate=0.0,
            window_size=10,
        )
        self.assertIn("terse", signal.labels())

    def test_chatty_label_when_below_low_threshold(self) -> None:
        signal = StyleSignal(
            terseness=0.30,
            formality=0.5,
            emoji_density=0.0,
            slang_density=0.0,
            question_rate=0.0,
            window_size=10,
        )
        self.assertIn("chatty", signal.labels())

    def test_no_label_in_deadzone(self) -> None:
        # Terseness=0.50 falls in the [0.45, 0.55] deadzone -> no
        # label. Same for formality at 0.50.
        signal = StyleSignal(
            terseness=0.50,
            formality=0.50,
            emoji_density=0.0,
            slang_density=0.0,
            question_rate=0.0,
            window_size=10,
        )
        self.assertEqual(signal.labels(), [])

    def test_emoji_label_only_when_high(self) -> None:
        # Emoji density of 0.04 -> below 0.05 threshold -> no label.
        signal_low = StyleSignal(
            terseness=0.50,
            formality=0.50,
            emoji_density=0.04,
            slang_density=0.0,
            question_rate=0.0,
            window_size=10,
        )
        self.assertEqual(signal_low.labels(), [])
        # 0.05 -> exactly threshold -> high label.
        signal_high = StyleSignal(
            terseness=0.50,
            formality=0.50,
            emoji_density=0.05,
            slang_density=0.0,
            question_rate=0.0,
            window_size=10,
        )
        self.assertIn("emoji-heavy", signal_high.labels())

    def test_question_rate_label_at_threshold(self) -> None:
        signal = StyleSignal(
            terseness=0.50,
            formality=0.50,
            emoji_density=0.0,
            slang_density=0.0,
            question_rate=0.40,
            window_size=10,
        )
        self.assertIn("asks back often", signal.labels())

    def test_label_order_is_stable(self) -> None:
        # Multiple labels must come out in the documented order:
        # terse/chatty, formal/casual, emoji, slang, question.
        signal = StyleSignal(
            terseness=0.70,
            formality=0.70,
            emoji_density=0.10,
            slang_density=0.30,
            question_rate=0.50,
            window_size=10,
        )
        labels = signal.labels()
        self.assertEqual(
            labels,
            ["terse", "formal", "emoji-heavy", "slang-heavy", "asks back often"],
        )


# ── warmup gate ─────────────────────────────────────────────────────


class WarmupTests(unittest.TestCase):
    def test_returns_none_below_warmup(self) -> None:
        analyzer = _build(style_signal_warmup_min=8)
        for _ in range(5):
            analyzer.record_user_turn("hello there friend")
        self.assertIsNone(analyzer.current_signal())

    def test_returns_signal_at_warmup(self) -> None:
        analyzer = _build(style_signal_warmup_min=8)
        for _ in range(8):
            analyzer.record_user_turn("hello there friend")
        signal = analyzer.current_signal()
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.window_size, 8)

    def test_empty_input_does_not_advance_window(self) -> None:
        analyzer = _build()
        analyzer.record_user_turn("")
        analyzer.record_user_turn("   ")
        self.assertEqual(analyzer.window_size(), 0)


# ── window roll ─────────────────────────────────────────────────────


class WindowRollTests(unittest.TestCase):
    def test_31st_turn_evicts_oldest_in_30_window(self) -> None:
        analyzer = _build(style_signal_window=30)
        for _ in range(30):
            analyzer.record_user_turn("just a normal sentence")
        self.assertEqual(analyzer.window_size(), 30)
        analyzer.record_user_turn("oh nice")
        self.assertEqual(analyzer.window_size(), 30)

    def test_window_capped_to_setting(self) -> None:
        analyzer = _build(style_signal_window=5)
        for i in range(20):
            analyzer.record_user_turn(f"turn number {i}")
        self.assertEqual(analyzer.window_size(), 5)

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
        history = [
            ("user", "first"),
            ("user", "second"),
            ("user", "third"),
        ]
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
        self.assertAlmostEqual(s1.terseness, s2.terseness, places=6)
        self.assertAlmostEqual(s1.formality, s2.formality, places=6)
        self.assertAlmostEqual(s1.emoji_density, s2.emoji_density, places=6)
        self.assertAlmostEqual(s1.slang_density, s2.slang_density, places=6)
        self.assertAlmostEqual(s1.question_rate, s2.question_rate, places=6)


# ── persistence round-trip ──────────────────────────────────────────


class PersistenceRoundTripTests(unittest.TestCase):
    def test_to_dict_from_dict_preserves_state(self) -> None:
        analyzer = _build()
        for content in [
            "yeah lol", "idk really", "just chilling",
            "wanna come?", "yo what's up", "tbh maybe",
            "ok cool", "alright man",
        ]:
            analyzer.record_user_turn(content)
        snapshot = analyzer.current_signal()
        assert snapshot is not None

        blob = analyzer.to_dict()
        restored = _build()
        restored.from_dict(blob)
        restored_signal = restored.current_signal()
        assert restored_signal is not None
        self.assertEqual(restored.window_size(), analyzer.window_size())
        self.assertAlmostEqual(restored_signal.terseness, snapshot.terseness, places=6)
        self.assertAlmostEqual(restored_signal.formality, snapshot.formality, places=6)
        self.assertAlmostEqual(restored_signal.slang_density, snapshot.slang_density, places=6)

    def test_from_dict_handles_garbage_gracefully(self) -> None:
        analyzer = _build()
        analyzer.from_dict(None)  # type: ignore[arg-type]
        analyzer.from_dict({"window": "not a list"})  # type: ignore[arg-type]
        # Non-dict row entries ("nope") and bogus-but-dict-shaped rows
        # should both not crash. A dict-shaped row with no recognized
        # keys round-trips to a default _TurnFeatures (all zeros) by
        # design -- restore is best-effort, not strict-validating.
        analyzer.from_dict({"window": ["nope", 42, None]})
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
        for _ in range(8):
            analyzer.record_user_turn("hello world friend")
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
        signal = StyleSignal(
            terseness=0.5,
            formality=0.5,
            emoji_density=0.0,
            slang_density=0.0,
            question_rate=0.0,
            window_size=10,
        )
        self.assertEqual(render_inner_life_block(signal, []), "")

    def test_renders_one_line_with_labels(self) -> None:
        signal = StyleSignal(
            terseness=0.7,
            formality=0.3,
            emoji_density=0.0,
            slang_density=0.20,
            question_rate=0.0,
            window_size=10,
        )
        labels = ["terse", "casual", "slang-heavy"]
        out = render_inner_life_block(
            signal, labels, user_display_name="Jacob",
        )
        self.assertEqual(
            out,
            "How Jacob writes lately: terse, casual, slang-heavy.",
        )


# ── settings-disabled path ──────────────────────────────────────────


class SettingsDisabledTests(unittest.TestCase):
    def test_no_settings_uses_module_defaults(self) -> None:
        # Construct with no agent_settings stub at all -- module-level
        # defaults must keep the analyzer healthy (no AttributeError).
        analyzer = StyleSignalAnalyzer()
        for _ in range(8):
            analyzer.record_user_turn("hello there friend")
        self.assertIsNotNone(analyzer.current_signal())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

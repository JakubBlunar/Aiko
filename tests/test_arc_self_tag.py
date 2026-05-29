"""H1 tests: arc self-tag parser, store consumer, hot-path guard."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.chat_database import ChatDatabase
from app.core.conversation_arc import (
    ArcEstimator,
    ArcStore,
    VALID_ARCS,
)
from app.core.services.response_text_service import (
    parse_arc_tags,
    strip_all_meta_tags,
)


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = ArcStore(self.db)

    def close(self) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class ArcTagParserTests(unittest.TestCase):
    def test_parse_single_tag(self) -> None:
        out = parse_arc_tags("[[reaction:gentle]] yeah I hear you. [[arc:support]]")
        self.assertEqual(out, ["support"])

    def test_parse_multiple_tags_keeps_order(self) -> None:
        out = parse_arc_tags("[[arc:silly]] hm wait [[arc:reflection]]")
        self.assertEqual(out, ["silly", "reflection"])

    def test_parse_empty_text(self) -> None:
        self.assertEqual(parse_arc_tags(""), [])
        self.assertEqual(parse_arc_tags(None), [])  # type: ignore[arg-type]

    def test_parse_lowercases(self) -> None:
        out = parse_arc_tags("[[ARC:Support]]")
        self.assertEqual(out, ["support"])

    def test_parse_unknown_value_passes_through(self) -> None:
        # Validation against VALID_ARCS happens at the dispatch site so
        # the parser stays cheap; an unknown value is still returned and
        # the caller filters it out.
        out = parse_arc_tags("[[arc:debug]] [[arc:support]]")
        self.assertEqual(out, ["debug", "support"])
        valid = [t for t in out if t in VALID_ARCS]
        self.assertEqual(valid, ["support"])

    def test_strip_removes_arc_tag_from_display(self) -> None:
        source = "let me listen for a sec. [[arc:support]] tell me more."
        cleaned = strip_all_meta_tags(source)
        self.assertNotIn("arc:", cleaned)
        self.assertIn("listen for a sec", cleaned)
        self.assertIn("tell me more", cleaned)

    def test_strip_handles_unclosed_tail(self) -> None:
        # Streaming holdback: an in-progress ``[[arc:`` opener at the
        # end of the buffer must be suppressed entirely so it never
        # leaks to TTS as the user-visible text.
        cleaned = strip_all_meta_tags("hey there [[arc:supp")
        self.assertNotIn("[[arc", cleaned)


class ArcStoreSelfTagTests(unittest.TestCase):
    def test_set_from_self_tag_writes_at_0_85(self) -> None:
        f = _Fixture()
        try:
            state = f.store.set_from_self_tag("u1", "support", since_turn=4)
            self.assertEqual(state.arc, "support")
            self.assertAlmostEqual(state.confidence, 0.85, places=3)
            self.assertEqual(state.since_turn, 4)
        finally:
            f.close()

    def test_set_from_self_tag_rejects_unknown_arc(self) -> None:
        f = _Fixture()
        try:
            # Should not write anything; the prior remains.
            f.store.upsert("u1", arc="planning", since_turn=1, confidence=0.5)
            state = f.store.set_from_self_tag("u1", "deep_dive", since_turn=5)
            self.assertEqual(state.arc, "planning")
        finally:
            f.close()

    def test_same_arc_self_tag_keeps_since_turn(self) -> None:
        f = _Fixture()
        try:
            f.store.set_from_self_tag("u1", "silly", since_turn=2)
            state = f.store.set_from_self_tag("u1", "silly", since_turn=8)
            # Two consecutive [[arc:silly]] tags represent one arc, not
            # two; ``since_turn`` anchors at the first emission.
            self.assertEqual(state.since_turn, 2)
            self.assertEqual(state.arc, "silly")
            self.assertAlmostEqual(state.confidence, 0.85, places=3)
        finally:
            f.close()

    def test_different_arc_self_tag_resets_since_turn(self) -> None:
        f = _Fixture()
        try:
            f.store.set_from_self_tag("u1", "silly", since_turn=2)
            state = f.store.set_from_self_tag("u1", "support", since_turn=8)
            self.assertEqual(state.arc, "support")
            self.assertEqual(state.since_turn, 8)
        finally:
            f.close()


class ArcEstimatorGuardTests(unittest.TestCase):
    def test_regex_does_not_overwrite_self_tag(self) -> None:
        f = _Fixture()
        try:
            # Self-tag landed on a previous turn at 0.85 (silly).
            f.store.set_from_self_tag("u1", "silly", since_turn=1)
            estimator = ArcEstimator(f.store)
            # User now ventures into venting territory. Regex would
            # propose support (0.55) -- the confidence-ladder guard
            # must refuse the overwrite and keep ``silly``.
            state = estimator.apply_turn(
                "u1",
                user_text="i feel exhausted today, total burnout",
                current_turn=3,
            )
            self.assertEqual(state.arc, "silly")
            # Confidence may decay slightly but stays well above the
            # incoming regex value.
            self.assertGreaterEqual(state.confidence, 0.5)
        finally:
            f.close()

    def test_regex_can_bump_same_arc_self_tag(self) -> None:
        f = _Fixture()
        try:
            f.store.set_from_self_tag("u1", "support", since_turn=1)
            estimator = ArcEstimator(f.store)
            state = estimator.apply_turn(
                "u1",
                user_text="i feel so drained, rough day",
                current_turn=2,
            )
            # Same arc -> max-of-prior-and-new keeps the 0.85 anchor.
            self.assertEqual(state.arc, "support")
            self.assertAlmostEqual(state.confidence, 0.85, places=2)
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()

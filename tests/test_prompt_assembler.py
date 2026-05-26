"""Tests for the budget-aware prompt assembler.

Covers the new ``assemble_with_budget`` entry point: per-block accounting,
verbatim-deduplication against the rolling summary, and overflow detection.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.chat_database import ChatDatabase
from app.core.prompt_assembler import (
    PromptAssembler,
    PromptTelemetry,
    _build_motion_grammar_addendum,
    _build_outfit_grammar_addendum,
    _build_overlay_grammar_addendum,
)


class _TempDb:
    """Context manager that yields a fresh ChatDatabase under a tmpdir.

    Closes the SQLite connection on exit so Windows can clean up the temp
    directory (sqlite holds the file open otherwise).
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db: ChatDatabase | None = None

    def __enter__(self) -> ChatDatabase:
        path = Path(self._tmp.name) / "test.db"
        self._db = ChatDatabase(path)
        return self._db

    def __exit__(self, *exc_info: object) -> None:
        if self._db is not None:
            conn = getattr(self._db._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass


def _make_assembler(db: ChatDatabase, persona_text: str | None = None) -> PromptAssembler:
    """Build an assembler with a controllable persona file (or none)."""
    if persona_text is None:
        persona_path = Path("data/persona/aiko_companion.txt")
        return PromptAssembler(db, persona_path=persona_path, recent_window=20)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    tmp.write(persona_text)
    tmp.close()
    return PromptAssembler(db, persona_path=Path(tmp.name), recent_window=20)


class PromptAssemblerBudgetTests(unittest.TestCase):
    def test_per_block_token_accounting(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Persona body line.")
            db.save_summary(
                session_id="s1",
                summary="• earlier: she liked sushi.",
                summary_tokens=10,
                messages_summarized=0,
            )
            db.add_message(
                session_id="s1",
                role="user",
                content="hello",
                token_count=2,
            )

            messages, telem = assembler.assemble_with_budget(
                "s1",
                "what about ramen",
                context_window=4096,
                response_budget=512,
            )

            self.assertIsInstance(telem, PromptTelemetry)
            self.assertGreater(telem.persona_tokens, 0)
            self.assertGreater(telem.summary_tokens, 0)
            self.assertGreater(telem.user_tokens, 0)
            # All blocks are folded into the system prompt counter.
            self.assertGreaterEqual(
                telem.system_tokens,
                telem.persona_tokens + telem.summary_tokens,
            )
            self.assertEqual(messages[0]["role"], "system")
            # Last message is always the new user turn.
            self.assertEqual(messages[-1]["content"], "what about ramen")
            self.assertTrue(telem.summary_active)

    def test_verbatim_drops_messages_already_in_summary(self) -> None:
        """If the summary covers messages 1..3, only msg #4+ go verbatim."""
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            for i in range(5):
                db.add_message(
                    session_id="s2",
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"old-msg-{i}",
                    token_count=2,
                )
            # Mark the first 3 messages as already summarized.
            db.save_summary(
                session_id="s2",
                summary="bullet summary of 3 msgs",
                summary_tokens=8,
                messages_summarized=3,
            )

            messages, telem = assembler.assemble_with_budget(
                "s2",
                "next",
                context_window=4096,
                response_budget=512,
            )

            verbatim = [m for m in messages if m["role"] in ("user", "assistant")]
            verbatim_user_inputs = [
                m for m in verbatim if m.get("content") != "next"
            ]
            # Only msgs with id > 3 should remain (so 2 verbatim turns).
            self.assertLessEqual(len(verbatim_user_inputs), 2)
            for msg in verbatim_user_inputs:
                # The first 3 ("old-msg-0", "old-msg-1", "old-msg-2") must
                # not appear since the summary already covers them.
                self.assertNotIn(msg["content"], {"old-msg-0", "old-msg-1", "old-msg-2"})
            self.assertEqual(telem.summary_messages, 3)
            self.assertTrue(telem.summary_active)

    def test_compaction_triggered_when_budget_overflows(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            # Stuff a fat history that won't fit in a tiny window.
            big = "word " * 500  # ~500 tokens
            for i in range(10):
                db.add_message(
                    session_id="s3",
                    role="user" if i % 2 == 0 else "assistant",
                    content=big,
                    token_count=500,
                )

            _, telem = assembler.assemble_with_budget(
                "s3",
                "next",
                context_window=2048,
                response_budget=256,
            )
            # Either compaction was triggered or messages were aggressively
            # dropped to fit -- both are valid signals the budget is tight.
            self.assertTrue(
                telem.compaction_triggered or telem.history_messages_dropped > 0,
            )

    def test_aggressive_mode_drops_rag_block(self) -> None:
        """Aggressive mode skips the RAG retriever entirely."""

        class _StubRag:
            def block_for(self, *_args: object, **_kwargs: object) -> str:
                return "Memory block: she likes sushi."

        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            assembler.set_rag_retriever(_StubRag())  # type: ignore[arg-type]
            db.add_message(
                session_id="s4",
                role="user",
                content="prior",
                token_count=2,
            )

            _, telem_normal = assembler.assemble_with_budget(
                "s4",
                "hello",
                context_window=4096,
                response_budget=256,
                aggressive=False,
            )
            _, telem_aggressive = assembler.assemble_with_budget(
                "s4",
                "hello",
                context_window=4096,
                response_budget=256,
                aggressive=True,
            )
            self.assertGreater(telem_normal.rag_tokens, 0)
            self.assertEqual(telem_aggressive.rag_tokens, 0)


class GrammarAddendumTests(unittest.TestCase):
    """Spot-checks on the new dynamic prompt addendum builders.

    These are the prompt-side surface that nudges the LLM into using
    ``[[overlay:tail_wag]]`` / ``[[outfit:day]]`` etc. instead of
    falling back to italic prose stage directions.
    """

    def test_overlay_addendum_includes_both_winks_under_has_wink(self) -> None:
        # ``has_wink`` is the single capability flag covering both
        # eyes; the gesture grammar advertises ``wink_left`` AND
        # ``wink_right`` even though there's no separate flag for
        # each. Without the flag-override this test would catch a
        # silent regression where the wink lines disappear.
        block = _build_overlay_grammar_addendum({
            "has_wink": True,
            "has_tail_wag": True,
        })
        self.assertIn("[[overlay:wink_left]]", block)
        self.assertIn("[[overlay:wink_right]]", block)
        self.assertIn("[[overlay:tail_wag]]", block)

    def test_overlay_addendum_split_into_emotional_and_gesture_tiers(self) -> None:
        block = _build_overlay_grammar_addendum({
            "has_blush": True,
            "has_wink": True,
            "has_tail_wag": True,
        })
        self.assertIn("Emotional overlays", block)
        self.assertIn("Body gestures", block)
        # The gesture tier explicitly forbids prose stage directions.
        self.assertIn("*shakes tail*", block)
        self.assertIn("never replace", block.lower())

    def test_overlay_addendum_empty_when_no_caps(self) -> None:
        self.assertEqual(_build_overlay_grammar_addendum({}), "")
        self.assertEqual(_build_overlay_grammar_addendum(None), "")

    def test_outfit_addendum_advertises_only_what_rig_supports(self) -> None:
        # The bullet rule lines (``- [[outfit:X]]``) are gated on the
        # capability flags. The illustrative example may still mention
        # ``[[outfit:day]]`` even when the rig only supports pajamas;
        # we check the bullet specifically.
        block = _build_outfit_grammar_addendum({"has_pajamas": True})
        self.assertIn("- [[outfit:pajamas]]", block)
        self.assertNotIn("- [[outfit:day]]", block)
        self.assertNotIn("- [[outfit:pajamas_hooded]]", block)
        block2 = _build_outfit_grammar_addendum({"has_day_clothes": True})
        self.assertIn("- [[outfit:day]]", block2)
        self.assertNotIn("- [[outfit:pajamas]]", block2)
        self.assertNotIn("- [[outfit:pajamas_hooded]]", block2)

    def test_outfit_addendum_advertises_pajamas_hooded_variant(self) -> None:
        # Hooded pajamas is a capability-gated bullet just like the
        # bare pajamas / day options: present iff ``has_pajamas_hooded``
        # is True, and unaffected by the other outfit flags.
        block = _build_outfit_grammar_addendum({"has_pajamas_hooded": True})
        self.assertIn("- [[outfit:pajamas_hooded]]", block)
        self.assertNotIn("- [[outfit:pajamas]]", block)
        self.assertNotIn("- [[outfit:day]]", block)

    def test_outfit_addendum_full_rig_lists_all_three(self) -> None:
        # Real Alexia exposes all three outfit caps; the grammar must
        # advertise every supported variant so the LLM can pick.
        block = _build_outfit_grammar_addendum({
            "has_pajamas": True,
            "has_pajamas_hooded": True,
            "has_day_clothes": True,
        })
        self.assertIn("- [[outfit:pajamas]]", block)
        self.assertIn("- [[outfit:pajamas_hooded]]", block)
        self.assertIn("- [[outfit:day]]", block)

    def test_outfit_addendum_empty_without_outfit_caps(self) -> None:
        self.assertEqual(
            _build_outfit_grammar_addendum({"has_blush": True}),
            "",
        )

    def test_motion_addendum_intersects_rig_with_registry(self) -> None:
        # Rig ships ``dh`` (cloth sway, not in registry) and ``wave``
        # (in registry). Only the wave should surface.
        block = _build_motion_grammar_addendum(["dh", "wave"])
        self.assertIn("[[motion:wave]]", block)
        self.assertNotIn("[[motion:dh]]", block)

    def test_motion_addendum_empty_when_no_recognised_motions(self) -> None:
        self.assertEqual(_build_motion_grammar_addendum(["dh"]), "")
        self.assertEqual(_build_motion_grammar_addendum([]), "")

    def test_motion_grammar_clarifies_gestures_are_overlays(self) -> None:
        """The motion block must explicitly steer the LLM away from
        ``[[motion:tail_wag]]`` / ``[[motion:wink_*]]`` / ``[[motion:ear_wiggle]]``
        — those are overlays. Without the contrast clarifier the model
        confused the two channels and the request fell on the floor.
        """
        block = _build_motion_grammar_addendum(["wave"])
        self.assertIn("[[overlay:", block)
        block_lower = block.lower()
        self.assertIn("tail-wag", block_lower)

    def test_overlay_gesture_grammar_contrasts_with_motion(self) -> None:
        """The body-gestures section in the overlay block must contrast
        with the motion channel: an explicit ``[[motion:tail_wag]] does
        nothing`` line (or equivalent) is the one nudge that pushed the
        LLM to consistently pick the overlay tag in our regression turn.
        """
        block = _build_overlay_grammar_addendum({"has_tail_wag": True})
        self.assertIn("[[motion:tail_wag]]", block)
        self.assertIn("[[overlay:tail_wag]]", block)


if __name__ == "__main__":
    unittest.main()

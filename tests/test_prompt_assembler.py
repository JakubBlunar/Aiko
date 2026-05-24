"""Tests for the budget-aware prompt assembler.

Covers the new ``assemble_with_budget`` entry point: per-block accounting,
verbatim-deduplication against the rolling summary, and overflow detection.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.chat_database import ChatDatabase
from app.core.prompt_assembler import PromptAssembler, PromptTelemetry


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


if __name__ == "__main__":
    unittest.main()

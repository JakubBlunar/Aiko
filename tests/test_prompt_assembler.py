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
    _SPEECH_GRAMMAR_ADDENDUM,
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


class NarrativeBlockProviderTests(unittest.TestCase):
    """The ``narrative`` slot is the inner-monologue line that surfaces
    a fresh prepared nudge ("On your mind: ...") in typed-mode turns.

    Until A1 it was wired to ``None`` and silently dropped. These tests
    lock in the new per-turn freshness (so a content change between two
    successive ``assemble_with_budget`` calls is reflected immediately,
    NOT cached behind ``history_max_id``) and the empty-string skip.
    """

    def test_narrative_block_surfaces_when_provider_returns_text(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="s5",
                role="user",
                content="hi",
                token_count=2,
            )
            assembler.set_inner_life_providers(
                narrative=lambda: "On your mind: yesterday's debugging session.",
            )
            messages, telem = assembler.assemble_with_budget(
                "s5",
                "what's up?",
                context_window=4096,
                response_budget=256,
            )
            self.assertGreater(telem.narrative_tokens, 0)
            self.assertIn(
                "On your mind: yesterday's debugging session.",
                messages[0]["content"],
            )

    def test_narrative_block_silent_when_provider_empty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="s6",
                role="user",
                content="hi",
                token_count=2,
            )
            # No provider registered at all -> default ``None`` -> empty.
            _, telem_unwired = assembler.assemble_with_budget(
                "s6",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertEqual(telem_unwired.narrative_tokens, 0)
            # Provider returning empty string -> still skipped.
            assembler.set_inner_life_providers(narrative=lambda: "")
            _, telem_empty = assembler.assemble_with_budget(
                "s6",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertEqual(telem_empty.narrative_tokens, 0)

    def test_narrative_block_refreshes_per_turn_not_cached(self) -> None:
        """Regression guard: the ``_StaticSlices`` cache must NOT include
        the narrative block. A nudge can flip between turns even when
        ``history_max_id`` doesn't move (NarrativeWeaver runs every N
        turns, ProactiveDirector consumes nudges) — caching it would
        surface stale text indefinitely.
        """
        narrative_value = ["A loose thread: chase the cat-tail bug."]

        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="s7",
                role="user",
                content="hi",
                token_count=2,
            )
            assembler.set_inner_life_providers(
                narrative=lambda: narrative_value[0],
            )
            messages_a, _ = assembler.assemble_with_budget(
                "s7",
                "first turn",
                context_window=4096,
                response_budget=256,
            )
            self.assertIn("chase the cat-tail bug", messages_a[0]["content"])

            # Same session, same history watermark — flip the provider
            # output. If the static-slice cache was retaining narrative
            # we'd still see the old text.
            narrative_value[0] = "Something you said you'd do: ship the docs."
            messages_b, _ = assembler.assemble_with_budget(
                "s7",
                "second turn",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("chase the cat-tail bug", messages_b[0]["content"])
            self.assertIn(
                "Something you said you'd do: ship the docs.",
                messages_b[0]["content"],
            )


class ActivityBlockProviderTests(unittest.TestCase):
    """The ``activity`` slot surfaces "Jacob is currently working in <App>"
    as an opt-in inner-life cue. Verifies the standard provider hooks:
    populates the prompt when wired, silent when empty, dropped under
    ``aggressive=True``."""

    def test_activity_block_lands_in_system_prompt(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="sa1",
                role="user",
                content="hi",
                token_count=2,
            )
            assembler.set_inner_life_providers(
                activity=lambda: "Jacob is currently working in Cursor.",
            )
            messages, _telem = assembler.assemble_with_budget(
                "sa1",
                "what's up?",
                context_window=4096,
                response_budget=256,
            )
            self.assertIn(
                "Jacob is currently working in Cursor.",
                messages[0]["content"],
            )

    def test_activity_block_silent_when_provider_empty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="sa2",
                role="user",
                content="hi",
                token_count=2,
            )
            assembler.set_inner_life_providers(activity=lambda: "")
            messages, _ = assembler.assemble_with_budget(
                "sa2",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("Jacob is currently working", messages[0]["content"])

    def test_activity_block_dropped_under_aggressive(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="sa3",
                role="user",
                content="hi",
                token_count=2,
            )
            assembler.set_inner_life_providers(
                activity=lambda: "Jacob is currently working in Cursor.",
            )
            messages, _ = assembler.assemble_with_budget(
                "sa3",
                "x",
                context_window=4096,
                response_budget=256,
                aggressive=True,
            )
            self.assertNotIn(
                "Jacob is currently working", messages[0]["content"],
            )


class AnniversaryBlockProviderTests(unittest.TestCase):
    """The anniversary inner-life provider lands in the system prompt
    after the relationship block, and is dropped under ``aggressive``."""

    def test_anniversary_block_in_system_prompt(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(session_id="ann1", role="user", content="hi", token_count=2)
            assembler.set_inner_life_providers(
                anniversary=lambda: (
                    "On your mind today — a month ago today: "
                    "we debugged the proactive bug together."
                ),
            )
            messages, _ = assembler.assemble_with_budget(
                "ann1",
                "yo",
                context_window=4096,
                response_budget=256,
            )
            self.assertIn("a month ago today", messages[0]["content"])

    def test_anniversary_block_silent_when_empty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(session_id="ann2", role="user", content="hi", token_count=2)
            assembler.set_inner_life_providers(anniversary=lambda: "")
            messages, _ = assembler.assemble_with_budget(
                "ann2",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("On your mind today", messages[0]["content"])

    def test_anniversary_block_dropped_under_aggressive(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(session_id="ann3", role="user", content="hi", token_count=2)
            assembler.set_inner_life_providers(
                anniversary=lambda: "On your mind today — a month ago: X.",
            )
            messages, _ = assembler.assemble_with_budget(
                "ann3",
                "x",
                context_window=4096,
                response_budget=256,
                aggressive=True,
            )
            self.assertNotIn("a month ago", messages[0]["content"])


class AxesBlockProviderTests(unittest.TestCase):
    """The relationship-axes inner-life provider feeds the system prompt."""

    def test_axes_block_lands_in_system_prompt(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(session_id="ax1", role="user", content="hi", token_count=2)
            assembler.set_inner_life_providers(
                axes=lambda: "How the relationship feels: you feel close to Jacob right now.",
            )
            messages, _ = assembler.assemble_with_budget(
                "ax1",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertIn("How the relationship feels", messages[0]["content"])

    def test_axes_block_silent_when_empty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(session_id="ax2", role="user", content="hi", token_count=2)
            assembler.set_inner_life_providers(axes=lambda: "")
            messages, _ = assembler.assemble_with_budget(
                "ax2",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("How the relationship feels", messages[0]["content"])

    def test_axes_block_dropped_under_aggressive(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(session_id="ax3", role="user", content="hi", token_count=2)
            assembler.set_inner_life_providers(
                axes=lambda: "How the relationship feels: close.",
            )
            messages, _ = assembler.assemble_with_budget(
                "ax3",
                "x",
                context_window=4096,
                response_budget=256,
                aggressive=True,
            )
            self.assertNotIn("How the relationship feels", messages[0]["content"])


class NoveltyBlockProviderTests(unittest.TestCase):
    """K6 novelty provider lands in the system prompt, is dropped under
    ``aggressive=True``, and receives the current ``user_text``."""

    def test_novelty_block_lands_in_system_prompt(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="nv1", role="user", content="hi", token_count=2,
            )
            seen: list[str] = []

            def _provider(user_text: str) -> str:
                seen.append(user_text)
                return "Heads-up: Jacob just brought up something new."

            assembler.set_inner_life_providers(novelty=_provider)
            messages, _ = assembler.assemble_with_budget(
                "nv1",
                "what about quantum computing?",
                context_window=4096,
                response_budget=256,
            )
            self.assertEqual(seen, ["what about quantum computing?"])
            self.assertIn("Heads-up", messages[0]["content"])

    def test_novelty_block_silent_when_provider_empty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="nv2", role="user", content="hi", token_count=2,
            )
            # Empty provider -> system prompt doesn't grow a Heads-up line.
            assembler.set_inner_life_providers(novelty=lambda _t: "")
            messages, _ = assembler.assemble_with_budget(
                "nv2",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("Heads-up", messages[0]["content"])

    def test_novelty_block_dropped_under_aggressive(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="nv3", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                novelty=lambda _t: "Heads-up: out of the blue.",
            )
            messages, _ = assembler.assemble_with_budget(
                "nv3",
                "x",
                context_window=4096,
                response_budget=256,
                aggressive=True,
            )
            self.assertNotIn("Heads-up", messages[0]["content"])

    def test_novelty_provider_exception_swallowed(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="nv4", role="user", content="hi", token_count=2,
            )

            def _boom(_t: str) -> str:
                raise RuntimeError("detector exploded")

            assembler.set_inner_life_providers(novelty=_boom)
            # Should not raise; the block just disappears.
            messages, _ = assembler.assemble_with_budget(
                "nv4",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("Heads-up", messages[0]["content"])


class StagnationBlockProviderTests(unittest.TestCase):
    """K18 stagnation provider lands in the system prompt right after
    novelty, is dropped under ``aggressive=True``, receives
    ``user_text`` (for symmetry with the K6 provider), and survives
    a raising provider without breaking the turn."""

    def test_stagnation_block_lands_after_novelty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="st1", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                novelty=lambda _t: "Heads-up: novelty cue.",
                stagnation=lambda _t: "Heads-up: been on this for a while.",
            )
            messages, _ = assembler.assemble_with_budget(
                "st1",
                "we keep coming back to this",
                context_window=4096,
                response_budget=256,
            )
            content = messages[0]["content"]
            # Both blocks should be present...
            self.assertIn("Heads-up: novelty cue", content)
            self.assertIn("been on this for a while", content)
            # ...with novelty *before* stagnation, since reaction
            # cues cluster together and the order encodes the K6-then-
            # K18 dataflow.
            self.assertLess(
                content.index("novelty cue"),
                content.index("been on this for a while"),
            )

    def test_stagnation_block_silent_when_provider_empty(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="st2", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                stagnation=lambda _t: "",
            )
            messages, _ = assembler.assemble_with_budget(
                "st2",
                "anything",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("Heads-up", messages[0]["content"])

    def test_stagnation_block_dropped_under_aggressive(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="st3", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                stagnation=lambda _t: "Heads-up: lulled.",
            )
            messages, _ = assembler.assemble_with_budget(
                "st3",
                "x",
                context_window=4096,
                response_budget=256,
                aggressive=True,
            )
            self.assertNotIn("Heads-up", messages[0]["content"])

    def test_stagnation_provider_exception_swallowed(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="st4", role="user", content="hi", token_count=2,
            )

            def _boom(_t: str) -> str:
                raise RuntimeError("stagnation exploded")

            assembler.set_inner_life_providers(stagnation=_boom)
            # Should not raise; the block just disappears.
            messages, _ = assembler.assemble_with_budget(
                "st4",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertNotIn("Heads-up", messages[0]["content"])

    def test_stagnation_provider_receives_user_text(self) -> None:
        # The provider takes ``user_text`` for symmetry with K6 even
        # though the streak detector itself doesn't currently read
        # it -- pin the contract so future refactors don't silently
        # drop the argument.
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="st5", role="user", content="hi", token_count=2,
            )
            seen: list[str] = []

            def _provider(user_text: str) -> str:
                seen.append(user_text)
                return ""

            assembler.set_inner_life_providers(stagnation=_provider)
            assembler.assemble_with_budget(
                "st5",
                "we keep circling this",
                context_window=4096,
                response_budget=256,
            )
            self.assertEqual(seen, ["we keep circling this"])


class GroundingLineModeTests(unittest.TestCase):
    """K16 unified ambient grounding line modes.

    Locks the suppression matrix:
      ``off``     -- grounding line absent; granular blocks render.
      ``replace`` -- grounding line present; eight granular blocks
                     suppressed (circadian, ambient_noise, affect,
                     mood_hint, relationship, user_state, world,
                     activity).
      ``split``   -- grounding line present; only situational blocks
                     suppressed (circadian, ambient_noise, world,
                     activity); affect / mood_hint / relationship /
                     user_state retained.

    Anniversary, profile, novelty, stagnation, knowledge_gaps,
    belief_gaps, agenda, axes, petname, vocal_tone, catchphrase,
    narrative, arc, pajama are NEVER suppressed and are checked here
    only as a regression guard.
    """

    GRANULAR_PROVIDERS = {
        "circadian": "GRAN_CIRCADIAN",
        "ambient_noise": "GRAN_NOISE",
        "world": "GRAN_WORLD",
        "activity": "GRAN_ACTIVITY",
        "affect": "GRAN_AFFECT",
        "relationship": "GRAN_RELATIONSHIP",
        "user_state": "GRAN_USER_STATE",
        "anniversary": "GRAN_ANNIV",
        "axes": "GRAN_AXES",
    }

    def _wire(self, assembler, *, grounding_text: str) -> None:
        kwargs = {name: (lambda v=value: v) for name, value in self.GRANULAR_PROVIDERS.items()}
        kwargs["grounding_line"] = lambda: grounding_text
        assembler.set_inner_life_providers(**kwargs)

    def test_off_mode_keeps_all_granular_blocks(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="g1", role="user", content="hi", token_count=2,
            )
            assembler.set_grounding_line_mode("off")
            self._wire(assembler, grounding_text="GROUND_LINE_OFF_PARAGRAPH")
            messages, _ = assembler.assemble_with_budget(
                "g1", "x", context_window=4096, response_budget=256,
            )
            content = messages[0]["content"]
            self.assertNotIn("GROUND_LINE", content)
            for label in self.GRANULAR_PROVIDERS.values():
                self.assertIn(label, content, f"missing {label} in off mode")

    def test_replace_mode_drops_eight_blocks_keeps_others(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="g2", role="user", content="hi", token_count=2,
            )
            assembler.set_grounding_line_mode("replace")
            self._wire(assembler, grounding_text="GROUND_LINE_REPLACE")
            messages, _ = assembler.assemble_with_budget(
                "g2", "x", context_window=4096, response_budget=256,
            )
            content = messages[0]["content"]
            self.assertIn("GROUND_LINE_REPLACE", content)
            # Eight blocks dropped under replace.
            for label in (
                "GRAN_CIRCADIAN", "GRAN_NOISE",
                "GRAN_WORLD", "GRAN_ACTIVITY",
                "GRAN_AFFECT", "GRAN_RELATIONSHIP",
                "GRAN_USER_STATE",
            ):
                self.assertNotIn(label, content, f"{label} should be suppressed in replace mode")
            # Always-standalone regression guards.
            self.assertIn("GRAN_ANNIV", content)
            self.assertIn("GRAN_AXES", content)

    def test_split_mode_drops_situational_keeps_trend(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="g3", role="user", content="hi", token_count=2,
            )
            assembler.set_grounding_line_mode("split")
            self._wire(assembler, grounding_text="GROUND_LINE_SPLIT")
            messages, _ = assembler.assemble_with_budget(
                "g3", "x", context_window=4096, response_budget=256,
            )
            content = messages[0]["content"]
            self.assertIn("GROUND_LINE_SPLIT", content)
            # Situational blocks dropped under split.
            for label in (
                "GRAN_CIRCADIAN", "GRAN_NOISE",
                "GRAN_WORLD", "GRAN_ACTIVITY",
            ):
                self.assertNotIn(label, content, f"{label} should be suppressed in split mode")
            # Trend / phase blocks retained under split.
            for label in (
                "GRAN_AFFECT", "GRAN_RELATIONSHIP", "GRAN_USER_STATE",
            ):
                self.assertIn(label, content, f"{label} should be retained in split mode")
            # Always-standalone.
            self.assertIn("GRAN_ANNIV", content)
            self.assertIn("GRAN_AXES", content)

    def test_invalid_mode_clamps_to_off(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="g4", role="user", content="hi", token_count=2,
            )
            assembler.set_grounding_line_mode("nonsense")
            self._wire(assembler, grounding_text="GROUND_LINE_INVALID")
            messages, _ = assembler.assemble_with_budget(
                "g4", "x", context_window=4096, response_budget=256,
            )
            content = messages[0]["content"]
            # Mode clamped to off -> grounding block builds (provider
            # still returns text), but no granular suppression fires.
            self.assertIn("GRAN_CIRCADIAN", content)
            self.assertIn("GRAN_AFFECT", content)

    def test_grounding_line_dropped_under_aggressive(self) -> None:
        # Even in replace/split, aggressive trim suppresses the
        # grounding line (paragraph savings come from the rolling
        # summary anyway).
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="g5", role="user", content="hi", token_count=2,
            )
            assembler.set_grounding_line_mode("replace")
            self._wire(assembler, grounding_text="GROUND_LINE_AGG")
            messages, _ = assembler.assemble_with_budget(
                "g5", "x",
                context_window=4096, response_budget=256, aggressive=True,
            )
            content = messages[0]["content"]
            self.assertNotIn("GROUND_LINE_AGG", content)

    def test_grounding_line_provider_timing_recorded(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="g6", role="user", content="hi", token_count=2,
            )
            assembler.set_grounding_line_mode("replace")
            self._wire(assembler, grounding_text="GROUND_LINE_TIMING")
            _, telemetry = assembler.assemble_with_budget(
                "g6", "x", context_window=4096, response_budget=256,
            )
            self.assertIn("grounding_line", telemetry.provider_ms)
            self.assertGreaterEqual(telemetry.provider_ms["grounding_line"], 0.0)


class PhaseTelemetryTests(unittest.TestCase):
    """P2 (perf backlog): per-provider wall time + aggregate phase
    timings on :class:`PromptTelemetry`. These tests pin the data
    contract -- not the absolute timing numbers, which are machine-
    dependent."""

    def test_provider_ms_only_includes_providers_that_ran(self) -> None:
        # No providers wired -> empty dict, not a hardcoded list of
        # zeros. This is the v1 promise: the dict reflects live wiring,
        # not a legacy 10-block schema.
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="pt1", role="user", content="hi", token_count=2,
            )
            _, telem = assembler.assemble_with_budget(
                "pt1",
                "x",
                context_window=4096,
                response_budget=256,
            )
            self.assertEqual(telem.provider_ms, {})

    def test_provider_ms_records_each_wired_provider(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="pt2", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                affect=lambda: "affect",
                circadian=lambda: "circ",
                novelty=lambda _t: "Heads-up.",
                stagnation=lambda _t: "",
            )
            _, telem = assembler.assemble_with_budget(
                "pt2",
                "anything",
                context_window=4096,
                response_budget=256,
            )
            # Static (zero-arg) providers go through the cached slice
            # build, which has its own timing path; we don't pin it
            # here. The live (per-turn) providers MUST appear.
            self.assertIn("novelty", telem.provider_ms)
            self.assertIn("stagnation", telem.provider_ms)
            for name, ms in telem.provider_ms.items():
                # Wall time must always be non-negative -- catches
                # timer-direction bugs.
                self.assertGreaterEqual(ms, 0.0, f"provider {name}")

    def test_provider_ms_round_tripped_through_as_dict(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="pt3", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                novelty=lambda _t: "Heads-up.",
            )
            _, telem = assembler.assemble_with_budget(
                "pt3", "x", context_window=4096, response_budget=256,
            )
            payload = telem.as_dict()
            # Survives round-trip and is JSON-friendly (floats only).
            self.assertIn("novelty", payload["provider_ms"])
            self.assertIsInstance(payload["provider_ms"]["novelty"], float)
            # New top-level fields exist.
            self.assertIn("rag_lookup_ms", payload)
            self.assertIn("assemble_ms", payload)
            self.assertGreaterEqual(payload["assemble_ms"], 0.0)

    def test_assemble_ms_covers_full_build(self) -> None:
        # ``assemble_ms`` must be at least the sum of recorded provider
        # times -- catches a bug where the timer is started in the
        # wrong place (e.g. *after* the slice cache instead of at the
        # top of ``assemble_with_budget``).
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="pt4", role="user", content="hi", token_count=2,
            )
            assembler.set_inner_life_providers(
                novelty=lambda _t: "n",
                belief_gaps=lambda: "b",
                knowledge_gaps=lambda _t: "k",
            )
            _, telem = assembler.assemble_with_budget(
                "pt4", "x", context_window=4096, response_budget=256,
            )
            provider_total = sum(telem.provider_ms.values())
            # Allow a small float-rounding tolerance: provider_ms
            # entries are rounded to 2dp, ``assemble_ms`` likewise.
            self.assertGreaterEqual(
                telem.assemble_ms + 0.05, provider_total,
                f"assemble_ms={telem.assemble_ms} < provider_total={provider_total}",
            )

    def test_embed_fields_default_to_zero_without_turn_runner(self) -> None:
        # ``assemble_with_budget`` only stamps the assemble/RAG/provider
        # phase fields; the P1 embed_calls/embed_ms are populated by
        # ``TurnRunner`` post-build. Direct callers (tests, ad-hoc
        # scripts) should see clean zeros.
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="pt5", role="user", content="hi", token_count=2,
            )
            _, telem = assembler.assemble_with_budget(
                "pt5", "x", context_window=4096, response_budget=256,
            )
            self.assertEqual(telem.embed_calls, 0)
            self.assertEqual(telem.embed_ms, 0.0)


class FailingProviderTimingTests(unittest.TestCase):
    """A provider that raises must still record a timing bucket -- the
    operator wants to see "novelty took 3ms then exploded", not "novelty
    silently disappeared from the telemetry"."""

    def test_raising_provider_is_still_timed(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="P")
            db.add_message(
                session_id="pt6", role="user", content="hi", token_count=2,
            )

            def _boom(_t: str) -> str:
                raise RuntimeError("explode")

            assembler.set_inner_life_providers(novelty=_boom)
            _, telem = assembler.assemble_with_budget(
                "pt6", "x", context_window=4096, response_budget=256,
            )
            # The provider raised but timing must still be recorded so
            # operators can see "this provider is broken AND was slow".
            self.assertIn("novelty", telem.provider_ms)


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


class SpeechGrammarAddendumTests(unittest.TestCase):
    """The ``_SPEECH_GRAMMAR_ADDENDUM`` mirrors persona-side rules in a
    place that survives a user rewriting / deleting their persona file.
    Locks in the existing stage-direction + correction grammar plus the
    name-templated "match <user>'s register" cue from A2 (user-affect
    awareness).
    """

    def test_addendum_advertises_core_stage_directions(self) -> None:
        for tag in ("[[laugh]]", "[[sigh]]", "[[gasp]]", "[[hum]]"):
            self.assertIn(tag, _SPEECH_GRAMMAR_ADDENDUM)
        self.assertIn("[[correct]]", _SPEECH_GRAMMAR_ADDENDUM)

    def test_addendum_instructs_aiko_to_match_user_register(self) -> None:
        """A2: when the prompt mentions ``User sounds: …`` or
        ``Right now <name>: …`` (vocal_tone / user_state blocks),
        Aiko should mirror the register instead of ignoring the cue.
        Without this nudge the LLM treats the cues as decoration.
        """
        from app.core.prompt_assembler import build_speech_grammar_addendum

        addendum = build_speech_grammar_addendum("Jacob")
        self.assertIn("Match Jacob's register", addendum)
        # Anchor on the actual block names so a future rename of the
        # vocal_tone / user_state prompt prefixes catches this test
        # before it ships.
        self.assertIn("User sounds:", addendum)
        self.assertIn("Right now Jacob:", addendum)
        # Behavioral instruction must explicitly forbid the mechanical
        # phrasing we observed in early prototypes.
        addendum_lower = addendum.lower()
        self.assertIn("naturally", addendum_lower)
        self.assertIn("never quote the system line", addendum_lower)


if __name__ == "__main__":
    unittest.main()

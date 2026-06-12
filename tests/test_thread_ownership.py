"""Tests for K55 thread ownership — the pure verdict walk, the
render, the inner-life provider plumbing (via a minimal mixin host
stub), and the prompt-assembler slot wiring."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.conversation import thread_ownership as town
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)


class DeriveTopicTests(unittest.TestCase):
    def test_want_text_wins(self) -> None:
        self.assertEqual(
            town.derive_topic("ask about the garden", "long reply text"),
            "ask about the garden",
        )

    def test_falls_back_to_assistant_text(self) -> None:
        self.assertEqual(
            town.derive_topic(None, "I read a thing about bees"),
            "I read a thing about bees",
        )
        self.assertEqual(
            town.derive_topic("  ", "I read a thing about bees"),
            "I read a thing about bees",
        )

    def test_whitespace_collapsed_and_trimmed(self) -> None:
        topic = town.derive_topic(None, "a   b\n\nc " + "x" * 400)
        self.assertTrue(topic.startswith("a b c"))
        self.assertLessEqual(len(topic), 160)
        self.assertTrue(topic.endswith("…"))

    def test_empty_everything(self) -> None:
        self.assertEqual(town.derive_topic(None, ""), "")


class EvaluateReplyTests(unittest.TestCase):
    def _thread(self, embedding=None) -> town.OwnedThread:
        return town.OwnedThread(
            topic="the bees thing", source=town.SOURCE_INITIATIVE,
            embedding=embedding,
        )

    def test_on_topic_short_reply_is_engaged(self) -> None:
        # "yeah I loved it" is an answer, not a pivot — cosine wins
        # over the length gate.
        thread = self._thread(np.array([1.0, 0.0], dtype=np.float32))
        verdict = town.evaluate_reply(
            thread, "yeah I loved it",
            np.array([0.9, 0.1], dtype=np.float32),
        )
        self.assertEqual(verdict.verdict, town.VERDICT_ENGAGED)
        self.assertIsNotNone(verdict.cosine)

    def test_off_topic_reply_is_pivot(self) -> None:
        thread = self._thread(np.array([1.0, 0.0], dtype=np.float32))
        verdict = town.evaluate_reply(
            thread, "anyway, what about lunch",
            np.array([0.0, 1.0], dtype=np.float32),
        )
        self.assertEqual(verdict.verdict, town.VERDICT_PIVOT)

    def test_no_embedding_substantial_is_engaged(self) -> None:
        verdict = town.evaluate_reply(
            self._thread(None), "x" * 100, None, engaged_chars=80,
        )
        self.assertEqual(verdict.verdict, town.VERDICT_ENGAGED)
        self.assertIsNone(verdict.cosine)

    def test_no_embedding_short_is_pivot(self) -> None:
        verdict = town.evaluate_reply(
            self._thread(None), "ok cool", None, engaged_chars=80,
        )
        self.assertEqual(verdict.verdict, town.VERDICT_PIVOT)

    def test_very_short_reply_never_measured(self) -> None:
        # Below the measurable floor the cosine is skipped even when
        # both embeddings exist — "ok" carries no topical signal.
        thread = self._thread(np.array([1.0, 0.0], dtype=np.float32))
        verdict = town.evaluate_reply(
            thread, "ok", np.array([1.0, 0.0], dtype=np.float32),
        )
        self.assertIsNone(verdict.cosine)
        self.assertEqual(verdict.verdict, town.VERDICT_PIVOT)

    def test_mismatched_shapes_fall_back_to_length(self) -> None:
        thread = self._thread(np.array([1.0, 0.0], dtype=np.float32))
        verdict = town.evaluate_reply(
            thread, "x" * 100,
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
        )
        self.assertIsNone(verdict.cosine)
        self.assertEqual(verdict.verdict, town.VERDICT_ENGAGED)

    def test_threshold_respected(self) -> None:
        thread = self._thread(np.array([1.0, 0.0], dtype=np.float32))
        vec = np.array([0.5, 0.866], dtype=np.float32)  # cosine ~0.5
        engaged = town.evaluate_reply(
            thread, "some medium reply", vec,
            min_topical_similarity=0.30,
        )
        self.assertEqual(engaged.verdict, town.VERDICT_ENGAGED)
        pivot = town.evaluate_reply(
            thread, "some medium reply", vec,
            min_topical_similarity=0.70,
        )
        self.assertEqual(pivot.verdict, town.VERDICT_PIVOT)


class RenderTests(unittest.TestCase):
    def test_copy(self) -> None:
        block = town.render_return_block(
            "the bees thing", user_display_name="Jacob",
        )
        self.assertIn("the bees thing", block)
        self.assertIn("Jacob", block)
        self.assertIn("ONE shot", block)
        self.assertIn("never a second", block)

    def test_blank_topic_fallback(self) -> None:
        block = town.render_return_block("", user_display_name="Jacob")
        self.assertIn("the thing you brought up", block)


# ── provider plumbing ───────────────────────────────────────────────


class _FakeEmbedder:
    def __init__(self, vec) -> None:
        self._vec = vec
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        if isinstance(self._vec, Exception):
            raise self._vec
        return self._vec


class _Host(InnerLifeProvidersMixin):
    user_display_name = "Jacob"

    def __init__(self, *, enabled: bool = True, embedder=None) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                thread_ownership_enabled=enabled,
                thread_engaged_chars=80,
                thread_min_topical_similarity=0.30,
            ),
        )
        self._embedder = embedder
        self._owned_thread = None


class ProviderTests(unittest.TestCase):
    def _open_thread(self, host: _Host, embedding=None) -> None:
        host._owned_thread = town.OwnedThread(
            topic="the bees documentary",
            source=town.SOURCE_INITIATIVE,
            embedding=embedding,
        )

    def test_no_thread_silent(self) -> None:
        host = _Host()
        self.assertEqual(
            host._render_thread_ownership_block("hi there"), "",
        )

    def test_disabled_switch_keeps_thread(self) -> None:
        host = _Host(enabled=False)
        self._open_thread(host)
        self.assertEqual(
            host._render_thread_ownership_block("ok"), "",
        )
        self.assertIsNotNone(host._owned_thread)

    def test_blank_user_text_keeps_thread(self) -> None:
        # A proactive turn must not consume the evaluation slot.
        host = _Host()
        self._open_thread(host)
        self.assertEqual(host._render_thread_ownership_block(""), "")
        self.assertIsNotNone(host._owned_thread)

    def test_pivot_renders_once_then_dropped(self) -> None:
        host = _Host(
            embedder=_FakeEmbedder(
                np.array([0.0, 1.0], dtype=np.float32),
            ),
        )
        self._open_thread(
            host, embedding=np.array([1.0, 0.0], dtype=np.float32),
        )
        block = host._render_thread_ownership_block(
            "anyway what about lunch",
        )
        self.assertIn("the bees documentary", block)
        self.assertIn("ONE shot", block)
        self.assertIsNone(host._owned_thread)
        # One return maximum — the slot is gone.
        self.assertEqual(
            host._render_thread_ownership_block("more pivoting"), "",
        )

    def test_engaged_clears_silently(self) -> None:
        host = _Host(
            embedder=_FakeEmbedder(
                np.array([1.0, 0.0], dtype=np.float32),
            ),
        )
        self._open_thread(
            host, embedding=np.array([1.0, 0.0], dtype=np.float32),
        )
        self.assertEqual(
            host._render_thread_ownership_block(
                "oh I watched it too!",
            ),
            "",
        )
        self.assertIsNone(host._owned_thread)

    def test_embedder_failure_falls_back_to_length(self) -> None:
        host = _Host(embedder=_FakeEmbedder(RuntimeError("down")))
        self._open_thread(
            host, embedding=np.array([1.0, 0.0], dtype=np.float32),
        )
        block = host._render_thread_ownership_block("ok sure")
        self.assertIn("ONE shot", block)
        self.assertIsNone(host._owned_thread)


class ThreadOwnershipProviderSlotTests(unittest.TestCase):
    """K55 block lands in the system prompt, receives the live
    user_text, sits between the K53 initiative block and the K52
    wants block, and is NOT dropped under ``aggressive=True`` (the
    provider consumes one-shot state)."""

    _CUE = "You opened a thread last turn -- the bees thing"

    def _assemble(self, *, aggressive: bool = False, **providers):
        from app.core.infra.chat_database import ChatDatabase
        from app.core.session.prompt_assembler import PromptAssembler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        persona = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        persona.write("P")
        persona.close()
        assembler = PromptAssembler(
            db, persona_path=Path(persona.name), recent_window=20,
        )
        db.add_message(
            session_id="t1", role="user", content="hi", token_count=2,
        )
        assembler.set_inner_life_providers(**providers)
        messages, _ = assembler.assemble_with_budget(
            "t1", "hello there",
            context_window=4096, response_budget=256,
            aggressive=aggressive,
        )
        return messages[0]["content"]

    def test_block_lands_in_system_prompt(self) -> None:
        content = self._assemble(thread_ownership=lambda _t: self._CUE)
        self.assertIn(self._CUE, content)

    def test_provider_receives_user_text(self) -> None:
        seen: list[str] = []

        def provider(user_text: str) -> str:
            seen.append(user_text)
            return ""

        self._assemble(thread_ownership=provider)
        self.assertEqual(seen, ["hello there"])

    def test_sits_between_initiative_and_wants(self) -> None:
        initiative_cue = "This turn is yours."
        wants_cue = "Things you've been wanting from a conversation"
        content = self._assemble(
            initiative=lambda _t: initiative_cue,
            thread_ownership=lambda _t: self._CUE,
            wants=lambda: wants_cue,
        )
        self.assertLess(
            content.index(initiative_cue), content.index(self._CUE),
        )
        self.assertLess(
            content.index(self._CUE), content.index(wants_cue),
        )

    def test_not_dropped_under_aggressive(self) -> None:
        content = self._assemble(
            thread_ownership=lambda _t: self._CUE, aggressive=True,
        )
        self.assertIn(self._CUE, content)


if __name__ == "__main__":
    unittest.main()

"""Tests for K55 thread ownership — the pure verdict walk, the
render, the inner-life provider plumbing (via a minimal mixin host
stub), and the prompt-assembler slot wiring."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_a_long_off_topic_reply_is_moving_on(self) -> None:
        # K89: he answered *something*, at length, about something
        # else. Nudging over that is the nagging, not the persistence.
        thread = self._thread(np.array([1.0, 0.0], dtype=np.float32))
        verdict = town.evaluate_reply(
            thread, "x" * 120,
            np.array([0.0, 1.0], dtype=np.float32),
            engaged_chars=80,
        )
        self.assertEqual(verdict.verdict, town.VERDICT_MOVED_ON)

    def test_moving_on_needs_a_topical_read(self) -> None:
        # Without a cosine a long reply is indistinguishable from a
        # long answer, so it keeps the old benefit of the doubt.
        verdict = town.evaluate_reply(
            self._thread(None), "x" * 120, None, engaged_chars=80,
        )
        self.assertEqual(verdict.verdict, town.VERDICT_ENGAGED)

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


class AdvanceTests(unittest.TestCase):
    """K89 -- the stake walk. Every test here is a way of stopping."""

    def _thread(self, **kw) -> town.OwnedThread:
        return town.OwnedThread(
            topic="the bees thing", source=town.SOURCE_INITIATIVE, **kw,
        )

    def _pivot(self, cosine: float | None = 0.1) -> town.ReplyVerdict:
        return town.ReplyVerdict(town.VERDICT_PIVOT, cosine, 20)

    def test_an_answer_retires_it_without_a_cue(self) -> None:
        outcome = town.advance(
            self._thread(),
            town.ReplyVerdict(town.VERDICT_ENGAGED, 0.9, 40),
        )
        self.assertIsNone(outcome.thread)
        self.assertFalse(outcome.cue)
        self.assertEqual(outcome.reason, town.RETIRE_SATISFIED)

    def test_moving_on_retires_it_without_a_cue(self) -> None:
        outcome = town.advance(
            self._thread(),
            town.ReplyVerdict(town.VERDICT_MOVED_ON, 0.05, 200),
        )
        self.assertIsNone(outcome.thread)
        self.assertFalse(outcome.cue)
        self.assertEqual(outcome.reason, town.RETIRE_MOVED_ON)

    def test_a_brush_off_buys_a_return_and_keeps_the_thread(self) -> None:
        outcome = town.advance(self._thread(), self._pivot())
        self.assertTrue(outcome.cue)
        self.assertIsNotNone(outcome.thread)
        self.assertEqual(outcome.thread.returns_used, 1)
        self.assertAlmostEqual(outcome.thread.stake, 0.65, places=3)
        self.assertAlmostEqual(outcome.thread.last_cosine, 0.1, places=3)

    def _walk(self, **kw) -> tuple[list[bool], str]:
        """Pivot at it until it dies; return the cues and the reason."""
        thread = self._thread()
        seen: list[bool] = []
        for _ in range(6):
            outcome = town.advance(thread, self._pivot(cosine=None), **kw)
            seen.append(outcome.cue)
            if outcome.thread is None:
                return seen, outcome.reason
            thread = outcome.thread
        self.fail("thread never retired")

    def test_the_stake_alone_buys_exactly_two(self) -> None:
        # max_returns lifted out of the way, so this is the arithmetic
        # deciding rather than the cap.
        seen, reason = self._walk(max_returns=99)
        self.assertEqual(seen, [True, True])
        self.assertEqual(reason, town.RETIRE_STAKE_SPENT)

    def test_the_guard_rail_holds_when_the_stake_is_free(self) -> None:
        # A return that costs nothing must still stop at max_returns --
        # the arithmetic is the model, the cap is the promise.
        seen, reason = self._walk(stake_decay=0.0)
        self.assertEqual(seen, [True, True])
        self.assertEqual(reason, town.RETIRE_RETURNS_SPENT)

    def test_a_spent_thread_leaves_the_slot_empty(self) -> None:
        # The last return retires on its way out, so the cue can say
        # it is the last one without lying.
        thread = town.advance(self._thread(), self._pivot(None)).thread
        outcome = town.advance(thread, self._pivot(None))
        self.assertTrue(outcome.cue)
        self.assertIsNone(outcome.thread)

    def test_a_cooling_cosine_retires_the_second_return(self) -> None:
        thread = town.advance(self._thread(), self._pivot(0.20)).thread
        outcome = town.advance(thread, self._pivot(0.05))
        self.assertFalse(outcome.cue)
        self.assertIsNone(outcome.thread)
        self.assertEqual(outcome.reason, town.RETIRE_NOT_BITING)

    def test_a_steady_cosine_still_spends_the_second(self) -> None:
        thread = town.advance(self._thread(), self._pivot(0.20)).thread
        outcome = town.advance(thread, self._pivot(0.19))
        self.assertTrue(outcome.cue)

    def test_the_cooling_check_never_costs_the_first_return(self) -> None:
        # Nothing to compare against on the opening reply, however
        # cold it is -- one nudge is the K55 behaviour K89 inherits.
        outcome = town.advance(self._thread(), self._pivot(-0.4))
        self.assertTrue(outcome.cue)

    def test_an_old_thread_is_a_resurrection_not_a_return(self) -> None:
        opened = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        outcome = town.advance(
            self._thread(opened_at=opened),
            self._pivot(),
            now=opened + timedelta(minutes=90),
            max_age_minutes=45.0,
        )
        self.assertFalse(outcome.cue)
        self.assertEqual(outcome.reason, town.RETIRE_TOO_OLD)

    def test_a_fresh_thread_survives_the_age_check(self) -> None:
        opened = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        outcome = town.advance(
            self._thread(opened_at=opened),
            self._pivot(),
            now=opened + timedelta(minutes=5),
        )
        self.assertTrue(outcome.cue)

    def test_a_naive_stamp_does_not_kill_a_live_thread(self) -> None:
        outcome = town.advance(
            self._thread(opened_at=datetime(2026, 1, 1, 12, 0)),
            self._pivot(),
            now=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
        )
        self.assertTrue(outcome.cue)


class RenderTests(unittest.TestCase):
    def test_copy(self) -> None:
        block = town.render_return_block(
            "the bees thing", user_display_name="Jacob", last=False,
        )
        self.assertIn("the bees thing", block)
        self.assertIn("Jacob", block)
        self.assertIn("ONE shot", block)
        self.assertIn("one more shot later", block)

    def test_the_second_return_is_quieter_than_the_first(self) -> None:
        second = town.render_return_block(
            "the bees thing", user_display_name="Jacob", attempt=2,
        )
        self.assertIn("lighter than last time", second)
        self.assertIn("let it go for good", second)
        # Never a reproach, and never a reminder that she already asked.
        self.assertIn("don't point out that you already asked", second)

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
                thread_max_returns=2,
                thread_stake_decay=0.35,
                thread_min_stake=0.25,
                thread_max_age_minutes=45.0,
                thread_cooling_margin=0.05,
            ),
        )
        self._embedder = embedder
        self._owned_thread = None
        self.triggers: list[dict] = []

    def _queue_emotion_trigger(self, **kw) -> None:
        self.triggers.append(kw)


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

    def test_a_pivot_renders_and_keeps_the_thread_alive(self) -> None:
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
        # K89: the slot survives its first evaluation.
        self.assertIsNotNone(host._owned_thread)
        self.assertEqual(host._owned_thread.returns_used, 1)

    def test_a_second_pivot_is_the_last_one(self) -> None:
        host = _Host(
            embedder=_FakeEmbedder(
                np.array([0.0, 1.0], dtype=np.float32),
            ),
        )
        self._open_thread(
            host, embedding=np.array([1.0, 0.0], dtype=np.float32),
        )
        host._render_thread_ownership_block("anyway what about lunch")
        second = host._render_thread_ownership_block("mm, and dinner?")
        self.assertIn("lighter than last time", second)
        self.assertIsNone(host._owned_thread)
        self.assertEqual(
            host._render_thread_ownership_block("still pivoting"), "",
        )

    def test_only_the_first_brush_off_is_worth_a_sulk(self) -> None:
        host = _Host(
            embedder=_FakeEmbedder(
                np.array([0.0, 1.0], dtype=np.float32),
            ),
        )
        self._open_thread(
            host, embedding=np.array([1.0, 0.0], dtype=np.float32),
        )
        host._render_thread_ownership_block("anyway what about lunch")
        host._render_thread_ownership_block("mm, and dinner?")
        self.assertEqual(len(host.triggers), 1)
        self.assertEqual(host.triggers[0]["source"], "thread_pivot")

    def test_a_long_reply_elsewhere_retires_it_silently(self) -> None:
        host = _Host(
            embedder=_FakeEmbedder(
                np.array([0.0, 1.0], dtype=np.float32),
            ),
        )
        self._open_thread(
            host, embedding=np.array([1.0, 0.0], dtype=np.float32),
        )
        self.assertEqual(
            host._render_thread_ownership_block("x" * 200), "",
        )
        self.assertIsNone(host._owned_thread)
        self.assertEqual(host.triggers, [])

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
        self.assertIsNotNone(host._owned_thread)


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

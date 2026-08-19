"""Tests for K53 initiative turns — pure gate walk, the stateful
director, the render, and the prompt-assembler slot wiring."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.conversation import initiative_director as idir


def _decide(**overrides) -> idir.InitiativeDecision:
    kwargs = dict(
        turns_since_initiative=10,
        session_turn_count=10,
        base_period=8,
        arc="casual_check_in",
        closeness=0.6,
        comfort=0.6,
        misattunement_active=False,
        rupture_active=False,
        user_text="short message",
        substantial_chars=240,
        warmup_turns=3,
        wants_imperative_active=False,
        force=False,
    )
    kwargs.update(overrides)
    return idir.decide(**kwargs)


class DirectQuestionGateTests(unittest.TestCase):
    """K95 — a direct question is not an opening.

    The gap these close: for the whole of K92 phase 2 the stance ceiling
    recorded ``direct_question`` while this director, which could not see
    it, fired the floor-taking directive anyway — 17 of 75 measured
    renders. The fix has to hold in both directions: block on a question,
    and not cost an initiative beat when it does.
    """

    def test_a_short_question_defers_the_directive(self) -> None:
        decision = _decide(user_text="what do you think?")
        self.assertFalse(decision.fire)
        self.assertEqual(decision.reason, "direct_question")

    def test_length_alone_would_have_let_this_through(self) -> None:
        # The precise regression: well under substantial_chars, so the
        # only pre-K95 turn-shape gate had nothing to say about it.
        text = "what do you think?"
        self.assertLess(len(text), 240)
        self.assertTrue(_decide(user_text=text, force=False).reason)
        self.assertEqual(
            _decide(
                user_text=text, respect_direct_question=False,
            ).reason,
            "fire",
        )

    def test_the_dialogue_act_alone_is_enough(self) -> None:
        # No question mark; the K4 tag carries it.
        decision = _decide(
            user_text="tell me about your day", dialogue_act="question",
        )
        self.assertEqual(decision.reason, "direct_question")

    def test_trailing_whitespace_does_not_hide_the_mark(self) -> None:
        self.assertEqual(
            _decide(user_text="you okay?  \n").reason, "direct_question",
        )

    def test_a_statement_still_fires(self) -> None:
        self.assertEqual(
            _decide(user_text="the build finally passed").reason, "fire",
        )

    def test_a_long_question_reports_the_specific_reason(self) -> None:
        # Both gates apply; the more informative one should win, which
        # is why K95 sits above the length hatch.
        decision = _decide(user_text="x" * 300 + "?")
        self.assertEqual(decision.reason, "direct_question")

    def test_the_flag_restores_length_only_behaviour(self) -> None:
        decision = _decide(
            user_text="what do you think?", respect_direct_question=False,
        )
        self.assertTrue(decision.fire)

    def test_force_still_bypasses_it(self) -> None:
        # A forced repro must stay forceable, or the MCP tool cannot
        # reproduce the directive on a question turn.
        decision = _decide(user_text="what do you think?", force=True)
        self.assertTrue(decision.fire)

    def test_the_arc_block_still_outranks_it(self) -> None:
        # Ordering sanity: the safety gate is above the courtesy gate.
        self.assertEqual(
            _decide(arc="support", user_text="you okay?").reason,
            "arc_blocked",
        )

    def test_it_shares_the_predicate_with_the_stance_ceiling(self) -> None:
        """The two consumers cannot be allowed to drift apart.

        A ceiling saying ``direct_question`` while the prompt carries a
        floor-taking directive is the exact failure K95 exists to
        prevent, so this asserts the *same function* backs both rather
        than two copies that happen to agree today.
        """
        from app.core.conversation import stance as stance_mod
        from app.core.conversation import turn_shape

        for text, act in (
            ("what do you think?", None),
            ("tell me about it", "question"),
            ("the build passed", None),
            ("", None),
        ):
            with self.subTest(text=text, act=act):
                shared = turn_shape.is_direct_question(text, act)
                ceiling = stance_mod._is_direct_question(
                    stance_mod.StanceInputs(
                        blocks=frozenset(),
                        user_text=text,
                        dialogue_act=act,
                    )
                )
                gate = _decide(
                    user_text=text, dialogue_act=act,
                ).reason == "direct_question"
                self.assertEqual(shared, ceiling)
                self.assertEqual(shared, gate)


class EffectivePeriodTests(unittest.TestCase):
    def test_base(self) -> None:
        self.assertEqual(
            idir.compute_effective_period(
                8, arc="planning", closeness=0.6, comfort=0.6,
            ),
            8,
        )

    def test_light_arc_shortens(self) -> None:
        self.assertEqual(
            idir.compute_effective_period(
                8, arc="playful", closeness=0.6, comfort=0.6,
            ),
            6,
        )

    def test_cold_axes_lengthen(self) -> None:
        self.assertEqual(
            idir.compute_effective_period(
                8, arc="planning", closeness=-0.5, comfort=-0.5,
            ),
            12,
        )
        self.assertEqual(
            idir.compute_effective_period(
                8, arc="planning", closeness=0.1, comfort=0.1,
            ),
            10,
        )

    def test_floor_of_three(self) -> None:
        self.assertEqual(
            idir.compute_effective_period(
                3, arc="silly", closeness=1.0, comfort=1.0,
            ),
            3,
        )

    def test_missing_axes_neutral(self) -> None:
        # None axes read as 0 -> mean 0 < 0.25 -> +2.
        self.assertEqual(
            idir.compute_effective_period(
                8, arc="planning", closeness=None, comfort=None,
            ),
            10,
        )


class DecideTests(unittest.TestCase):
    def test_fires_when_due(self) -> None:
        decision = _decide()
        self.assertTrue(decision.fire)
        self.assertEqual(decision.reason, "fire")

    def test_support_arc_blocks(self) -> None:
        self.assertEqual(_decide(arc="support").reason, "arc_blocked")

    def test_reflection_arc_blocks(self) -> None:
        self.assertEqual(_decide(arc="reflection").reason, "arc_blocked")

    def test_misattunement_blocks(self) -> None:
        self.assertEqual(
            _decide(misattunement_active=True).reason, "misattunement",
        )

    def test_rupture_blocks(self) -> None:
        self.assertEqual(_decide(rupture_active=True).reason, "rupture")

    def test_warmup_blocks(self) -> None:
        self.assertEqual(
            _decide(session_turn_count=2).reason, "warmup",
        )

    def test_substantial_message_defers(self) -> None:
        self.assertEqual(
            _decide(user_text="x" * 300).reason, "user_substantial",
        )

    def test_not_due(self) -> None:
        self.assertEqual(
            _decide(turns_since_initiative=2).reason, "not_due",
        )

    def test_wants_imperative_defers(self) -> None:
        self.assertEqual(
            _decide(wants_imperative_active=True).reason,
            "wants_imperative_active",
        )

    def test_force_bypasses_gates(self) -> None:
        decision = _decide(
            turns_since_initiative=0,
            session_turn_count=1,
            misattunement_active=True,
            user_text="x" * 500,
            force=True,
        )
        self.assertTrue(decision.fire)

    def test_force_still_blocked_by_support_arc(self) -> None:
        decision = _decide(arc="support", force=True)
        self.assertFalse(decision.fire)
        self.assertEqual(decision.reason, "arc_blocked")


class DirectorStateTests(unittest.TestCase):
    def _kwargs(self, **overrides) -> dict:
        kwargs = dict(
            base_period=8,
            arc="planning",
            closeness=0.6,
            comfort=0.6,
            misattunement_active=False,
            rupture_active=False,
            user_text="hi",
            substantial_chars=240,
            warmup_turns=0,
            wants_imperative_active=False,
            force=False,
        )
        kwargs.update(overrides)
        return kwargs

    def test_counter_increments_and_resets_on_fire(self) -> None:
        director = idir.InitiativeDirector()
        fired_at: list[int] = []
        for turn in range(1, 20):
            decision = director.note_turn_and_decide(**self._kwargs())
            if decision.fire:
                fired_at.append(turn)
        # period 8 -> fires at turn 8 and again at 16.
        self.assertEqual(fired_at, [8, 16])

    def test_substantial_does_not_reset(self) -> None:
        director = idir.InitiativeDirector()
        for _ in range(8):
            director.note_turn_and_decide(
                **self._kwargs(user_text="x" * 500),
            )
        # Due since turn 8 but deferred every time; one short message
        # fires immediately.
        decision = director.note_turn_and_decide(**self._kwargs())
        self.assertTrue(decision.fire)

    def test_a_question_defers_the_beat_without_spending_it(self) -> None:
        """K95 must change placement, not frequency.

        This is the whole reason the gate was safe to add to a family
        whose measured problem is too *little* own material: the counter
        resets only on a real fire, so eight question turns in a row cost
        nothing and the directive lands on the first turn that is not a
        question.
        """
        director = idir.InitiativeDirector()
        for _ in range(8):
            decision = director.note_turn_and_decide(
                **self._kwargs(user_text="you around?"),
            )
            self.assertFalse(decision.fire)
            self.assertEqual(decision.reason, "direct_question")
        decision = director.note_turn_and_decide(**self._kwargs())
        self.assertTrue(decision.fire)

    def test_wants_imperative_resets_counter(self) -> None:
        director = idir.InitiativeDirector()
        for _ in range(8):
            director.note_turn_and_decide(**self._kwargs())
        # Turn 9 carries a live K52 imperative -> defer AND reset.
        decision = director.note_turn_and_decide(
            **self._kwargs(wants_imperative_active=True),
        )
        self.assertFalse(decision.fire)
        self.assertEqual(director.turns_since_initiative, 0)
        # The very next turn must NOT fire (no double floor-grab).
        decision = director.note_turn_and_decide(**self._kwargs())
        self.assertFalse(decision.fire)


class RenderTests(unittest.TestCase):
    def test_with_want(self) -> None:
        block = idir.render_block(
            "ask Jacob about the garden", user_display_name="Jacob",
        )
        self.assertIn("This turn is yours", block)
        self.assertIn("ask Jacob about the garden", block)
        self.assertIn("NOT enough this turn", block)

    def test_without_want_generic(self) -> None:
        block = idir.render_block(None, user_display_name="Jacob")
        self.assertIn("This turn is yours", block)
        self.assertIn("steer the thread", block)


class InitiativeProviderSlotTests(unittest.TestCase):
    """K53 block lands in the system prompt, receives the live
    user_text, precedes the K52 wants block, and is NOT dropped
    under ``aggressive=True`` (the director's counter advances every
    evaluated turn, so dropping the call would lose the beat)."""

    _CUE = "This turn is yours. Still answer what Jacob said."

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
            session_id="i1", role="user", content="hi", token_count=2,
        )
        assembler.set_inner_life_providers(**providers)
        messages, _ = assembler.assemble_with_budget(
            "i1", "hello there",
            context_window=4096, response_budget=256,
            aggressive=aggressive,
        )
        return messages[0]["content"]

    def test_block_lands_in_system_prompt(self) -> None:
        content = self._assemble(initiative=lambda _t: self._CUE)
        self.assertIn(self._CUE, content)

    def test_provider_receives_user_text(self) -> None:
        seen: list[str] = []

        def provider(user_text: str) -> str:
            seen.append(user_text)
            return ""

        self._assemble(initiative=provider)
        self.assertEqual(seen, ["hello there"])

    def test_block_precedes_wants(self) -> None:
        wants_cue = "Things you've been wanting from a conversation"
        content = self._assemble(
            initiative=lambda _t: self._CUE,
            wants=lambda: wants_cue,
        )
        self.assertLess(content.index(self._CUE), content.index(wants_cue))

    def test_not_dropped_under_aggressive(self) -> None:
        content = self._assemble(
            initiative=lambda _t: self._CUE, aggressive=True,
        )
        self.assertIn(self._CUE, content)


if __name__ == "__main__":
    unittest.main()

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

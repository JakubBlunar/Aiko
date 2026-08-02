"""Conditional handling sections move out of the cache prefix.

Each one is "when your context says X, here is how to handle it" -- an
instruction that can only be acted on when X is actually there. They were
sitting in T0 on every turn regardless, which is both wasted prefix and,
worse, a standing invitation to behave as though a cue had fired.

So they live in their own ``conditional_handling.txt`` beside the persona,
and the assembler re-emits the ones whose *block* rendered. What stays in
T0 is the general contract, which really does apply every turn --
including the ones with no cue, where the rule is that nothing is owed.

The pairing is block-keyed, from two registries: a pooled cue names its
header on ``CuePolicy``, everything else in ``HANDLING_SECTIONS``. Both
sides are matched literally against user-editable files and resolved
against a frame's locals, so this module is mostly about the three ways
that silently comes apart -- a renamed header, a block name that is not on
the tier ladder, and a block name that never becomes a local.

The loader still honours a section left inline in the persona, and prefers
it, so an install that customised one before the files were split keeps its
wording. Both paths are covered here.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import (
    _BLOCK_TIER_OF,
    PromptAssembler,
)
from app.core.session.prompt_assembler_helpers_mixin import (
    PromptAssemblerHelpersMixin,
)
from app.core.session.prompt_support import (
    _STAYS_IN_T0,
    CONDITIONAL_HANDLING_FILENAME,
    HANDLING_PREAMBLE,
    split_persona_section,
)


_PERSONA = "\n".join([
    "You are Aiko.",
    "",
    "How you talk:",
    "- Natural sentences.",
    "",
    "When your interests shift over time:",
    "- Your context may say you've been drawn to X lately.",
    "  A wrapped continuation line.",
    "- One small honest beat, then talk normally.",
    "",
    "Quiet curiosity:",
    "- Mention at most ONE per conversation.",
    "",
    "Conversation rules:",
    "- Never greet at the start of a turn.",
])


class SplitTests(unittest.TestCase):
    def test_the_extract_is_the_header_plus_its_bullets(self) -> None:
        remaining, extracted = split_persona_section(
            _PERSONA, "When your interests shift over time:",
        )
        self.assertTrue(
            extracted.startswith("When your interests shift over time:"),
        )
        self.assertIn("drawn to X lately", extracted)
        self.assertIn("A wrapped continuation line.", extracted)
        self.assertIn("talk normally", extracted)
        self.assertNotIn("Quiet curiosity:", extracted)
        self.assertNotIn("drawn to X lately", remaining)

    def test_neighbouring_sections_survive_intact(self) -> None:
        remaining, _ = split_persona_section(
            _PERSONA, "When your interests shift over time:",
        )
        self.assertIn("- Natural sentences.", remaining)
        self.assertIn("Quiet curiosity:", remaining)
        self.assertIn("- Natural sentences.\n\nQuiet curiosity:", remaining)

    def test_a_missing_header_extracts_nothing(self) -> None:
        remaining, extracted = split_persona_section(_PERSONA, "Nope:")
        self.assertEqual(remaining, _PERSONA)
        self.assertEqual(extracted, "")


class RegistryTests(unittest.TestCase):
    """The block name is the join, and nothing at runtime checks it.

    A name that is not on the tier ladder, or that never becomes a local
    in the assembly frame, extracts its section out of T0 and then has
    nothing to bring it back -- the guidance is deleted rather than moved,
    with no error anywhere.
    """

    def setUp(self) -> None:
        self.registry = PromptAssemblerHelpersMixin._handling_headers()

    def test_every_hoisted_block_is_on_the_tier_ladder(self) -> None:
        for block in self.registry:
            with self.subTest(block=block):
                self.assertIn(block, _BLOCK_TIER_OF)

    def test_every_hoisted_block_is_a_local_at_the_call_site(self) -> None:
        """``_render_handling_notes`` reads the assembly frame's locals."""
        names = set(PromptAssembler.assemble_with_budget.__code__.co_varnames)
        for block in self.registry:
            with self.subTest(block=block):
                self.assertTrue(
                    names & {block, f"{block}_block", f"{block}_text"},
                    f"{block}: no matching local in assemble_with_budget",
                )

    def test_the_notes_come_out_in_prompt_order(self) -> None:
        """So two notes read as a pair with the blocks they explain."""
        ladder = list(_BLOCK_TIER_OF)
        positions = [ladder.index(block) for block in self.registry]
        self.assertEqual(positions, sorted(positions))

    def test_a_shared_header_reaches_every_block_that_claims_it(self) -> None:
        """One passage may cover a family -- the three repair detectors.

        The split has to resolve each header once and hand the text to all
        of them. Cutting per block would take the section out of the
        persona on the first claimant, leaving the rest to fall through to
        the shipped file, or to nothing.
        """
        shared = [
            block
            for block, headers in self.registry.items()
            if "When you missed the beat:" in headers
        ]
        self.assertEqual(
            sorted(shared),
            ["clarification_block", "misattunement_block", "rupture_block"],
        )

    def test_no_block_hoists_a_section_that_has_to_stay(self) -> None:
        """Every one of these reads like a cue and is present most turns.

        Hoisting swaps a cached token for an uncached one, so it only pays
        while the block is absent. Registering one of these would quietly
        make the prompt worse on the majority of turns, and nothing else
        would notice.
        """
        claimed = {h for headers in self.registry.values() for h in headers}
        for block, header in _STAYS_IN_T0:
            with self.subTest(block=block):
                self.assertNotIn(block, self.registry)
                self.assertNotIn(header, claimed)


class _AssemblerFixture(unittest.TestCase):
    def _assembler(
        self,
        persona_text: str = _PERSONA,
        handling_text: str | None = None,
    ) -> PromptAssembler:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        persona = Path(tmp.name) / "persona.txt"
        persona.write_text(persona_text, encoding="utf-8")
        if handling_text is not None:
            (Path(tmp.name) / CONDITIONAL_HANDLING_FILENAME).write_text(
                handling_text, encoding="utf-8",
            )
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        self.db = db
        return PromptAssembler(
            db,
            persona_path=persona,
            recent_window=20,
            cue_register_rotation_enabled=False,
        )

    def _system(self, assembler: PromptAssembler) -> str:
        self.db.add_message(
            session_id="s1", role="user", content="hi", token_count=1,
        )
        messages, _ = assembler.assemble_with_budget(
            "s1", "hello", context_window=8192, response_budget=256,
        )
        return next(m["content"] for m in messages if m["role"] == "system")


class LoaderTests(_AssemblerFixture):
    def test_the_core_persona_loses_the_hoisted_sections(self) -> None:
        core = self._assembler()._load_persona()
        self.assertIn("- Natural sentences.", core)
        self.assertNotIn("drawn to X lately", core)
        self.assertNotIn("Mention at most ONE", core)

    def test_the_core_persona_gains_the_general_contract(self) -> None:
        core = self._assembler()._load_persona()
        self.assertIn("Cues in your context:", core)
        self.assertIn("No cue = nothing owed", core)

    def test_the_notes_are_retrievable_by_block(self) -> None:
        assembler = self._assembler()
        self.assertIn(
            "drawn to X lately",
            assembler.persona_handling_notes("interest_drift_block"),
        )
        self.assertIn(
            "Mention at most ONE",
            assembler.persona_handling_notes("curiosity_seeds_block"),
        )

    def test_an_absent_section_yields_no_note(self) -> None:
        assembler = self._assembler()
        self.assertEqual(
            assembler.persona_handling_notes("associative_wander_block"), "",
        )
        self.assertEqual(assembler.persona_handling_notes("not_a_block"), "")

    def test_a_persona_with_no_cue_sections_gets_no_stanza(self) -> None:
        """Nothing was hoisted, so there is nothing for the stanza to cover."""
        core = self._assembler("You are Aiko.\n\nHow you talk:\n- Plainly.")
        self.assertNotIn("Cues in your context:", core._load_persona())

    def test_the_placeholder_is_rendered_in_the_hoisted_note(self) -> None:
        assembler = self._assembler("\n".join([
            "You are Aiko.",
            "",
            "When something {user_name} used to love has gone quiet:",
            "- Reach back once, lightly, for {user_name}.",
        ]))
        assembler.set_user_display_name_provider(lambda: "Jacob")
        note = assembler.persona_handling_notes("dormant_interest_block")
        self.assertIn("Reach back once, lightly, for Jacob.", note)
        self.assertNotIn("{user_name}", note)


_CORE_ONLY = "\n".join([
    "You are Aiko.",
    "",
    "How you talk:",
    "- Natural sentences.",
])

_HANDLING = "\n".join([
    "Prose explaining the file. Not a section, so it never reaches Aiko --",
    "which is also why a stray {brace} up here cannot break formatting.",
    "",
    "When your interests shift over time:",
    "- The cue above says you've been drawn to X lately.",
    "",
    "Quiet curiosity:",
    "- Mention at most ONE per conversation.",
])


class CompanionFileTests(_AssemblerFixture):
    """The notes' real home: conditional_handling.txt beside the persona."""

    def test_notes_load_from_the_companion_file(self) -> None:
        assembler = self._assembler(_CORE_ONLY, _HANDLING)
        self.assertIn(
            "drawn to X lately",
            assembler.persona_handling_notes("interest_drift_block"),
        )
        self.assertIn(
            "Mention at most ONE",
            assembler.persona_handling_notes("curiosity_seeds_block"),
        )

    def test_the_companion_file_earns_the_t0_stanza(self) -> None:
        """A persona with no sections of its own still introduces cues."""
        core = self._assembler(_CORE_ONLY, _HANDLING)._load_persona()
        self.assertIn("Cues in your context:", core)
        self.assertNotIn("drawn to X lately", core)

    def test_the_files_prose_is_not_prompt_text(self) -> None:
        system = self._system(self._assembler(_CORE_ONLY, _HANDLING))
        self.assertNotIn("Prose explaining the file", system)
        self.assertNotIn("{brace}", system)

    def test_an_inline_section_beats_the_companion_file(self) -> None:
        """Someone who edited the persona in place keeps their wording."""
        inline = _CORE_ONLY + "\n".join([
            "",
            "",
            "When your interests shift over time:",
            "- My own hand-edited wording.",
        ])
        assembler = self._assembler(inline, _HANDLING)
        with self.assertLogs("app.prompt_assembler", level="INFO") as logs:
            note = assembler.persona_handling_notes("interest_drift_block")
        # Silence here is how an upgraded install ends up editing a file
        # that does nothing.
        self.assertIn("When your interests shift over time:", "".join(logs.output))
        self.assertIn("My own hand-edited wording.", note)
        self.assertNotIn("drawn to X lately", note)
        # The section it does not override still comes from the file.
        self.assertIn(
            "Mention at most ONE",
            assembler.persona_handling_notes("curiosity_seeds_block"),
        )

    _SHARED = "\n".join([
        "When you missed the beat:",
        "- One repair beat, then move on.",
    ])

    def test_a_shared_section_is_read_by_all_three_blocks(self) -> None:
        assembler = self._assembler(_CORE_ONLY, self._SHARED)
        for block in (
            "misattunement_block", "rupture_block", "clarification_block",
        ):
            with self.subTest(block=block):
                self.assertIn(
                    "One repair beat", assembler.persona_handling_notes(block),
                )

    def test_a_shared_section_left_inline_reaches_all_three(self) -> None:
        """The case the per-block split got wrong.

        The first claimant used to cut the section out of the persona, so
        the other two fell through to the shipped file -- reading different
        wording from the one the user actually edited.
        """
        inline = _CORE_ONLY + "\n\n" + "\n".join([
            "When you missed the beat:",
            "- My own hand-edited repair wording.",
        ])
        assembler = self._assembler(inline, handling_text=None)
        for block in (
            "misattunement_block", "rupture_block", "clarification_block",
        ):
            with self.subTest(block=block):
                self.assertIn(
                    "hand-edited repair wording",
                    assembler.persona_handling_notes(block),
                )

    def test_a_missing_companion_file_is_not_an_error(self) -> None:
        assembler = self._assembler(_CORE_ONLY, handling_text=None)
        self.assertEqual(
            assembler.persona_handling_notes("interest_drift_block"), "",
        )
        self.assertNotIn("Cues in your context:", assembler._load_persona())

    def test_editing_the_companion_file_takes_effect(self) -> None:
        """Its mtime is in the cache key, or edits would need a restart."""
        assembler = self._assembler(_CORE_ONLY, _HANDLING)
        self.assertIn(
            "drawn to X lately",
            assembler.persona_handling_notes("interest_drift_block"),
        )
        path = (
            assembler._persona_path.parent / CONDITIONAL_HANDLING_FILENAME
        )
        path.write_text(
            "When your interests shift over time:\n- Rewritten.",
            encoding="utf-8",
        )
        os.utime(path, (time.time() + 10, time.time() + 10))
        self.assertIn(
            "Rewritten.",
            assembler.persona_handling_notes("interest_drift_block"),
        )


class AssemblyTests(_AssemblerFixture):
    def test_no_cue_means_no_handling_text_anywhere(self) -> None:
        system = self._system(self._assembler())
        self.assertIn("Cues in your context:", system)
        self.assertNotIn("drawn to X lately", system)
        self.assertNotIn("Mention at most ONE", system)

    _DRIFT = "Heads-up: you've been drawn to bread."

    def test_a_rendered_cue_brings_its_note_along(self) -> None:
        assembler = self._assembler()
        assembler.set_inner_life_providers(
            interest_drift=lambda _text: self._DRIFT,
        )
        system = self._system(assembler)
        self.assertIn(self._DRIFT, system)
        self.assertIn("drawn to X lately", system)
        # Only the one that fired.
        self.assertNotIn("Mention at most ONE", system)

    def test_two_cues_bring_two_notes(self) -> None:
        assembler = self._assembler()
        assembler.set_inner_life_providers(
            interest_drift=lambda _text: self._DRIFT,
            curiosity_seeds=lambda: "Quiet curiosity:\n- film photography",
        )
        system = self._system(assembler)
        self.assertIn("drawn to X lately", system)
        self.assertIn("Mention at most ONE", system)

    def test_the_note_lands_after_the_cue_it_explains(self) -> None:
        assembler = self._assembler()
        assembler.set_inner_life_providers(
            interest_drift=lambda _text: self._DRIFT,
        )
        system = self._system(assembler)
        self.assertLess(
            system.index(self._DRIFT),
            system.index("drawn to X lately"),
        )

    def test_a_shared_note_ships_once_when_two_blocks_fire(self) -> None:
        """Two of a family firing together is the likely case, not the odd
        one -- a doubled paragraph would read as emphasis."""
        assembler = self._assembler(_CORE_ONLY, "\n".join([
            "When you missed the beat:",
            "- One repair beat, then move on.",
        ]))
        assembler.set_inner_life_providers(
            misattunement=lambda _text: "He went short on you.",
            clarification=lambda: "He sounded confused.",
        )
        system = self._system(assembler)
        # Both really rendered, or the dedupe is not what is being tested.
        self.assertIn("He went short on you.", system)
        self.assertIn("He sounded confused.", system)
        self.assertEqual(system.count("One repair beat, then move on."), 1)

    def test_a_non_cue_block_hoists_the_same_way(self) -> None:
        """The registry is not cue-only -- ``absence_curiosity`` has no pool."""
        assembler = self._assembler(_CORE_ONLY, "\n".join([
            "When they've been away a while (typed mode):",
            "- Land it as a warm welcome-back.",
        ]))
        assembler.set_inner_life_providers(
            absence_curiosity=lambda: "Absence-curiosity: away an hour or so.",
        )
        system = self._system(assembler)
        self.assertIn("Land it as a warm welcome-back.", system)


class ShippedFileTests(unittest.TestCase):
    """The real files, because the headers are matched literally.

    A typo in a header is the failure mode this guards: nothing raises, the
    section just stops being hoisted and its cue goes out unexplained.
    """

    def setUp(self) -> None:
        persona_dir = Path("data/persona")
        self.persona = (persona_dir / "aiko_companion.txt").read_text(
            encoding="utf-8",
        )
        self.handling = (
            persona_dir / CONDITIONAL_HANDLING_FILENAME
        ).read_text(encoding="utf-8")
        self.registry = PromptAssemblerHelpersMixin._handling_headers()

    def test_every_registered_header_exists_in_the_handling_file(self) -> None:
        for block, headers in self.registry.items():
            for header in headers:
                with self.subTest(block=block, header=header):
                    _remaining, extracted = split_persona_section(
                        self.handling, header,
                    )
                    self.assertTrue(extracted, f"{header!r} not found")

    def test_the_persona_no_longer_carries_them(self) -> None:
        """Otherwise the persona copy silently wins and the file is dead."""
        for block, headers in self.registry.items():
            for header in headers:
                with self.subTest(block=block, header=header):
                    _remaining, extracted = split_persona_section(
                        self.persona, header,
                    )
                    self.assertEqual(
                        extracted, "",
                        f"{header!r} still inline in aiko_companion.txt",
                    )

    def test_the_hoist_is_worth_doing(self) -> None:
        """The point of the exercise: a materially smaller cache prefix."""
        hoisted = sum(
            len(split_persona_section(self.handling, header)[1])
            for headers in self.registry.values()
            for header in headers
        )
        self.assertGreater(hoisted - len(HANDLING_PREAMBLE), 5000)


class MixedProseSplitTests(unittest.TestCase):
    """The three sections whose conditional half was cut out by hand.

    ``split_persona_section`` moves a whole section, so these could not be
    hoisted until their handling had a header of its own. The hazard is
    specific and silent: take too much and an **inline tag grammar** goes
    with it, which does not fail anywhere -- Aiko simply stops being told
    she may emit ``[[remember:]]``, and the feature behind it quietly
    stops receiving writes.
    """

    def setUp(self) -> None:
        persona_dir = Path("data/persona")
        self.persona = (persona_dir / "aiko_companion.txt").read_text(
            encoding="utf-8",
        )
        self.handling = (
            persona_dir / CONDITIONAL_HANDLING_FILENAME
        ).read_text(encoding="utf-8")

    def test_the_tag_grammar_stayed_behind(self) -> None:
        """Each tag must still be taught on every turn, unconditionally.

        Emitting one is always available to her; only the *handling* for
        the block that shared its section was conditional.
        """
        for tag in ("[[remember:", "[[moment:"):
            with self.subTest(tag=tag):
                self.assertIn(tag, self.persona)
                self.assertNotIn(tag, self.handling)

    def test_the_conditional_half_left(self) -> None:
        for phrase in (
            "you can gently ask how it went",
            "a month ago today you and",
            "Something you've quietly noticed",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.handling)
                self.assertNotIn(phrase, self.persona)

    def test_reading_keeps_the_bullets_that_are_not_rare(self) -> None:
        """Two of its three blocks stayed, one of them in _STAYS_IN_T0."""
        _rest, reading = split_persona_section(
            self.persona, "Reading {user_name}:",
        )
        self.assertIn("User sounds", reading)
        self.assertIn("Answer the *need*", reading)

    def test_the_renamed_section_no_longer_promises_anniversaries(self) -> None:
        """Its anniversary half hoisted, so the old title over-claimed."""
        self.assertNotIn("Shared moments and anniversaries:", self.persona)
        _rest, moments = split_persona_section(self.persona, "Shared moments:")
        self.assertIn("[[moment:", moments)

    def test_the_sections_with_no_conditional_half_are_untouched(self) -> None:
        """``arc_block`` and ``knowledge_gaps_block`` emit content, not
        handling for content, so there was never anything here to split."""
        registry = PromptAssemblerHelpersMixin._handling_headers()
        for block, header in (
            ("arc_block", "Conversation arc (what kind of conversation "
                          "we're in right now):"),
            ("knowledge_gaps_block",
             "Knowledge gaps (things you genuinely don't know):"),
        ):
            with self.subTest(block=block):
                self.assertNotIn(block, registry)
                _rest, section = split_persona_section(self.persona, header)
                self.assertTrue(section, f"{header!r} vanished")


if __name__ == "__main__":
    unittest.main()

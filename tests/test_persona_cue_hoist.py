"""The seven cue-handling sections move out of the cache prefix.

Each one is "when your context says X, here is how to handle it" -- an
instruction that can only be acted on when X is actually there. They were
sitting in T0 on every turn regardless, which is both wasted prefix and,
worse, seven standing invitations to behave as though a cue had fired.

So they live in their own ``cue_handling.txt`` beside the persona, and the
assembler re-emits the ones whose cue rendered. What stays in T0 is the
general contract, which really does apply every turn -- including the ones
with no cue, where the rule is that nothing is owed.

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
from app.core.proactive.cue_accounting import CUE_POLICIES
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.prompt_support import (
    CUE_HANDLING_FILENAME,
    CUE_HANDLING_PREAMBLE,
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
            (Path(tmp.name) / CUE_HANDLING_FILENAME).write_text(
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

    def test_the_notes_are_retrievable_by_cue_type(self) -> None:
        assembler = self._assembler()
        self.assertIn(
            "drawn to X lately",
            assembler.persona_cue_handling("interest_drift"),
        )
        self.assertIn(
            "Mention at most ONE",
            assembler.persona_cue_handling("curiosity_seed"),
        )

    def test_an_absent_section_yields_no_note(self) -> None:
        assembler = self._assembler()
        self.assertEqual(
            assembler.persona_cue_handling("associative_wander"), "",
        )
        self.assertEqual(assembler.persona_cue_handling("not_a_cue"), "")

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
        note = assembler.persona_cue_handling("dormant_interest")
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
    """The notes' real home: cue_handling.txt next to the persona."""

    def test_notes_load_from_the_companion_file(self) -> None:
        assembler = self._assembler(_CORE_ONLY, _HANDLING)
        self.assertIn(
            "drawn to X lately",
            assembler.persona_cue_handling("interest_drift"),
        )
        self.assertIn(
            "Mention at most ONE",
            assembler.persona_cue_handling("curiosity_seed"),
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
            note = assembler.persona_cue_handling("interest_drift")
        # Silence here is how an upgraded install ends up editing a file
        # that does nothing.
        self.assertIn("interest_drift", "".join(logs.output))
        self.assertIn("My own hand-edited wording.", note)
        self.assertNotIn("drawn to X lately", note)
        # The section it does not override still comes from the file.
        self.assertIn(
            "Mention at most ONE",
            assembler.persona_cue_handling("curiosity_seed"),
        )

    def test_a_missing_companion_file_is_not_an_error(self) -> None:
        assembler = self._assembler(_CORE_ONLY, handling_text=None)
        self.assertEqual(assembler.persona_cue_handling("interest_drift"), "")
        self.assertNotIn("Cues in your context:", assembler._load_persona())

    def test_editing_the_companion_file_takes_effect(self) -> None:
        """Its mtime is in the cache key, or edits would need a restart."""
        assembler = self._assembler(_CORE_ONLY, _HANDLING)
        self.assertIn(
            "drawn to X lately",
            assembler.persona_cue_handling("interest_drift"),
        )
        path = assembler._persona_path.parent / CUE_HANDLING_FILENAME
        path.write_text(
            "When your interests shift over time:\n- Rewritten.",
            encoding="utf-8",
        )
        os.utime(path, (time.time() + 10, time.time() + 10))
        self.assertIn(
            "Rewritten.", assembler.persona_cue_handling("interest_drift"),
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
        self.handling = (persona_dir / CUE_HANDLING_FILENAME).read_text(
            encoding="utf-8",
        )

    def test_every_policy_header_exists_in_the_handling_file(self) -> None:
        for cue_type, policy in CUE_POLICIES.items():
            with self.subTest(cue=cue_type):
                self.assertTrue(policy.handling_section)
                _remaining, extracted = split_persona_section(
                    self.handling, policy.handling_section,
                )
                self.assertTrue(
                    extracted,
                    f"{cue_type}: {policy.handling_section!r} not found",
                )

    def test_the_persona_no_longer_carries_them(self) -> None:
        """Otherwise the persona copy silently wins and the file is dead."""
        for cue_type, policy in CUE_POLICIES.items():
            with self.subTest(cue=cue_type):
                _remaining, extracted = split_persona_section(
                    self.persona, policy.handling_section,
                )
                self.assertEqual(
                    extracted, "",
                    f"{cue_type}: still inline in aiko_companion.txt",
                )

    def test_the_hoist_is_worth_doing(self) -> None:
        """The point of the exercise: a materially smaller cache prefix."""
        hoisted = sum(
            len(split_persona_section(self.handling, p.handling_section)[1])
            for p in CUE_POLICIES.values()
        )
        self.assertGreater(hoisted - len(CUE_HANDLING_PREAMBLE), 2000)


if __name__ == "__main__":
    unittest.main()

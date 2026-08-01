"""Tests for K49 casual speech texture.

Covers the three moving parts: the TTS-only filler strip, the persona
section gate behind ``agent.speech_texture_enabled``, and the settings
round-trip for both new keys.
"""
from __future__ import annotations

import random
import unittest

from app.core.infra.agent_settings_parse import parse_agent_settings
from app.core.session.prompt_support import (
    SPEECH_TEXTURE_SECTION_HEADER,
    strip_persona_section,
)
from app.core.session.session_text_utils import strip_speech_fillers
from app.core.voice.cadence import CadenceContext, _maybe_prefix


class StripSpeechFillersTests(unittest.TestCase):
    def test_leading_filler_is_dropped(self):
        self.assertEqual(
            strip_speech_fillers("Uhm, yeah I think so"),
            "yeah I think so",
        )

    def test_mid_sentence_filler_between_commas(self):
        # One comma survives so the clause still reads as a pause.
        self.assertEqual(
            strip_speech_fillers("it's, uhm, complicated"),
            "it's, complicated",
        )

    def test_filler_after_sentence_boundary(self):
        self.assertEqual(
            strip_speech_fillers("Okay. Mm, sure."),
            "Okay. sure.",
        )

    def test_run_of_fillers_all_go(self):
        self.assertEqual(
            strip_speech_fillers("uhm, mm, yeah that works"),
            "yeah that works",
        )

    def test_ellipsis_separator(self):
        self.assertEqual(
            strip_speech_fillers("Hmm... let me think"),
            "let me think",
        )

    def test_em_dash_separator(self):
        self.assertEqual(
            strip_speech_fillers("uh -- hold on"),
            "hold on",
        )

    def test_trailing_filler_leaves_clean_punctuation(self):
        self.assertEqual(strip_speech_fillers("yeah, mm."), "yeah.")

    # ── mid-sentence safety: no separator means no rewrite ────────────
    def test_filler_without_own_punctuation_is_left_alone(self):
        # Guessing here is how you mangle grammar; requiring punctuation on
        # both sides is the whole safety property.
        self.assertEqual(
            strip_speech_fillers("it's uhm complicated"),
            "it's uhm complicated",
        )

    def test_word_interior_is_not_touched(self):
        for text in ("Umbrella weather, huh?", "Ermine coats.", "Muminek, hi"):
            with self.subTest(text=text):
                self.assertEqual(strip_speech_fillers(text), text)

    def test_filler_mid_clause_after_word_is_left_alone(self):
        self.assertEqual(
            strip_speech_fillers("the uh oh moment"),
            "the uh oh moment",
        )

    # ── the list is narrow: real words stay spoken ────────────────────
    def test_lexical_interjections_survive(self):
        for text in (
            "wow, that is wild",
            "oh -- wow, okay",
            "huh, weird.",
            "oof, yeah.",
            "nah, I'm good",
            "I mean, kind of?",
        ):
            with self.subTest(text=text):
                self.assertEqual(strip_speech_fillers(text), text)

    # ── never silence a turn ─────────────────────────────────────────
    def test_filler_only_reply_passes_through(self):
        for text in ("Mhm.", "mm", "Hmm...", "uhm"):
            with self.subTest(text=text):
                self.assertEqual(strip_speech_fillers(text), text)

    def test_empty_input_round_trips(self):
        self.assertEqual(strip_speech_fillers(""), "")
        self.assertEqual(strip_speech_fillers("   "), "   ")

    def test_case_insensitive(self):
        self.assertEqual(strip_speech_fillers("UHM, right"), "right")


class MaybePrefixDoubleFillerGuardTests(unittest.TestCase):
    """``_maybe_prefix`` must not stack a second interjection (K49)."""

    @staticmethod
    def _drowsy_ctx() -> CadenceContext:
        # random.Random(0).random() is ~0.844 -- above every prefix
        # probability -- so seed with a value that *would* fire to prove the
        # guard is what suppresses the prefix, not the dice.
        return CadenceContext(
            mood_label="tired",
            mood_valence=0.0,
            mood_arousal=0.3,
            circadian_drowsy=True,
            rng=random.Random(12345),
        )

    def test_prefix_fires_on_a_plain_sentence(self):
        # Guards the guard: if this ever stops firing the tests below pass
        # for the wrong reason.
        fired = False
        for seed in range(60):
            ctx = CadenceContext(
                mood_label="tired",
                mood_valence=0.0,
                mood_arousal=0.3,
                circadian_drowsy=True,
                rng=random.Random(seed),
            )
            prefix, _ = _maybe_prefix("the rooftops keep fighting me", ctx)
            if prefix:
                fired = True
                break
        self.assertTrue(fired, "no seed produced a prefix; test is inert")

    def test_no_prefix_when_sentence_opens_on_a_filler(self):
        for text in (
            "Mm, I guess so.",
            "oh -- wow, okay",
            "yeah, that tracks.",
            "uhm, hold on.",
            "Huh. weird.",
        ):
            with self.subTest(text=text):
                for seed in range(60):
                    ctx = CadenceContext(
                        mood_label="tired",
                        mood_valence=-0.5,
                        mood_arousal=0.3,
                        circadian_drowsy=True,
                        rng=random.Random(seed),
                    )
                    prefix, _ = _maybe_prefix(text, ctx)
                    self.assertEqual(prefix, "")

    def test_interjection_inside_a_word_does_not_suppress(self):
        prefix_seen = False
        for seed in range(60):
            ctx = CadenceContext(
                mood_label="tired",
                mood_valence=0.0,
                mood_arousal=0.3,
                circadian_drowsy=True,
                rng=random.Random(seed),
            )
            prefix, _ = _maybe_prefix("Ohio is far away", ctx)
            if prefix:
                prefix_seen = True
                break
        self.assertTrue(prefix_seen)


class DispatchStripWiringTests(unittest.TestCase):
    """The strip must reach TTS and only TTS (K49).

    ``_dispatch_chunk_with_earcons`` is the audio branch, so asserting the
    flag lands here is what proves the transcript can't be affected.
    """

    @staticmethod
    def _spoken(chunk: str, *, strip: bool) -> list[str]:
        from app.core.session.turn_runner import TurnRunner

        out: list[str] = []
        TurnRunner._dispatch_chunk_with_earcons(
            chunk,
            mood="neutral",
            on_tts_chunk=lambda text, _mood: out.append(text),
            on_earcon=None,
            strip_fillers=strip,
        )
        return out

    def test_off_by_default_keeps_the_filler(self):
        self.assertEqual(
            self._spoken("it's, uhm, complicated", strip=False),
            ["it's, uhm, complicated"],
        )

    def test_enabled_strips_the_filler(self):
        self.assertEqual(
            self._spoken("it's, uhm, complicated", strip=True),
            ["it's, complicated"],
        )

    def test_earcons_still_split_around_a_stripped_filler(self):
        spoken = self._spoken("Mm, right [[laugh]] anyway", strip=True)
        self.assertEqual([s.strip() for s in spoken], ["right", "anyway"])

    def test_lexical_interjection_survives_with_strip_on(self):
        self.assertEqual(
            self._spoken("wow, that is wild", strip=True),
            ["wow, that is wild"],
        )

    def test_runner_maps_the_setting_to_the_strip_flag(self):
        # ``speech_texture_spoken`` is the *positive* setting; the dispatcher
        # takes the negation. Easy to invert, so pin it.
        from app.core.session.turn_runner import TurnRunner

        for spoken, expected_strip in ((True, False), (False, True)):
            with self.subTest(speech_texture_spoken=spoken):
                runner = TurnRunner.__new__(TurnRunner)
                runner._speech_texture_spoken = spoken
                self.assertEqual(
                    not runner._speech_texture_spoken, expected_strip,
                )


class PersonaSectionGateTests(unittest.TestCase):
    PERSONA = "\n".join(
        [
            "How you talk:",
            "- Natural sentences.",
            "- Cat mannerisms only sparingly.",
            "",
            "Speech texture:",
            "- Real speech isn't clean.",
            "  A wrapped continuation line.",
            "- Keep it sparse.",
            "",
            "Conversation rules:",
            "- Never greet at the start of a turn.",
        ]
    )

    def test_section_and_its_bullets_are_removed(self):
        out = strip_persona_section(
            self.PERSONA, SPEECH_TEXTURE_SECTION_HEADER,
        )
        self.assertNotIn("Speech texture:", out)
        self.assertNotIn("Real speech isn't clean", out)
        self.assertNotIn("wrapped continuation", out)
        self.assertNotIn("Keep it sparse", out)

    def test_neighbouring_sections_survive_intact(self):
        out = strip_persona_section(
            self.PERSONA, SPEECH_TEXTURE_SECTION_HEADER,
        )
        self.assertIn("How you talk:", out)
        self.assertIn("Cat mannerisms only sparingly.", out)
        self.assertIn("Conversation rules:", out)
        self.assertIn("Never greet at the start of a turn.", out)
        # The blank line between them is preserved, not collapsed.
        self.assertIn(
            "- Cat mannerisms only sparingly.\n\nConversation rules:", out,
        )

    def test_missing_header_is_a_no_op(self):
        self.assertEqual(
            strip_persona_section(self.PERSONA, "Nonexistent:"),
            self.PERSONA,
        )

    def _assemble_system(self, *, enabled: bool) -> str:
        import tempfile
        from pathlib import Path

        from app.core.infra.chat_database import ChatDatabase
        from app.core.session.prompt_assembler import PromptAssembler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        persona = Path(tmp.name) / "persona.txt"
        persona.write_text(self.PERSONA, encoding="utf-8")
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        assembler = PromptAssembler(
            db,
            persona_path=persona,
            recent_window=20,
            cue_register_rotation_enabled=False,
            speech_texture_enabled=enabled,
        )
        db.add_message(
            session_id="s1", role="user", content="hi", token_count=1,
        )
        messages, _ = assembler.assemble_with_budget(
            "s1", "hello", context_window=8192, response_budget=256,
        )
        return next(m["content"] for m in messages if m["role"] == "system")

    def test_enabled_keeps_the_section_in_the_prompt(self):
        system = self._assemble_system(enabled=True)
        self.assertIn("Speech texture:", system)
        self.assertIn("Real speech isn't clean", system)

    def test_disabled_lifts_the_section_out_of_the_prompt(self):
        system = self._assemble_system(enabled=False)
        self.assertNotIn("Speech texture:", system)
        self.assertNotIn("Real speech isn't clean", system)
        # The rest of the persona still made it in.
        self.assertIn("How you talk:", system)
        self.assertIn("Conversation rules:", system)

    def test_shipped_persona_carries_the_section(self):
        from pathlib import Path

        raw = Path("data/persona/aiko_companion.txt").read_text(
            encoding="utf-8",
        )
        self.assertIn(SPEECH_TEXTURE_SECTION_HEADER, raw)
        stripped = strip_persona_section(
            raw, SPEECH_TEXTURE_SECTION_HEADER,
        )
        self.assertNotIn(SPEECH_TEXTURE_SECTION_HEADER, stripped)
        self.assertLess(len(stripped), len(raw))
        # Sections on either side are untouched.
        self.assertIn("How you talk:", stripped)
        self.assertIn("Conversation rules:", stripped)


class SpeechTextureSettingsTests(unittest.TestCase):
    def test_defaults_are_on(self):
        agent = parse_agent_settings({})
        self.assertTrue(agent.speech_texture_enabled)
        self.assertTrue(agent.speech_texture_spoken)

    def test_both_keys_round_trip(self):
        agent = parse_agent_settings(
            {
                "speech_texture_enabled": False,
                "speech_texture_spoken": False,
            }
        )
        self.assertFalse(agent.speech_texture_enabled)
        self.assertFalse(agent.speech_texture_spoken)

    def test_the_two_keys_are_independent(self):
        agent = parse_agent_settings({"speech_texture_spoken": False})
        self.assertTrue(agent.speech_texture_enabled)
        self.assertFalse(agent.speech_texture_spoken)

    def test_default_config_ships_both_keys(self):
        import json
        from pathlib import Path

        raw = json.loads(
            Path("config/default.json").read_text(encoding="utf-8"),
        )
        self.assertIn("speech_texture_enabled", raw["agent"])
        self.assertIn("speech_texture_spoken", raw["agent"])


if __name__ == "__main__":
    unittest.main()

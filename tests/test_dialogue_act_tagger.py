"""K4 tests: regex coverage, LLM fallback shape, async upgrade flow."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.chat_database import ChatDatabase
from app.core.dialogue_act_tagger import (
    DialogueActResult,
    DialogueActTagger,
    VALID_DIALOGUE_ACTS,
    _parse_llm_payload,
    tag_regex,
)


class _FakeOllama:
    def __init__(self, response: str = '{"act":"story","confidence":0.8}') -> None:
        self.response = response
        self.fail = False
        self.calls: list[dict] = []

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm fail")
        return self.response


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")

    def add_user(self, text: str = "hello") -> int:
        return self.db.add_message(
            session_id="s1", role="user", content=text,
        )

    def close(self) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class RegexCoverageTests(unittest.TestCase):
    def test_question_marks_tag_question(self) -> None:
        out = tag_regex("hey, what's the weather like today?")
        self.assertEqual(out.act, "question")
        self.assertEqual(out.source, "regex")
        self.assertGreater(out.confidence, 0.6)

    def test_wh_word_at_start_tags_question(self) -> None:
        out = tag_regex("how does that even work")
        self.assertEqual(out.act, "question")

    def test_planning_phrase_tags_planning(self) -> None:
        out = tag_regex("let's plan the launch step by step")
        self.assertEqual(out.act, "planning")
        self.assertGreater(out.confidence, 0.6)

    def test_what_if_we_tags_planning(self) -> None:
        out = tag_regex("what if we tried it differently next sprint")
        self.assertEqual(out.act, "planning")

    def test_banter_lol_tags_banter(self) -> None:
        out = tag_regex("lol you're ridiculous")
        self.assertEqual(out.act, "banter")

    def test_vent_lexical_tags_vent(self) -> None:
        out = tag_regex("i hate when this happens, rough day")
        self.assertEqual(out.act, "vent")
        self.assertGreater(out.confidence, 0.7)

    def test_vent_loudness_tags_vent(self) -> None:
        # Repeated punctuation alone is enough on the loudness track.
        out = tag_regex("nothing is working today!!!!")
        self.assertEqual(out.act, "vent")

    def test_chitchat_short_message(self) -> None:
        out = tag_regex("hey")
        self.assertEqual(out.act, "chitchat")

    def test_chitchat_filler_phrase(self) -> None:
        out = tag_regex("anyway, i guess that's fine")
        self.assertEqual(out.act, "chitchat")

    def test_story_fallback_for_long_neutral_prose(self) -> None:
        out = tag_regex(
            "Today I drove to the lake and saw a heron standing on the dock "
            "for almost an hour. It was very still and the water was glassy."
        )
        self.assertEqual(out.act, "story")
        self.assertEqual(out.source, "fallback")

    def test_empty_text_is_chitchat(self) -> None:
        out = tag_regex("")
        self.assertEqual(out.act, "chitchat")
        self.assertEqual(out.source, "fallback")

    def test_priority_order_vent_beats_question(self) -> None:
        # A venting message that ends in a rhetorical question should
        # land as ``vent``, not ``question`` -- vent is the loudest beat.
        out = tag_regex("i hate this, why does everything break???")
        self.assertEqual(out.act, "vent")


class ParseLlmPayloadTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        out = _parse_llm_payload('{"act":"vent","confidence":0.7}')
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.act, "vent")
        self.assertEqual(out.source, "llm")

    def test_fenced_json(self) -> None:
        raw = "```json\n{\"act\":\"banter\",\"confidence\":0.9}\n```"
        out = _parse_llm_payload(raw)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.act, "banter")

    def test_invalid_act_returns_none(self) -> None:
        self.assertIsNone(_parse_llm_payload('{"act":"nope","confidence":0.6}'))

    def test_clipped_confidence(self) -> None:
        out = _parse_llm_payload('{"act":"story","confidence":2.0}')
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.confidence, 1.0)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(_parse_llm_payload("garbage"))


class TaggerCoordinatorTests(unittest.TestCase):
    def _make(
        self,
        *,
        chat_db: ChatDatabase,
        response: str = '{"act":"vent","confidence":0.85}',
    ) -> tuple[DialogueActTagger, _FakeOllama]:
        ollama = _FakeOllama(response=response)
        tagger = DialogueActTagger(
            ollama=ollama,
            chat_db=chat_db,
            model="m",
            llm_min_user_turns=2,
        )
        return tagger, ollama

    def test_persists_inline_via_chat_db(self) -> None:
        f = _Fixture()
        try:
            mid = f.add_user("i hate this, total burnout")
            tagger, _ollama = self._make(chat_db=f.db)
            result = tagger.tag_user_turn(
                "i hate this, total burnout",
            )
            self.assertEqual(result.act, "vent")
            f.db.update_message_dialogue_act(mid, result.act)
            rows = f.db.get_messages("s1")
            self.assertEqual(rows[-1].dialogue_act, "vent")
        finally:
            f.close()

    def test_should_run_llm_throttles(self) -> None:
        f = _Fixture()
        try:
            tagger, _ollama = self._make(chat_db=f.db)
            low = DialogueActResult(act="story", confidence=0.45, source="fallback")
            # No turns yet -> shouldn't run.
            self.assertFalse(tagger.should_run_llm(regex_result=low))
            tagger.notify_user_turn()
            tagger.notify_user_turn()
            # Now the cadence is met.
            self.assertTrue(tagger.should_run_llm(regex_result=low))
            # High-confidence regex never schedules.
            high = DialogueActResult(act="vent", confidence=0.78, source="regex")
            self.assertFalse(tagger.should_run_llm(regex_result=high))
        finally:
            f.close()

    def test_llm_disagreement_persists_to_db(self) -> None:
        f = _Fixture()
        try:
            mid = f.add_user("today i drove to the lake and saw a heron")
            # Regex would tag this as ``story``; we simulate the LLM
            # disagreeing and returning ``chitchat`` so the DB gets
            # patched.
            tagger, ollama = self._make(
                chat_db=f.db,
                response='{"act":"chitchat","confidence":0.9}',
            )
            for _ in range(2):
                tagger.notify_user_turn()
            regex_result = DialogueActResult(
                act="story", confidence=0.45, source="fallback",
            )
            f.db.update_message_dialogue_act(mid, regex_result.act)
            llm_result = tagger.maybe_run_llm(
                message_id=mid,
                user_text="today i drove to the lake",
                regex_result=regex_result,
                history_provider=lambda: [("user", "earlier line")],
            )
            self.assertIsNotNone(llm_result)
            assert llm_result is not None
            self.assertEqual(llm_result.act, "chitchat")
            self.assertEqual(ollama.calls and len(ollama.calls), 1)
            rows = f.db.get_messages("s1")
            self.assertEqual(rows[-1].dialogue_act, "chitchat")
            stats = tagger.stats()
            self.assertEqual(stats["llm_disagreed"], 1)
            self.assertEqual(stats["llm_persisted"], 1)
        finally:
            f.close()

    def test_llm_agreement_keeps_existing_value(self) -> None:
        f = _Fixture()
        try:
            mid = f.add_user("just a quiet stretch")
            tagger, _ollama = self._make(
                chat_db=f.db,
                response='{"act":"story","confidence":0.9}',
            )
            for _ in range(2):
                tagger.notify_user_turn()
            f.db.update_message_dialogue_act(mid, "story")
            regex_result = DialogueActResult(
                act="story", confidence=0.45, source="fallback",
            )
            llm_result = tagger.maybe_run_llm(
                message_id=mid,
                user_text="just a quiet stretch",
                regex_result=regex_result,
                history_provider=lambda: [],
            )
            self.assertIsNotNone(llm_result)
            stats = tagger.stats()
            self.assertEqual(stats["llm_disagreed"], 0)
            self.assertEqual(stats["llm_persisted"], 0)
        finally:
            f.close()

    def test_llm_failure_does_not_crash(self) -> None:
        f = _Fixture()
        try:
            mid = f.add_user("hmm")
            tagger, ollama = self._make(chat_db=f.db)
            ollama.fail = True
            for _ in range(2):
                tagger.notify_user_turn()
            regex_result = DialogueActResult(
                act="story", confidence=0.45, source="fallback",
            )
            self.assertIsNone(
                tagger.maybe_run_llm(
                    message_id=mid,
                    user_text="hmm",
                    regex_result=regex_result,
                    history_provider=lambda: [],
                )
            )
            self.assertEqual(tagger.stats()["llm_failed"], 1)
        finally:
            f.close()


class ValidActsConstantTests(unittest.TestCase):
    def test_six_unique(self) -> None:
        self.assertEqual(len(VALID_DIALOGUE_ACTS), 6)
        self.assertEqual(len(set(VALID_DIALOGUE_ACTS)), 6)
        for v in ("question", "story", "vent", "banter", "planning", "chitchat"):
            self.assertIn(v, VALID_DIALOGUE_ACTS)


if __name__ == "__main__":
    unittest.main()

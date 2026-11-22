from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.conversation.exchange_recall import exchange_evidence, lookup_exchange
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.ollama_client import OllamaUsage
from app.llm.token_utils import estimate_tokens


def test_correction_recalls_original_morning_exchange_across_sessions(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "exchange.db")
    now = datetime(2026, 11, 17, 19, 51).astimezone()
    monkeypatch.setattr(timephrase, "now", lambda: now)

    def add(session, role, content, hour, minute):
        sent_at = datetime(2026, 11, 12, hour, minute).astimezone()
        return db.execute_commit(
            "INSERT INTO messages (session_id, role, content, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session, role, content, 0, sent_at.astimezone(timezone.utc).isoformat()),
        )

    add("owner:morning", "user", "I have survived! Finally out.", 9, 29)
    add("owner:morning", "assistant", "You caught me mid-nap.", 9, 30)
    add("owner:morning", "user", "Sorry, I had to wake you up.", 9, 32)
    add("owner:morning", "user", "I wanted to say how the dentist went.", 9, 37)
    add("owner:morning", "user", "Managed to survive successfully.", 9, 53)
    add("owner:night", "user", "I worked late after the dentist.", 23, 50)
    add("other:morning", "user", "Private dentist details. I survived it.", 9, 28)
    correction = ("I am free today. Dentist was in Thursday, you forgot I even woke you up "
                  "to tell you how I managed to survive it.")
    db.add_message("owner:current", "user", correction)

    text, ids = exchange_evidence(
        db, correction, "owner:current", "User", budget=450, estimate_tokens=estimate_tokens,
    )
    assert ids
    assert "2026-11-12 09:29" in text and "I have survived!" in text
    assert "2026-11-12 09:32" in text and "wake you up" in text
    assert "Private dentist details" not in text
    assert correction not in text
    assert exchange_evidence(
        db, "I forgot to book the dentist", "owner:current", "User",
        budget=450, estimate_tokens=estimate_tokens,
    ) == ("", [])


def test_plan_recall_keeps_the_whole_original_exchange_and_its_owner(tmp_path):
    db = ChatDatabase(tmp_path / "decision.db")
    with closing(db._get_conn()):
        for session in ("other:setup", "owner:setup"):
            ids = [db.add_message(session, role, line) for role, line in (
                ("user", "The recorder is ready."),
                ("assistant", "Shall we use a brass bell for the opening?"),
                ("user", "I'd like to read the introduction first."),
            )]
        question = "Did I agree to the brass bell for the opening?"
        db.add_message("owner:recall", "user", question)

        def recall(text, budget=450):
            return exchange_evidence(db, text, "owner:recall", "Robin", budget=budget,
                                     estimate_tokens=estimate_tokens)

        text, selected = recall(question)
        assert selected == ids[1:] and "The recorder is ready" not in text
        assert 'Aiko: "Shall we use' in text and 'Robin: "I\'d like to read' in text
        assert not recall(question, budget=45)[1]
        assert recall("What was the plan?") == ("", [])
        assert recall("Did I agree to the brass bell? Can you set a reminder?") == ("", [])


@pytest.mark.parametrize("later_turn", [
    "Actually, I decided against using the brass bell.",
    "We could use the brass bell for another recording.",
])
def test_plan_recall_defers_when_another_exchange_mentions_the_topic(tmp_path, later_turn):
    db = ChatDatabase(tmp_path / "decision.db")
    with closing(db._get_conn()):
        db.add_message("owner:first", "assistant", "I suggest using a brass bell.")
        db.add_message("owner:first", "user", "Sounds good.")
        db.add_message("owner:later", "user", later_turn)
        db.add_message("owner:later", "assistant", "I see.")
        lookup = lookup_exchange(
            db, "What was the plan for the brass bell?", "owner:recall", "Robin",
            budget=450, estimate_tokens=estimate_tokens,
        )
        if later_turn.startswith("Actually"):
            assert lookup.reason == "decision_incomplete" and not lookup.message_ids
        else:
            assert lookup.reason == "decision_ambiguous" and later_turn in lookup.reply


def test_decision_context_marks_the_users_proposal_and_clears_prior_selections(tmp_path):
    db = ChatDatabase(tmp_path / "decision.db")
    with closing(db._get_conn()):
        first = db.add_message("owner:lamp", "user", "We could try the amber lamp.")
        second = db.add_message("owner:lamp", "assistant", "Yes, for the first reading.")
        host = SimpleNamespace(
            _chat_db=db, user_display_name="Robin", _memory_settings=SimpleNamespace(),
            _last_surfaced_items=["old"], _last_context_concept_ids={9},
        )
        region = InnerLifePart1Mixin.build_relevant_context(
            host, user_text="What did we decide about the amber lamp?", recent_turns=[],
            session_key="owner:recall", budget_tokens=1000,
        )
        assert region.concept_trace["original_exchange_message_ids"] == [first, second]
        assert region.concept_trace["surfaced"] == [] and region.reply_guidance
        assert 'Robin: "We could try the amber lamp."' in region.text
        assert 'Aiko: "Yes, for the first reading."' in region.text
        assert region.decision_reply == (
            'You suggested “We could try the amber lamp.” I said “Yes, for the first reading.”'
        )
        assert not host._last_surfaced_items and not host._last_context_concept_ids


@pytest.mark.parametrize("session", ["owner:recall", "owner:museum"])
def test_open_recap_is_the_same_in_chat_speech_and_history_on_repeat(tmp_path, session):
    db = ChatDatabase(tmp_path / "chat.db")
    with closing(db._get_conn()):
        proposal = "We could wind the clock before the tour."
        other_plan = "I'll open the gallery first."
        db.add_message("owner:museum", "assistant", proposal)
        db.add_message("owner:museum", "user", other_plan)
        host = SimpleNamespace(_chat_db=db, _memory_settings=SimpleNamespace(),
                               user_display_name="Robin")
        persona = tmp_path / "persona.txt"
        persona.write_text("You are Aiko.", encoding="utf-8")
        prompt = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
        prompt.set_relevant_context_provider(
            lambda **kwargs: InnerLifePart1Mixin.build_relevant_context(host, **kwargs),
        )
        client = Mock(last_usage=OllamaUsage(prompt_tokens=450, completion_tokens=99))
        runner = TurnRunner(client, db, prompt, model="test", context_window=8192,
                            max_tokens=512, temperature=0,
                            filler_enabled=False)
        expected = f'My suggestion was "{proposal}" You said "{other_plan}"'
        spoken = []
        for _ in range(2):
            visible = []
            spoken.clear()
            result = runner.run(
                session_key=session, user_text="What was our plan for the clock?",
                on_token=visible.append, on_tts_chunk=lambda s, _: spoken.append(s),
            )
            assert result.text == expected
            assert proposal in "".join(visible) and other_plan in " ".join(spoken)
            assert result.usage.done_reason == "quoted_exchange"
            assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (0, 0)
            assert db.get_message_row(result.assistant_message_id).content == expected
        client.chat_stream.assert_not_called()
        spoken.clear()
        cancelled = runner.run(
            session_key=session, user_text="What was our plan for the clock?",
            stop_requested=lambda: True, on_tts_chunk=lambda s, _: spoken.append(s),
        )
        assert cancelled.aborted and not cancelled.text and not spoken
        client.chat_stream.assert_not_called()
        db.add_message("owner:later", "user", "Actually, I'll wind the clock after the tour.")
        later = lookup_exchange(
            db, "What was the plan for the clock?", "owner:fresh", "Robin",
            budget=450, estimate_tokens=estimate_tokens,
        )
        assert later.reason == "decision_incomplete" and not later.message_ids


@pytest.mark.parametrize("question,reply", [
    ("Did I agree to the bell?", "Yes, for the first reading."),
    ("What was the plan for the bell? Also translate it.", "Yes."),
    ("What was the plan for the bell in a table?", "Yes."),
    ("What was the plan for the bell in your own words?", "Yes."),
    ("What was the plan for the bell?", "Maybe. [[remember:testing]]"),
])
def test_direct_mixed_or_tagged_exchanges_use_normal_generation(tmp_path, question, reply):
    db = ChatDatabase(tmp_path / "decision.db")
    with closing(db._get_conn()):
        db.add_message("owner:test", "assistant", "We could try the bell.")
        db.add_message("owner:test", "user", reply)
        result = lookup_exchange(db, question, "owner:fresh", "Robin", budget=450,
                                 estimate_tokens=estimate_tokens)
        assert result.reply == ""


def test_reported_speech_does_not_make_the_reporting_speaker_the_proposer(tmp_path):
    db = ChatDatabase(tmp_path / "decision.db")
    with closing(db._get_conn()):
        db.add_message("owner:test", "user", "You said we could try the bronze bell.")
        db.add_message("owner:test", "assistant", "Yes, that was my idea.")
        result = lookup_exchange(
            db, "What was our plan for the bronze bell?", "owner:fresh", "Robin",
            budget=450, estimate_tokens=estimate_tokens,
        )
        assert result.text and len(result.message_ids) == 2 and not result.reply


def test_missing_then_date_then_named_second_proposal_through_chat(tmp_path):
    db = ChatDatabase(tmp_path / "clarify.db")
    with closing(db._get_conn()):
        question = "Did I agree to use a lantern?"
        old = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        with timephrase.scoped_now(lambda: old):
            db.add_message("owner:a", "assistant", "We could use a brass lantern.")
            db.add_message("owner:a", "user", "Yes, for the first reading only.")
            second = db.add_message("owner:b", "user", "We could borrow the silver lantern.")
            reply = db.add_message("owner:b", "assistant", "I can ask about it.")
        with timephrase.scoped_now(lambda: now):
            for session in ("stranger:lamps", "owner:noremember"):
                db.add_message(session, "assistant", "We could use the secret lantern.")
                db.add_message(session, "user", "I'll bring it.")
            host = SimpleNamespace(_chat_db=db, _memory_settings=SimpleNamespace(),
                                   user_display_name="Robin")
            persona = tmp_path / "persona.txt"
            persona.write_text("You are Aiko.", encoding="utf-8")
            prompt = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
            prompt.set_relevant_context_provider(
                lambda **kwargs: InnerLifePart1Mixin.build_relevant_context(host, **kwargs),
            )
            client = Mock(last_usage=OllamaUsage())
            runner = TurnRunner(client, db, prompt, model="test", context_window=8192,
                                max_tokens=512, temperature=0, filler_enabled=False)

            def ask(line):
                shown, spoken = [], []
                result = runner.run(
                    session_key="owner:recall", user_text=line, on_token=shown.append,
                    on_tts_chunk=lambda chunk, _: spoken.append(chunk),
                )
                assert shown and spoken
                assert result.text == db.get_message_row(result.assistant_message_id).content
                return result

            missing = ask(question)
            assert "can't find the original exchange" in missing.text
            assert missing.usage.done_reason == "decision_clarification"
            options = ask("October 2, 2026.")
            assert "brass lantern" in options.text and "silver lantern" in options.text
            assert "secret" not in options.text and "first reading" not in options.text
            assert options.telemetry.concepts_surfaced["reason"] == "decision_ambiguous"
            result = ask("The silver one.")
            assert result.text == ('You suggested "We could borrow the silver lantern." '
                                   'I said "I can ask about it."')
            assert result.telemetry.concepts_surfaced["original_exchange_message_ids"] == [
                second, reply,
            ]
            ask(question)
            unresolved = ask("I don't remember.")
            assert unresolved.telemetry.concepts_surfaced["reason"] == "decision_unresolved"
            assert "?" not in unresolved.text
            client.chat_stream.assert_not_called()


@pytest.mark.parametrize("extra", ["third", "identical", "markup", "partial"])
def test_ambiguous_lookup_only_offers_choices_it_can_distinguish(tmp_path, extra):
    db = ChatDatabase(tmp_path / "choices.db")
    with closing(db._get_conn()):
        first = "We could use a copper lantern."
        other = {"identical": first, "markup": "We could use a [[touch:lantern]]."}.get(
            extra, "We could use a silver lantern.",
        )
        for session, proposal in (("owner:a", first), ("owner:b", other)):
            db.add_message(session, "assistant", proposal)
            db.add_message(session, "user", "Only for rehearsal.")
        if extra == "third":
            db.add_message("owner:c", "user", "We could buy a glass lantern.")
            db.add_message("owner:c", "assistant", "I can check.")
        if extra == "partial":
            db.add_message("owner:c", "user", "I've since changed my mind about the lantern.")
        result = lookup_exchange(db, "What did we decide about the lantern?", "owner:recall",
                                 "Robin", budget=450, estimate_tokens=estimate_tokens)
        assert result.reason == "decision_ambiguous" and not result.message_ids
        assert "more than one possible match" in result.reply and "[[" not in result.reply


def test_neighboring_proposals_in_one_chat_keep_separate_replies(tmp_path):
    db = ChatDatabase(tmp_path / "nearby.db")
    with closing(db._get_conn()):
        session = "owner:studio"
        first = db.add_message(session, "assistant", "We could use a glass lantern.")
        db.add_message(session, "user", "Sure, just for the reading.")
        second = db.add_message(session, "assistant", "We could borrow the steel lantern.")
        db.add_message(session, "user", "I'd like to think about that.")
        result = lookup_exchange(db, "What was the plan for the lantern?", "owner:recall",
                                 "Robin", budget=450, estimate_tokens=estimate_tokens)
        assert result.reason == "decision_ambiguous" and result.message_ids == [first, second]
        assert "glass lantern" in result.reply and "steel lantern" in result.reply
        assert "just for the reading" not in result.reply and "think about that" not in result.reply


def test_partial_exchange_quotes_only_the_available_turn_and_uses_given_date(tmp_path):
    db = ChatDatabase(tmp_path / "fragment.db")
    with closing(db._get_conn()):
        mid = db.add_message("owner:a", "assistant", "We could bring the glass lantern.")
        result = lookup_exchange(db, "Did I agree to the lantern today?", "owner:recall",
                                 "Robin", budget=450, estimate_tokens=estimate_tokens)
        assert result.reason == "decision_incomplete" and result.message_ids == [mid]
        assert "I said “We could bring the glass lantern.”" in result.reply
        assert "?" not in result.reply
        absent = lookup_exchange(db, "What did we decide about the telescope yesterday?",
                                 "owner:recall", "Robin", budget=450,
                                 estimate_tokens=estimate_tokens)
        assert absent.reason == "decision_missing" and "?" not in absent.reply

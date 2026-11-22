from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.conversation.exchange_recall import exchange_evidence
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
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
        assert recall(question, budget=45) == ("", [])
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
        assert exchange_evidence(
            db, "What was the plan for the brass bell?", "owner:recall", "Robin",
            budget=450, estimate_tokens=estimate_tokens,
        ) == ("", [])


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
        assert not host._last_surfaced_items and not host._last_context_concept_ids

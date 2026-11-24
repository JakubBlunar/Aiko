import pytest

from app.core.conversation.narrative_reasoning import check_timeline, timeline_guidance
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from scripts.narrative_reasoning_replay import FLASHBACK, TIMELINE


@pytest.mark.parametrize(("name", "history", "question", "expected"), TIMELINE)
def test_timeline_routing(name, history, question, expected):
    guidance = timeline_guidance(question, history[-1] if history else "")
    assert bool(guidance) == (name != "pivot")


def test_journey_and_unrelated_time_questions():
    assert "journey" in timeline_guidance("Where was it on my way to rehearsal?", FLASHBACK)
    assert not timeline_guidance("Where was your office before you moved?")
    assert not timeline_guidance("What time is it?", FLASHBACK)


def test_timeline_in_ordinary_followup(tmp_path):
    db = ChatDatabase(tmp_path / "timeline.db")
    persona = tmp_path / "persona.txt"
    persona.write_text("Answer briefly.", encoding="utf-8")
    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
    db.add_message("fiction", "user", FLASHBACK)
    db.add_message("fiction", "assistant", "Go on.")
    q = "Who has the spare key now?"
    mid = db.add_message("fiction", "user", q)
    messages, telemetry = assembler.assemble_with_budget(
        "fiction", q, current_user_message_id=mid, context_window=8192, response_budget=240)
    assert "Put the reported events" in telemetry.system_prompt
    assert messages[-1] == {"role": "user", "content": q}
    assert not telemetry.story_read and telemetry.decision_reply == "It's with Jo."


def test_transfer_needs_a_matching_object_and_confirmed_returner():
    previous = "At dance class I handed Kim the folder."
    assert check_timeline("After dance class Kim gave it back. Who has the folder now?",
                          previous).reply == "You have it back."
    for mismatch in ("After practice Kim gave it back. Who has the folder now?",
                     "After dance class Ana gave it back. Who has the folder now?",
                     "After dance class Kim gave it back. Who has the pass now?",
                     "After dance class Kim almost gave it back. Who has the folder now?"):
        assert not check_timeline(mismatch, previous).reply

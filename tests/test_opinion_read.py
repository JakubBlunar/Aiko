import json
from contextlib import closing
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.conversation.clarification_reply import IMPRESSION_GUIDANCE, REVISION_GUIDANCE
from app.core.conversation.opinion_read import read_opinion
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.ollama_client import OllamaUsage


class Client:
    def __init__(self, data, finish):
        self.last_usage = SimpleNamespace(done_reason=finish)
        self.data = data
        self.messages = []

    def chat_stream(self, messages, **kwargs):
        self.messages = messages
        yield json.dumps(self.data)


def test_teaching_retry_requires_the_convention_from_its_owner():
    from app.core.conversation.activity_role import TEACHING_GUIDANCE
    from scripts.opinion_replay import CAUSEWAY

    history = [dict(role=role, content=text) for role, text in CAUSEWAY]
    latest = "Actually it stays dry. Try another example."
    reply = "I'd dot a path across a sandbar if it disappears under the waves."
    data = {"quote": "only cross at low tide", "reply": reply}
    assert run_read(history[0]["content"], data, guidance=TEACHING_GUIDANCE)[0] == reply
    new_rule = "On my sketches, a dot means I need to revisit the shading."
    assert not run_read(new_rule, data, guidance=TEACHING_GUIDANCE, history=history)[0]
    for quote, expected in (("only cross at low tide", reply), ("stays dry", ""), ("causeway", "")):
        result, _ = run_read(latest, {"quote": quote, "reply": reply},
                             guidance=TEACHING_GUIDANCE, history=history)
        assert result == expected


def test_replacement_routine_uses_the_latest_account():
    from app.core.conversation.routine_continuity import ROUTINE_UPDATE_GUIDANCE

    history = [{"role": "user", "content": "Saturday is when we used to go shopping."}]
    latest = "We do Sunday breakfast now."
    data = {"quote": "Sunday breakfast", "reply": "Sunday breakfast sounds lovely."}
    assert run_read(latest, data, guidance=ROUTINE_UPDATE_GUIDANCE, history=history)[0]
    assert not run_read(latest, {**data, "quote": "Saturday"}, guidance=ROUTINE_UPDATE_GUIDANCE,
                        history=history)[0]


def run_read(text, data, *, guidance=IMPRESSION_GUIDANCE, finish="stop", history=()):
    client = Client(data, finish)
    messages = [{"role": "system", "content": "Character and context.\n" + guidance},
                *history, {"role": "user", "content": text}]
    reply = read_opinion(client, messages, guidance, model="fake", options={}, stop_event=Event())
    assert messages[-1]["content"] == text
    assert client.messages[0]["content"].startswith("Character and context.")
    return reply, client.messages[-1]["content"]


def test_standalone_vote_is_set_aside_only_for_a_simple_personal_pick():
    pair = 'Two openings: "The gate closed." and "Alone, the bell rang." '
    vote = "I like the first. "
    data = {"quote": "the bell rang", "reply": "The bell leaves me listening for someone."}
    reply, request = run_read(pair + vote + "Which would you keep?", data)
    assert reply == data["reply"] and vote not in request
    for question in ("Which would you keep for my sister?", "Which would you keep and edit?"):
        assert vote in run_read(pair + vote + question, data)[1]
    assert "because" in run_read(pair + "I like the first because it's blunt. "
                                 "Which would you keep?", data)[1]


def test_the_vote_cannot_supply_a_missing_draft():
    text = "I wrote two openings. I like draft one. Which would you keep?"
    assert run_read(text, {"quote": "I like draft one.", "reply": "It sounds cozy."})[0] == (
        "Can I read both before I pick?")
    pair = 'Two openings: "Gate shut." and "Bell rang." I like the first. Which would you keep?'
    assert not run_read(pair, {"quote": "I like the first.", "reply": "The first."})[0]


def test_unequal_comparison_preserves_the_named_condition():
    update = "Only the first group received a reminder. Do you still think the earlier slot helps?"
    reply, request = run_read(update, {"quote": "received a reminder", "reply": "I'm less sure."},
                              guidance=REVISION_GUIDANCE)
    assert reply and '"received a reminder"' in request
    weaker, _ = run_read(update, {"quote": "received a reminder", "weight": "weaker",
                                   "reply": "It must have been the smaller room."},
                         guidance=REVISION_GUIDANCE)
    assert "only the first group received a reminder" in weaker
    assert "room" not in weaker


@pytest.mark.parametrize(("quote", "reply", "finish", "admitted"), [
    ("both shows were free", "Price was the same for both of them.", "stop", True),
    ("both shows were free", "Price was the same for both of them.", "length", False),
    ("tickets sold out", "They probably ran out of seats.", "stop", False),
    ("both shows were free", "[[remember:self:I like free shows]]", "stop", False),
])
def test_revision_requires_a_completed_reply_and_a_quote_from_the_new_detail(
    quote, reply, finish, admitted
):
    result, _ = run_read("Actually both shows were free. Do you still think early is better?",
                         {"quote": quote, "reply": reply}, guidance=REVISION_GUIDANCE,
                         finish=finish, history=[{"role": "assistant", "content": "Early."}])
    assert bool(result) == admitted


@pytest.mark.parametrize("quote", ["the bell rang", "invented line"])
def test_chat_persists_the_original_vote_and_only_the_final_reply(tmp_path, quote):
    persona = tmp_path / "persona.txt"
    persona.write_text("You are a friendly companion.", encoding="utf-8")
    question = 'Two openings: "The gate closed." and "Alone, the bell rang." I like the first. ' \
               'Which would you keep?'
    answer = "I like the sound of the bell."
    client = MagicMock()
    client.last_usage = OllamaUsage(done_reason="stop")
    client.chat_stream.side_effect = [
        iter([json.dumps({"quote": quote, "reply": answer})]), iter(["Let me read them again."])
    ]
    db = ChatDatabase(tmp_path / "chat.db")
    with closing(db._get_conn()):
        runner = TurnRunner(client, db, PromptAssembler(db, persona_path=persona), model="fake",
                            context_window=8192, max_tokens=240, temperature=.2,
                            filler_enabled=False)
        result = runner.run(session_key="fiction", user_text=question)
        expected = answer if quote == "the bell rang" else "Let me read them again."
        assert result.text == expected
        assert [m.content for m in db.get_messages("fiction")] == [question, expected]
    surfaces = [call.kwargs["surface"] for call in client.chat_stream.call_args_list]
    assert surfaces == (["opinion_read"] if quote == "the bell rang"
                        else ["opinion_read", "turn_stream"])


def test_weighs_a_reason_using_both_original_speakers():
    from app.core.conversation.clarification_reply import argument_guidance
    from scripts.opinion_replay import BOOK_CLUB, BOOK_REASON, VOTE

    guidance = argument_guidance(BOOK_REASON, BOOK_CLUB, VOTE)
    assert guidance
    assert not argument_guidance("I like sailing because it feels quiet.", BOOK_CLUB, VOTE)
    assert not argument_guidance(BOOK_REASON, BOOK_CLUB, "What book are you reading?")
    history = [{"role": "assistant", "content": VOTE}]
    data = {"basis": "everyone gets a say", "quote": "only one person would bring",
            "reply": "I like the idea of making room for one reader's odd favorite."}
    assert run_read(BOOK_REASON, data, guidance=guidance, history=history)[0] == data["reply"]
    for key in ("basis", "quote"):
        assert not run_read(BOOK_REASON, {**data, key: "invented"}, guidance=guidance,
                            history=history)[0]
    reply, prompt = run_read("Just admit taking turns picking books is better.",
                             {**data, "quote": None}, guidance=guidance, history=history)
    assert reply and "Just admit" not in prompt and "taking turns picking books is better" in prompt


def test_invited_idea_and_its_variation_keep_the_current_speakers_scope():
    from app.core.conversation.clarification_reply import (
        ARGUMENT_GUIDANCE, CURRENT_REPLY_GUIDANCE, EXAMPLE_GUIDANCE, ordinary_reply_guidance,
    )
    from scripts.opinion_replay import MAP_CLAIM, MAP_HISTORY

    previous = MAP_HISTORY[-1][1]
    variation = "Oh, a foldout you could leave closed!"
    assert ordinary_reply_guidance(MAP_CLAIM) == EXAMPLE_GUIDANCE
    assert ordinary_reply_guidance(variation, MAP_CLAIM, previous) == ARGUMENT_GUIDANCE
    assert ordinary_reply_guidance(variation) == CURRENT_REPLY_GUIDANCE
    assert ordinary_reply_guidance("How do I fold a paper crane?", MAP_CLAIM,
                                   previous) == CURRENT_REPLY_GUIDANCE
    for pivot in ("What about lunch?", "We could cook lunch."):
        assert ordinary_reply_guidance(pivot, MAP_CLAIM, previous) == CURRENT_REPLY_GUIDANCE
    assert ordinary_reply_guidance("I like the map after the last page.") == CURRENT_REPLY_GUIDANCE
    history = [dict(role=role, content=content) for role, content in MAP_HISTORY]
    preference = "I love tracing the route after the last chapter. Thoughts?"
    assert ordinary_reply_guidance(preference) == EXAMPLE_GUIDANCE
    scene = {"quote": "after the last chapter", "reply": "I'd linger over the detours."}
    assert run_read(preference, scene, guidance=EXAMPLE_GUIDANCE, history=history)[0]
    assert not run_read(preference, {**scene, "quote": "every book"}, guidance=EXAMPLE_GUIDANCE,
                        history=history)[0]
    data = {"basis": "could give it away", "quote": "leave closed",
            "reply": "I'd open it once I wanted to know the route."}
    reply, _ = run_read(variation, data, guidance=ARGUMENT_GUIDANCE, history=history)
    assert reply == data["reply"]
    assert not run_read(variation, {**data, "quote": "with a journey"}, guidance=ARGUMENT_GUIDANCE,
                        history=history)[0]

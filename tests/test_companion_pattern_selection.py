from types import SimpleNamespace as NS

import pytest

from app.core.concepts.preference_selection import PreferenceSelection
from app.core.memory.pattern_extraction import (
    compact_pattern, naming_only_plan, one_off_naming_refusal,
)
from app.core.memory.shared_shorthand import admit_shorthand
from app.core.proactive.curiosity_answer import short_answer, specific_ask, volunteered_answer


def test_preference_is_scoped_and_deduplicated_across_lanes():
    note = "During block printing, show the ink order in a numbered list."
    memory = NS(id=7, kind="preference", source_message_id=12, source_session="u:art",
                content=note, metadata={})
    concepts = [NS(concept_id=i, subject="user", kind="boundary", label=label, rationale="")
                for i, label in enumerate(("Feels safe with a numbered list",
                                           "Needs a numbered list because of poor memory"), 1)]
    edge = NS(src_type="memory", src_id="7", polarity=1)
    host = NS(_user_id="u", _memory_store=NS(get_many=lambda ids: {7: memory}),
              _concept_store=NS(evidence_for=lambda ids: {i: [edge] for i in ids},
                                applicability={}))

    def select(text):
        return PreferenceSelection(host._concept_store, host._memory_store, None, "u", text, None)

    selector = select("During block printing, which ink goes first?")
    chosen = selector.select([(c, 1) for c in concepts])
    assert [row.label for row, _ in chosen] == [note]
    assert selector.select([(concepts[1], 1)], excluded={1}) == []
    assert select("I am tuning a piano.").select([(concepts[0], 1)]) == []
    assert select("What did I ask about a numbered list?").select([(concepts[0], 1)])
    memory.metadata = {"superseded_reason": "withdrawn"}
    assert not select("During block printing").select([(concepts[0], 1)])
    assert concepts[0].label == "Feels safe with a numbered list"


@pytest.mark.parametrize(("text", "answered"), [
    ("The cello carrier is made of carbon fiber.", True),
    ("The cello carrier arrived and my coat has a velvet lining.", False),
    ("The cello carrier has a dirty lining.", False),
    ("I hope the cello carrier is made of carbon fiber.", False),
])
def test_property_answer_requires_a_statement_about_the_referent(text, answered):
    assert volunteered_answer("material of the cello carrier", "What is the cello carrier made of?",
                              text, NS(echoed=True, kind="lexical")) is answered


def test_time_question_requires_a_time_and_specific_ask():
    question = "When is the museum pickup?"
    verdict = NS(echoed=True, kind="lexical")
    assert not volunteered_answer("time of museum pickup", question,
                                  "The museum pickup is at the side entrance.", verdict)
    assert volunteered_answer("time of museum pickup", question,
                              "The museum pickup is at noon.", verdict)
    assert not specific_ask(question, "Did you arrange the museum pickup?")
    assert specific_ask(question, question)
    assert short_answer(question, "Noon.")
    assert not short_answer(question, "Perhaps.")


def test_different_property_of_the_same_object_cannot_close_the_question():
    verdict = NS(echoed=True, kind="lexical")
    question = "What color is the scarf?"
    for text in ("My scarf is torn.", "The scarf is on the blue chair."):
        assert not volunteered_answer("scarf", question, text, verdict)
    assert volunteered_answer("scarf", question, "My scarf is teal.", verdict)
    assert not specific_ask(question, "Where did you put the scarf?")
    assert not specific_ask(
        "What is the cello carrier made of?", "What is the flute carrier made of?",
        subject="material of cello carrier",
    )


def test_compact_adoption_requires_a_definition_and_fresh_user_agreement():
    rows = [NS(id=1, role="assistant", session_id="s",
               content="Let's call the quiet inventory an amber sweep."),
            NS(id=2, role="user", session_id="s", content="Yes, let's use that name.")]
    item = {"kind": "shared_term", "term": "amber sweep", "meaning": "quiet inventory",
            "definition_id": 1, "adoption_id": 2}
    compact_pattern(item, {r.id: r for r in rows})
    assert admit_shorthand(item, rows, owner="u", session_key="s")
    rows[1].content = "Yes, but I'd rather leave it unnamed."
    item.pop("shorthand")
    compact_pattern(item, {r.id: r for r in rows})
    assert not admit_shorthand(item, rows, owner="u", session_key="s")


def test_naming_alone_is_not_a_booking_but_real_plans_survive():
    row = NS(id=1, content="Let's call the rehearsal a lantern round.")
    plan = {"content": "We scheduled the lantern round.", "temporal_type": "future_plan",
            "evidence": [{"message_id": 1}]}
    assert naming_only_plan(plan, {1: row}, ["lantern round"])
    row.content += " I also booked the hall."
    assert not naming_only_plan(plan, {1: row}, ["lantern round"])
    row.content = "Let's call the dentist."
    assert not naming_only_plan(plan, {1: row})


def test_specific_refusal_does_not_make_a_durable_preference():
    item = {"kind": "preference", "evidence": [{"message_id": 1}]}
    row = NS(role="user", content="I'd rather leave it unnamed.")
    assert one_off_naming_refusal(item, {1: row})
    row.content = "I prefer to leave all my projects unnamed."
    assert not one_off_naming_refusal(item, {1: row})

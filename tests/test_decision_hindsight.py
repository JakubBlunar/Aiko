from contextlib import closing

from app.core.conversation.decision_hindsight import check_hindsight
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.hindsight_cases import AFTER, MOVE, RAIN, SHELTER, SUN, THEN
from scripts.longitudinal_reasoning_replay import NOW, seed_rows


def test_timing_roles_scope_and_question_lens(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [RAIN, SHELTER, MOVE, SUN,
                  (4, "user", "The picnic forecast was revised the next day."),
                  (5, "user", "The concert turned out badly.")])
        seed_rows(db, "else", [(1, "user", "The picnic forecast mentioned snow.")])
        def answer(text):
            return check_hindsight(text, "", db, "sam", "sam:now", None)

        result = answer(THEN).guidance
        assert RAIN[2] in result.split("Choice:")[0] and SHELTER[2] in result.split("Choice:")[0]
        assert SUN[2] not in result
        assert SUN[2] in answer(AFTER).guidance.split("Reported outcome:")[1]
        assert "revised" in result.split("Other remarks")[1]
        assert "concert" not in result and "snow" not in result
        assert "eventual result known" in answer(AFTER).guidance
        assert not answer("Write about the picnic.").guidance
        assert not answer("In hindsight, can you write a song about the picnic?").guidance
        pronoun = check_hindsight("Would you make that call again with the same information?",
                                  "We moved the picnic indoors.", db, "sam", "sam:now", None)
        assert RAIN[2] in pronoun.guidance

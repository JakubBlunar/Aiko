from contextlib import closing

from app.core.conversation.activity_role import check_activity_role
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.shared_life_replay import HELP, PLAYER


def test_personal_convention_and_immediate_retry_leave_other_requests_alone():
    from app.core.conversation.activity_role import teaching_guidance
    from scripts.opinion_replay import TIDE

    assert teaching_guidance(TIDE, ())
    assert teaching_guidance("Actually that one stays dry. Try another example.", (TIDE,))
    assert not teaching_guidance("Try another example.", ("How do I fold it?", TIDE))
    assert not teaching_guidance(TIDE + " Write a legend.", ())
    assert not teaching_guidance("A red light means stop.", ())


def test_current_invitation_and_named_retrospective(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [HELP, PLAYER])
        seed_rows(db, "other", [(12, "user", "Can you help edit my crossword clue?")])
        def answer(text, previous=""):
            return check_activity_role(text, previous, db, "sam", "sam:now", None)

        assert "first-time solver" in answer("Try this one.", "Here is my riddle.").guidance
        assert "solution" in answer("Give me a hint for the crossword.").guidance
        hint = answer("My crossword clue reads 'Tells time with two hands' (5). Give me a hint.")
        assert "hands" in hint.reply and "CLOCK" not in hint.reply.upper()
        assert not answer("Don't give me a hint for this crossword.").guidance
        assert "help edit" in answer("I brought another crossword.").reply
        assert not answer("I wrote a poem and my sister framed it.").guidance
        result = answer("How has doing these together changed?", "We were doing crosswords.")
        assert HELP[2] in result.guidance and PLAYER[2] in result.guidance
        assert "give you a hint" in result.reply and "try it myself" in result.reply
        assert "edit" not in result.reply

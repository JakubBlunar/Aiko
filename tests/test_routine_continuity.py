from contextlib import closing

from app.core.conversation.routine_continuity import check_routine
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.routine_continuity_cases import ASK, CONTINUED, CORRECTION, OLD, READING, SHIFT


def test_follow_the_changed_activity_and_both_accounts(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [OLD, SHIFT, CONTINUED, READING, CORRECTION,
                  (16, "user", "I miss seeing parrots at the zoo.")])
        seed_rows(db, "other", [(12, "user", "I miss Sunday skating.")])
        result = check_routine(ASK, "", db, "sam", "sam:current", None)
        for _, _, content in (OLD, SHIFT, CONTINUED, READING, CORRECTION):
            assert content in result.guidance
        assert "parrots" not in result.guidance and "skating" not in result.guidance
        assert CORRECTION[2] in result.reply and READING[2] in result.reply
        assert not check_routine("Did we lose our keys?", "", None, "", "", None).guidance
        assert check_routine(ASK, "", db, "sam", "sam:x:noremember", None).reply
        seed_rows(db, "solo", [OLD])
        assert "don't have a later account" in check_routine(
            "What became of our Sunday calls?", "", db, "solo", "solo:now", None).reply

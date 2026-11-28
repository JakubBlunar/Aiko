from contextlib import closing

from app.core.conversation.routine_continuity import check_routine, check_shortcut
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.routine_continuity_cases import (
    ASK, CONTINUED, CORRECTION, LOGISTICS, OLD, READING, SHIFT,
)


def test_shortcut_uses_the_speakers_link_and_lets_a_new_routine_replace_it(tmp_path):
    from scripts.companion_contribution_replay import BREAKFAST, DELIVERY, ERRAND, WALK_ASK, WEEKEND

    db = ChatDatabase(tmp_path / "shortcut.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def offer(text=DELIVERY, previous="", key="sam:here"):
            return check_shortcut(text, previous, db, "sam", key, None)

        seed_rows(db, "other", [(2, "user", ERRAND)])
        seed_rows(db, "sam", [(2, "assistant", ERRAND), (3, "user", WEEKEND),
                              (4, "user", ERRAND.replace("Saturdays", "Sundays")),
                              (5, "user", "I get groceries on Saturdays. " + WEEKEND),
                              (5, "user", "I get groceries on Saturdays, and " + WEEKEND),
                              (5, "user", ERRAND.replace("the shop", "the guitar shop"))])
        assert not offer().guidance
        seed_rows(db, "sam", [(6, "user", ERRAND)])
        assert ERRAND in offer().guidance and not offer().reply
        assert not offer(key="sam:hidden:noremember").guidance
        assert not offer("I could get my tickets delivered. That would save an hour.").guidance
        assert not offer("How can I get my Saturday groceries delivered faster?").guidance
        assert not offer("We could have breakfast instead.", DELIVERY).guidance
        assert "let the old concern go" in offer(BREAKFAST, DELIVERY).guidance
        assert not offer(DELIVERY + " " + BREAKFAST).guidance
        seed_rows(db, "sam", [(7, "assistant", WALK_ASK), (7, "user", BREAKFAST)])
        assert not offer().guidance


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
        seed_rows(db, "logistics", [OLD, SHIFT, LOGISTICS])
        assert LOGISTICS[2] in check_routine(ASK, "", db, "logistics", "logistics:now", None).reply

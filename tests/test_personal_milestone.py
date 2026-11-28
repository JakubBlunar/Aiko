from contextlib import closing

from app.core.conversation.personal_milestone import check_milestone, check_preparation
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.personal_perspective_replay import GOAL, UPDATE


def test_preparation_is_optional_attributed_scoped_and_current(tmp_path):
    from scripts.companion_contribution_replay import OTHER, PLAN, SIGN_WORK, SUPPER

    db = ChatDatabase(tmp_path / "prep.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def read(text=SUPPER, previous="", key="sam:here"):
            return check_preparation(text, previous, db, "sam", key, None)

        seed_rows(db, "someone", [(2, "user", SIGN_WORK)])
        seed_rows(db, "sam", [(3, "assistant", SIGN_WORK), (4, "user", PLAN)])
        assert not read().guidance
        seed_rows(db, "sam", [(5, "user", OTHER)])
        assert not read().guidance
        seed_rows(db, "sam", [(6, "user", SIGN_WORK)])
        action = SIGN_WORK.split(" so people")[0]
        assert action in read().guidance and "so people" not in read().guidance and not read().reply
        assert NOW.replace(day=6).date().isoformat() not in read().guidance
        generic = "Everyone found the room. I barely did anything tonight."
        assert not read(generic).guidance
        assert action in read(generic, "Back from the community supper!").guidance
        assert not read("Everyone found the hotel. I barely did anything.").guidance
        assert not read(SUPPER.replace("supper", "concert")).guidance
        assert not read(key="sam:private:noremember").guidance
        seed_rows(db, "sam", [(7, "user", "Actually my neighbour did the community supper sign.")])
        assert not read().guidance
        assert not read("How do I pack the signs away?").guidance
        assert "replace any earlier guess" in read("Turns out everyone used the kitchen.",
                                                   SUPPER).guidance


def test_requires_the_users_goal_actor_subject_and_qualifier(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def guidance(text=UPDATE, key="sam:now"):
            return check_milestone(text, db, "sam", key, None).guidance

        seed_rows(db, "other", [(2, "user", GOAL)])
        seed_rows(db, "sam", [(3, "assistant", GOAL)])
        assert not guidance()
        seed_rows(db, "sam", [(5, "user", GOAL)])
        assert GOAL in guidance()
        answer = check_milestone(UPDATE, db, "sam", "sam:now", None).reply
        assert "doors open at eight" in answer and "Italian" in answer
        assert not guidance(key="sam:private:noremember")
        assert not guidance(UPDATE.replace("Italian", "French"))
        assert not guidance(UPDATE.replace("I rang", "my friend rang"))
        assert not guidance(UPDATE.replace("No time to write a script", "I read a script"))
        seed_rows(db, "sam", [(9, "user", "Unscripted calls in Italian are already routine.")])
        assert not guidance()

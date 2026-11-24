from contextlib import closing

from app.core.conversation.personal_milestone import check_milestone
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.personal_perspective_replay import GOAL, UPDATE


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

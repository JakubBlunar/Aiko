from contextlib import closing

from app.core.conversation.shared_creation import check_shared_creation
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.shared_creation_cases import ASK, DOORS, INSIDE, MEET, REVISED, TRACES


def test_named_project_speakers_revisions_and_hard_conflict(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [DOORS, MEET, (10, "assistant", "I love the real station cafe."),
                  (12, "assistant", "In our library story I want a late supper.")])
        seed_rows(db, "other", [(15, "user", "I want a fire in our station story.")])
        def answer(text=ASK):
            return check_shared_creation(text, "", db, "sam", "sam:now", None)

        base = answer()
        assert DOORS[2] in base.guidance and MEET[2] in base.guidance and not base.reply
        assert not any(word in base.guidance for word in ("cafe", "supper", "fire"))
        seed_rows(db, "sam", [(13, "assistant", INSIDE[2])])
        assert "Which" in answer().reply
        revision = answer("I've changed my mind. The doors can open. " + ASK)
        assert not revision.reply and "Stage the scene outside" not in revision.guidance
        seed_rows(db, "sam", [(16, "assistant", TRACES[2])])
        assert answer().reply
        seed_rows(db, "sam", [(17, "assistant", REVISED[2])])
        assert not answer().reply and INSIDE[2] not in answer().guidance
        assert not answer("What would you add to our station timetable?").guidance
        assert not check_shared_creation(ASK, "", db, "sam", "sam:x:noremember", None).guidance

from contextlib import closing

from app.core.conversation.eye_for_detail import check_eye_for_detail
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.eye_for_detail_cases import ASK, THEATRE, TRACES
from scripts.longitudinal_reasoning_replay import NOW, seed_rows


def test_requires_distinct_own_observations_and_a_present_detail(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def guidance(scene=THEATRE, key="sam:chat"):
            return check_eye_for_detail(ASK, scene, db, "sam", key, None).guidance

        seed_rows(db, "other", TRACES)
        seed_rows(db, "sam", [(1, "user", TRACES[0][2]), TRACES[1],
                  (13, "assistant", TRACES[1][2])])
        assert TRACES[1][2] not in guidance()
        seed_rows(db, "sam", TRACES[:1])
        assert all(row[2] in guidance() for row in TRACES)
        shot = check_eye_for_detail(THEATRE + ASK, "", db, "sam", "sam:now", None)
        assert "stair worn into a hollow" in shot.reply
        assert TRACES[0][2] not in guidance("The room has a white screen and a green lamp.")
        assert TRACES[0][2] not in guidance("The room has no scuffed rail, just a white screen.")
        assert TRACES[0][2] not in guidance(key="sam:x:noremember")
        assert not check_eye_for_detail("Which detail would you fix in the code?", "",
                                        None, "", "", None).guidance

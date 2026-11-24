from contextlib import closing

from app.core.conversation.project_purpose import check_project_purpose
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.project_purpose_cases import LEARN, QUESTION, SHARE, START, STATE


def test_prior_exchange_resolves_project_and_separates_beneficiary_evidence(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def answer(key="sam:now"):
            return check_project_purpose(QUESTION, STATE, db, "sam", key, None)

        seed_rows(db, "sam", [(1, "assistant", START + SHARE + ".")])
        seed_rows(db, "else", [(1, "user", START + SHARE + ".")])
        assert "hoping" in answer().reply
        seed_rows(db, "sam", [(4, "user", START + LEARN + " and so that " + SHARE + "."),
                  (10, "user", "Our cousins used the recipe book to make dumplings."),
                  (16, "user", "The street atlas is still unfinished.")])
        found = answer().guidance
        assert LEARN in found and SHARE in found and "cousins used" in found
        result = answer().reply
        assert "do the fold yourself" in result and "already been useful" in result
        assert "made dumplings with Gran twice" in result
        assert "street atlas" not in found
        wrong = check_project_purpose("Did we give up on the stamp book?", STATE, db, "sam",
                                      "sam:now", None)
        assert LEARN not in wrong.guidance
        assert LEARN not in answer(key="sam:private:noremember").guidance
        assert not check_project_purpose("Was it worth it?", "", None, "", "", None).guidance
        seed_rows(db, "sam", [(14, "user", "We started the poetry book to share our verses.")])
        ambiguous = check_project_purpose("Did we give up on our book?", "", db, "sam",
                                          "sam:now", None)
        assert "Which project" in ambiguous.reply


def test_users_ability_leaves_beneficiary_learning_open(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [(1, "user", START + "our cousins could learn the fold.")])
        result = check_project_purpose(STATE + QUESTION, "", db, "sam", "sam:now", None)
        assert "your cousins learn the fold" in result.reply
        assert "still open" in result.reply

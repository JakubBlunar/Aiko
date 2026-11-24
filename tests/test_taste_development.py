from contextlib import closing

from app.core.conversation.taste_development import check_taste
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.perspective_replay import EXCEPTION, LETTER, NEW_ENDINGS, TIDY


def test_refinement_belongs_to_speaker_with_original_scope(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [TIDY, (12, "user", LETTER), (15, "assistant", EXCEPTION),
                  (17, "assistant", "I prefer oak for the kitchen.")])
        seed_rows(db, "other", [(16, "assistant", "I like unhappy endings.")])
        db.add_message("sam:else:noremember", "assistant", "I prefer bleak endings.")
        mid = db.add_message("sam:now", "user", NEW_ENDINGS)
        result = check_taste(NEW_ENDINGS, "", db, "sam", "sam:now", mid)
        assert TIDY[2] in result.guidance and EXCEPTION in result.guidance
        assert LETTER not in result.guidance and "the kitchen" not in result.guidance
        assert "unhappy" not in result.guidance and "bleak" not in result.guidance
        assert not check_taste(NEW_ENDINGS, "", db, "sam", "sam:x:noremember", mid).guidance
        assert not check_taste("Translate this ending", "", None, "", "", None).guidance


def test_new_choice_follows_the_qualified_reason_across_swapped_labels(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [TIDY, (12, "assistant", LETTER)])
        def answer(q):
            return check_taste(q, "", db, "sam", "sam:now", None).reply

        assert "pick B" in answer(NEW_ENDINGS)
        assert "pick A" in answer(NEW_ENDINGS.replace("In A", "In B").replace("B stops", "A stops"))
        assert not answer(NEW_ENDINGS.replace("both reveal", "don't reveal"))

        seed_rows(db, "sam", [(13, "assistant", "I like wool for winter hats because it is warm.")])
        question = "Which fabric would you pick for a winter hat, cotton or wool?"
        assert "choose wool" in answer(question)
        assert not answer(question.replace("winter", "summer"))

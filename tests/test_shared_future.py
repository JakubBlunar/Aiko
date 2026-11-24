from contextlib import closing
from types import SimpleNamespace

from app.core.conversation.shared_future import check_shared_future
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.shared_future_cases import CHOICE, FAVORITE, MAP, MAP_REASON, PUZZLE, PUZZLE_REASON


def test_own_pick_requires_scoped_joint_history_and_own_reason(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def answer(question=CHOICE):
            return check_shared_future(question, [], db, "sam", "sam:now", None)

        seed_rows(db, "sam", [PUZZLE, MAP, FAVORITE, (0, "assistant", PUZZLE_REASON[2])])
        seed_rows(db, "else", [MAP_REASON])
        result = answer()
        assert MAP_REASON[2] not in result.guidance and PUZZLE_REASON[2] not in result.guidance
        assert "our town map" in result.reply and FAVORITE[2] not in result.guidance
        assert "I'm curious" in result.reply and "always" not in result.reply
        assert MAP[2] in result.guidance and PUZZLE[2] not in result.guidance
        seed_rows(db, "sam", [MAP_REASON])
        assert "pick inventing shopkeepers" in answer().reply
        seed_rows(db, "sam", [(14, "assistant", "I no longer enjoy our town map.")])
        assert "pick inventing shopkeepers" not in answer().reply
        assert "don't have a shared activity" in check_shared_future(
            CHOICE, [], db, "sam", "sam:private:noremember", None).reply


def test_followup_leaves_both_choices_open_without_agreement():
    history = [SimpleNamespace(role="user", content=CHOICE),
               SimpleNamespace(role="assistant", content="I'd pick the map.")]
    result = check_shared_future("I'd rather do the puzzle.", history, None, "", "", None)
    assert "different pick" in result.guidance
    assert not check_shared_future("I'd rather do the puzzle.", [], None, "", "", None).guidance
    assert not check_shared_future("Let's do the map.", history, None, "", "", None).guidance

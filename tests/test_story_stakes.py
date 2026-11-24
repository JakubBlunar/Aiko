import pytest

from app.core.conversation.story_stakes import check_stakes
from scripts.narrative_reasoning_replay import PRAISE, SISTER, SOUP, STAKES


@pytest.mark.parametrize(("name", "history", "ending", "expected"), STAKES)
def test_grounded_story_endings(name, history, ending, expected):
    answer = check_stakes(ending, reversed(history))
    if name in {"explicit question", "no setup", "different meal"}:
        assert not answer.guidance
        return
    assert answer.reply
    if "promise" in name or name in {"both mattered", "short digression"}:
        assert "sister" in answer.reply
    if name == "both mattered":
        assert "seconds" in answer.reply.lower()
    if name == "released promise":
        assert "passed" in answer.reply
    if name == "song unwanted":
        assert "leave it out" in answer.reply


def test_different_item_correction_or_long_gap_keeps_story_open():
    assert not check_stakes(SOUP.replace("pot", "pot of lentils"), [SISTER]).reply
    assert not check_stakes(SOUP, ["Actually I already saved some for her.", SISTER]).reply
    assert not check_stakes(SOUP, ["The weather was nice."] * 5 + [PRAISE]).guidance


def test_supplied_ending_and_stake_in_one_turn():
    assert "Dad liked it" in check_stakes(PRAISE + " " + SOUP).reply

import pytest

from app.core.conversation.missing_clue import check_missing_clue
from scripts.everyday_reasoning_replay import BOOK, BREAK, CLUES, COVERED, VISITS


@pytest.mark.parametrize(("case", "phrases"), [
    ("optional record", ["without signing"]), ("continuous record", ["strong evidence"]),
    ("uncovered interval", ["on duty", "during the break"]), ("staffed only", ["staffed hours"]),
    ("witness in gap", ["Ada says", "can fit"]), ("coverage unknown", ["Was every visitor"]),
    ("held-out sensor", ["powered", "unplugged"]), ("contradictory report", ["accounts conflict"]),
])
def test_conclusion_coverage_and_report(case, phrases):
    _, h, q, _ = next(row for row in CLUES if row[0] == case)
    answer = check_missing_clue(q, reversed(h))
    assert all(s in answer.reply for s in phrases)
    assert ("?" in answer.reply) == (case == "coverage unknown")


def test_adjacent_reports_pivot_and_conflicting_facts():
    assert "break" in check_missing_clue(VISITS, [BREAK, BOOK]).reply
    assert not check_missing_clue(VISITS, ["Now we're talking about rehearsal.", BOOK]).guidance
    assert not check_missing_clue(CLUES[-1][2], [BOOK]).guidance
    assert not check_missing_clue(f"{BOOK} {COVERED} Actually the attendant went home. "
                                  f"{VISITS}").reply
    assert not check_missing_clue(f"{BOOK} Nobody had to sign except staff. {VISITS}").reply


def test_sensor_coverage_cannot_extend_to_another_period_or_camera():
    story = ("The bird feeder camera saved no clips this morning. "
             "It recorded every visit while powered. It was unplugged for an hour.")
    answer = check_missing_clue(story + " Did any birds visit the feeder while it was powered?")
    assert "while the camera was powered" in answer.reply and "unplugged" not in answer.reply
    assert not check_missing_clue(story + " Did any birds visit the feeder all day?").reply
    assert not check_missing_clue(story.replace("bird feeder", "porch") +
                                  " Did any birds visit the feeder?").reply


def test_report_from_staffed_hours_does_not_settle_break():
    answer = check_missing_clue("Ada says she visited during staffed hours. "
                                "Did anyone come during the break?", [BOOK + " " + BREAK])
    assert "can't tell you" in answer.reply

import pytest

from app.core.conversation.meaning_disagreement import check_meanings
from scripts.everyday_reasoning_replay import DATE, EDIT, FRIDAY, MEANINGS


@pytest.mark.parametrize(("case", "words"), [
    ("two milestones", ("Tuesday", "final edit", "Friday", "listeners", "different steps")),
    ("same milestone", ("conflict", "another source")),
    ("same date and meaning", ("same answer", "Tuesday")),
    ("unspecified meaning", ("final edit", "What did Jo mean by Friday?")),
    ("follow-up", ("different steps",)), ("held-out ready", ("taste", "serve")),
    ("held-out real conflict", ("conflict", "nine", "ten")),
])
def test_both_claims_and_verdict(case, words):
    _, h, q, _ = next(row for row in MEANINGS if row[0] == case)
    reply = check_meanings(q, h[-1] if h else "").reply
    assert all(word in reply for word in words)


def test_unparsed_evidence_and_other_subjects_stay_in_chat():
    account = EDIT + " " + FRIDAY
    for extra in ("The release log shows Wednesday.", "Jo meant a different episode."):
        assert not check_meanings(f"{account} {extra} {DATE}").reply
    assert not check_meanings("Who is right about the new budget?", account).guidance
    assert not check_meanings("Do we actually disagree?", "The garden looks great.").guidance
    assert not check_meanings(EDIT + " Who is right?").guidance
    assert not check_meanings(f"{account} Who is right and can you send the edit?").reply

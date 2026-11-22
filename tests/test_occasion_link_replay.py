import pytest

from scripts.occasion_link_replay import _literal_quote, explicit_link


QUESTION = "How many separate times did the delivery cart stall?"
FIRST = "On Thursday the delivery cart stalled at the crossing."


@pytest.mark.parametrize(("later", "quote", "expected"), [
    ("The delivery cart stalled again at the gate.", "stalled again", "distinct"),
    ("The delivery cart didn't stall again.", "stall again", None),
    ("If the delivery cart stalled again, I would replace it.", "stalled again", None),
    ("I replaced a second wheel for Thursday's stall.", "Thursday's stall", "same"),
    ("I ordered a second wheel for the delivery cart.", "a second wheel", None),
    ("I parked the delivery cart. The forklift stalled again.", "stalled again", None),
    ("I fixed that same stall in the yard.", "in the yard", None),
    ("I don't know if it was the same stall.", "the same stall", None),
    ("The cart stalled again after Thursday's stall.", "Thursday's stall", None),
])
def test_proposed_quote_must_support_the_relation_in_its_source(later, quote, expected):
    assert explicit_link(QUESTION, FIRST, later, FIRST, _literal_quote(quote, later)) == expected


def test_citation_preserves_the_source_words():
    assert _literal_quote('"the delivery cart stalled"', FIRST) == "the delivery cart stalled"
    assert _literal_quote("the delivery cart stalled twice", FIRST) == ""
    assert _literal_quote(" ", FIRST) == ""

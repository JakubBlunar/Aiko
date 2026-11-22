import json

from scripts.mined_choice_replay import BRANCHES, OCCASIONS, compare


def test_mines_before_holdouts_and_compares_only_cited_contexts():
    pairs = iter(((1, 2), (3, 4), (10, 11)))
    calls = []

    def infer(turns, structured):
        calls.append((turns, structured))
        if structured:
            ids = next(pairs)
            return json.dumps({"concepts": [{"label": f"generated pattern {ids[0]}",
                                             "evidence_concept_ids": ids}]}), 42, "stop"
        return "actual reply", 55, "stop"

    result = compare(infer)
    assert result["status"] == "completed"
    assert all(structured and not any(q in str(turns) for q in OCCASIONS)
               for turns, structured in calls[:3])
    assert result["contexts"]["parent"] == "generated pattern 10"
    assert result["contexts"]["children"] == "generated pattern 1\ngenerated pattern 3"
    assert len(result["trials"]) == len(OCCASIONS) * len(BRANCHES) * 2
    half = len(result["trials"]) // 2
    assert [turns[1:] for turns, _ in calls[3:3 + half]] == [
        turns[1:] for turns, _ in calls[3 + half:]]


def test_mining_can_abstain_without_fabricating_an_arm():
    result = compare(lambda *_: ('{"concepts":[]}', 1, "stop"))
    assert result["status"] == "no child" and "trials" not in result

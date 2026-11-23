"""Independent choices support a pursuit; the concept cannot corroborate itself."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.concepts.proposers.base import ExistingConcept, ProposerContext
from app.core.concepts.proposers.pursuit_aiko import propose_pursuit_aiko
from app.core.concepts.pursuit_choices import SKETCH_PURSUIT
from app.core.conversation.pursuit_reply import pursuit_reply
from app.core.world import in_progress_beat
from scripts.pursuit_chat_replay import CASES, INTEREST, NOW, REASON, WHY, history


@pytest.mark.parametrize("case", CASES)
def test_pursuit_history_uses_promoted_concept_and_actual_sources(tmp_path, case):
    with history(tmp_path, case) as (db, host):
        provider = host._choice_history_payload
        facts = provider("aiko")["choice_history"]
        assert facts["finding"]["kind"] == CASES[case]
        assert provider("user") == {}
        concepts = host._concept_store.list_by(status="active", kind="pursuit")
        if case == "assigned":
            assert concepts == [] and host._concept_store.count() == 0
            assert "pursuit" not in facts
        else:
            concept, = concepts
            assert concept.label == SKETCH_PURSUIT
            assert facts["pursuit"]["concept_id"] == concept.concept_id
            assert len(set(facts["pursuit"]["memory_ids"])) == 6
            if case == "voluntary":
                assert len(host._memory_store.iter_by_kinds(("pursuit_note",))) == 7
                assert len(host._concept_store.evidence_of(concept.concept_id)) == 6

        turns = [SimpleNamespace(role="user", content=INTEREST),
                 SimpleNamespace(role="assistant", content="I've been sketching.")]
        if case == "changed":
            assert facts["expressed_reason"]["statement"] == REASON
            assert "I enjoy getting a face right." in pursuit_reply(WHY, turns, provider)
            assert "book" in pursuit_reply(INTEREST, [], provider)
            reason_id = facts["expressed_reason"]["message_id"]
            db.update_message_content(reason_id, "The sketch is on the desk.")
        assert "haven't pinned down" in pursuit_reply(WHY, turns, provider)
        host._settings.tools.enabled = False
        assert pursuit_reply(INTEREST, [], host._pursuit_reply_history) == ""


def test_cued_or_repeated_episode_does_not_reinforce(tmp_path):
    with history(tmp_path, "voluntary") as (_, host):
        note = host._memory_store.iter_by_kinds(("pursuit_note",))[0]
        known = [ExistingConcept(1, SKETCH_PURSUIT)]
        ctx = ProposerContext(call_llm=lambda *_: pytest.fail("Unexpected synthesis model call"))
        legacy = replace(note, metadata={k: v for k, v in note.metadata.items()
                                         if k != "sketch_choice"})
        assert propose_pursuit_aiko(ctx, memories=[legacy], existing=known) == []
        duplicates = [replace(note, id=i) for i in range(90, 93)]
        assert propose_pursuit_aiko(ctx, memories=duplicates) == []
        proposal, = propose_pursuit_aiko(ctx, memories=[note, legacy], existing=known)
        assert proposal.reinforces_id == 1 and proposal.evidence == [("memory", str(note.id))]

        saved = {}
        beat = in_progress_beat.build(key="home_new_sketch", activity="drawing", posture="sitting",
                                      summary=note.content, now=NOW,
                                      choice_receipt=note.metadata["sketch_choice"])
        assert in_progress_beat.save(saved.__setitem__, beat)
        assert in_progress_beat.load(saved.get).choice_receipt == beat.choice_receipt


def test_unrelated_question_and_ambiguous_followup_need_no_history():
    def unexpected(*_):
        pytest.fail("Read pursuit history for an unrelated turn")
    assert not pursuit_reply("What am I into lately?", [], unexpected)
    assert not pursuit_reply(WHY, [], unexpected)
    mixed = [SimpleNamespace(role="user", content=INTEREST),
             SimpleNamespace(role="assistant", content="Sketching and reading.")]
    assert not pursuit_reply(WHY, mixed, unexpected)
    assert not pursuit_reply(WHY, [SimpleNamespace(role="user", content="I like sketching."),
                                   SimpleNamespace(role="assistant", content="I do too.")],
                             unexpected)

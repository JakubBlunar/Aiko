"""Independent choices support a pursuit; the concept cannot corroborate itself."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.concepts.concept_consolidation_worker import ConceptConsolidationWorker
from app.core.concepts.concept_source_policy import ConceptSourcePolicyMixin
from app.core.concepts.proposers.base import ExistingConcept, ProposerContext
from app.core.concepts.proposers.pursuit_aiko import propose_pursuit_aiko
from app.core.conversation.pursuit_reply import pursuit_reply
from app.core.world import in_progress_beat
from app.core.world.pursuit_activities import pursuit_label
from scripts.pursuit_chat_replay import CASES, EXAMPLES, INTEREST, NOW, REASON, WHY, history


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
            assert concept.label == pursuit_label("sketching")
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
            assert "reading" in pursuit_reply(INTEREST, [], provider)
            reason_id = facts["expressed_reason"]["message_id"]
            db.update_message_content(reason_id, "The sketch is on the desk.")
        assert "haven't pinned down" in pursuit_reply(WHY, turns, provider)
        host._settings.tools.enabled = False
        assert pursuit_reply(INTEREST, [], host._pursuit_reply_history) == ""


def test_cued_or_repeated_episode_does_not_reinforce(tmp_path):
    with history(tmp_path, "voluntary") as (_, host):
        note = host._memory_store.iter_by_kinds(("pursuit_note",))[0]
        known = [ExistingConcept(1, pursuit_label("sketching"))]
        ctx = ProposerContext(call_llm=lambda *_: pytest.fail("Unexpected synthesis model call"))
        legacy = replace(note, metadata={k: v for k, v in note.metadata.items()
                                         if k != "activity_choice"})
        assert propose_pursuit_aiko(ctx, memories=[legacy], existing=known) == []
        duplicates = [replace(note, id=i) for i in range(90, 93)]
        assert propose_pursuit_aiko(ctx, memories=duplicates) == []
        proposal, = propose_pursuit_aiko(ctx, memories=[note, legacy], existing=known)
        assert proposal.reinforces_id == 1 and proposal.evidence == [("memory", str(note.id))]

        saved = {}
        beat = in_progress_beat.build(key="home_new_sketch", activity="drawing", posture="sitting",
                                      summary=note.content, now=NOW,
                                      choice_receipt=note.metadata["activity_choice"])
        assert in_progress_beat.save(saved.__setitem__, beat)
        assert in_progress_beat.load(saved.get).choice_receipt == beat.choice_receipt


@pytest.mark.parametrize(("key", "name"), [
    ("read_book", "reading"), ("garden", "tending plants"), ("tea", "making tea"),
    ("stretching", "stretching"), ("home_revise_sketch", "sketching"),
])
def test_voluntary_activities_share_the_same_concept_lifecycle(tmp_path, key, name):
    with history(tmp_path, "voluntary", key) as (_, host):
        facts = host._choice_history_payload("aiko")["choice_history"]
        assert facts["activity"] == name
        assert facts["pursuit"]["statement"] == pursuit_label(name)
        assert len(facts["pursuit"]["memory_ids"]) == 6
        assert pursuit_label(name) in pursuit_reply(f"Are you still {name}?", [],
                                                    host._pursuit_reply_history)


def test_two_pursuits_keep_concepts_evidence_and_reasons_separate(tmp_path):
    with history(tmp_path, "changed", "writing", paired=True) as (db, host):
        provider = host._pursuit_reply_history
        facts = provider("aiko")["choice_history"]
        writing, reading = (facts["activities"][name] for name in ("writing", "reading"))
        assert writing["finding"]["previous_activity"] == "reading"
        assert reading["finding"]["kind"] == "repeated_choice"
        assert writing["pursuit"]["concept_id"] != reading["pursuit"]["concept_id"]
        consolidation = ConceptConsolidationWorker(
            concept_store=host._concept_store, memory_settings=SimpleNamespace(),
            agent_settings=host._settings.agent, ollama=None, rate_limiter=None,
        )
        assert consolidation._collect_pairs(0.85, {}) == []
        assert not host._concept_store.merge_into(canonical_id=writing["pursuit"]["concept_id"],
                                                  absorbed_id=reading["pursuit"]["concept_id"])
        assert set(writing["pursuit"]["memory_ids"]).isdisjoint(reading["pursuit"]["memory_ids"])
        for key, topic in (("writing", "writing"), ("read_book", "reading books")):
            assert EXAMPLES[key][2] in pursuit_reply(f"Why do you enjoy {topic}?", [], provider)
        assert "enough to say" in pursuit_reply("Are you still drawing?", [], provider)
        db.update_message_content(reading["expressed_reason"]["message_id"],
                                  "I don’t enjoy reading as much these days.")
        assert "haven't pinned down" in pursuit_reply("Why do you like reading?", [], provider)
        assert EXAMPLES["writing"][2] in pursuit_reply("Why do you like writing?", [], provider)

        notes = host._memory_store.iter_by_kinds(("pursuit_note",))
        ctx = ProposerContext(call_llm=lambda *_: [])
        proposals = {p.label: p for p in propose_pursuit_aiko(ctx, memories=notes)}
        proposal = proposals[pursuit_label("writing")]
        read_id = reading["pursuit"]["memory_ids"][0]
        policy = ConceptSourcePolicyMixin()
        policy._concept_store, policy._memory_store = host._concept_store, host._memory_store
        mixed = replace(proposal, evidence=[*proposal.evidence, ("memory", str(read_id))])
        wrong_target = replace(proposal, reinforces_id=reading["pursuit"]["concept_id"])
        assert policy._claim_source_refusal(mixed) == "missing_independent_activity_choice"
        assert policy._claim_source_refusal(wrong_target) == "mismatched_pursuit_activity"


def test_unrelated_question_and_ambiguous_followup_need_no_history():
    def unexpected(*_):
        pytest.fail("Read pursuit history for an unrelated turn")
    assert not pursuit_reply("What am I into lately?", [], unexpected)
    assert not pursuit_reply(WHY, [], unexpected)
    mixed = [SimpleNamespace(role="user", content=INTEREST),
             SimpleNamespace(role="assistant", content="Sketching and reading.")]
    assert not pursuit_reply(WHY, mixed, unexpected)
    mixed[-1].content = "Sketching and painting."
    assert not pursuit_reply(WHY, mixed, unexpected)
    assert not pursuit_reply(WHY, [SimpleNamespace(role="user", content="I like sketching."),
                                   SimpleNamespace(role="assistant", content="I do too.")],
                             unexpected)

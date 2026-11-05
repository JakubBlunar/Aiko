import numpy as np

from app.core.concepts.proposers.base import CandidateProposal, ProposerContext, ProposerSpec
from app.core.memory.memory_store import MemoryStore
from tests.test_concept_synthesis_worker import WorkerHarness


def test_non_style_concepts_retain_validated_applicability_and_distinct_scopes():
    output = {"label": "Noah prefers brief replies when tired", "rationale": "A stated context",
              "applicability": {"contexts": [{"name": "tired", "memory_id": 1,
                                               "quote": "when tired"}], "exceptions": []}}
    harness = WorkerHarness(lambda *_: {"concepts": [output]}, clusters=[], self_memories=[])
    memories = MemoryStore(harness.path)
    try:
        rows = [memories.add(text, "preference", np.ones(3), skip_dedupe=True)
                for text in ("Noah prefers brief replies when tired",
                             "Short replies suit Noah when tired")]
        harness.worker._memory_store = memories
        harness.worker._routing_begin({})

        def propose(ctx, memories):
            ctx.call_llm("Interpret the sources", "Use their stated contexts")
            return [CandidateProposal(output["label"], output["rationale"], .6,
                                      [("memory", str(row.id)) for row in memories],
                                      "identity", "user")]

        spec = ProposerSpec("identity", "user", "set", "clusters", propose)
        proposals = harness.worker._capture_proposer(spec).propose(
            ProposerContext(harness.worker._call_llm), memories=rows,
        )
        assert proposals[0].applicability["contexts"][0]["name"] == "tired"
        stats = {"added": 0, "reinforced": 0, "by_subject": {}}
        harness.worker._persist(proposals[0], stats)
        concept = harness.store.list_by(kind="identity")[0]
        assert harness.store.applicability.get(concept.concept_id)["contexts"][0]["name"] == "tired"
        other = CandidateProposal(
            "Noah prefers detailed replies during debugging", "A different context",
            .6, [("memory", str(row.id)) for row in rows], "identity", "user",
            applicability={"version": 1, "contexts": [{"name": "troubleshooting"}],
                           "exceptions": []},
        )
        match, _ = harness.worker._find_duplicate(other, concept.embedding)
        assert match is None
        before = concept.last_reinforced_at
        harness.worker._reinforce(concept, other, stats)
        assert concept.last_reinforced_at == before

        output["applicability"]["contexts"][0]["memory_id"] = 999
        assert harness.worker._capture_proposer(spec).propose(
            ProposerContext(harness.worker._call_llm), memories=rows,
        ) == []
        assert harness.worker._routing_stats["unsupported_applicability"] == 1

        output["applicability"]["contexts"][0]["memory_id"] = rows[0].id

        def changing_source(*_):
            memories.update(rows[0].id, content="A corrected source")
            return [output]

        assert harness.worker._capture_proposer(spec).propose(
            ProposerContext(changing_source), memories=rows,
        ) == []
        assert harness.worker._routing_stats["longitudinal_source_changed"] == 1

        assert harness.worker._claim_source_refusal(proposals[0]) == "changed_applicability_sources"
        from app.core.concepts.concept_applicability import parse_applicability

        self_row = memories.add("I prefer brief replies when tired", "self", np.ones(3))
        proposals[0].applicability = parse_applicability({"contexts": [
            {"name": "tired", "memory_id": self_row.id, "quote": "when tired"},
        ]}, {self_row.id: self_row})
        assert harness.worker._claim_source_refusal(proposals[0]) == "different_subject"
    finally:
        memories.close()


def test_conditional_identity_rendering_preserves_scope_and_unknown_context():
    from tests.test_concept_surfacing import _RenderHarness, _concept

    host = _RenderHarness()
    concept = _concept(7, "Brief replies suit Noah when tired", kind="identity")
    scope = {"version": 1, "contexts": [{"name": "tired"}], "exceptions": []}
    unknown, _ = host._render_relevant_concepts(
        [concept], style_scopes={7: scope}, style_states={7: "unknown_context"},
    )
    assert "A possible reading, not a current rule" in unknown
    assert "tired" in unknown
    matched, _ = host._render_relevant_concepts(
        [concept], style_scopes={7: scope}, style_states={7: "matched"},
    )
    assert "reading fits the current context" in matched
    for state in ("exception", "other_context", "source_changed"):
        text, trace = host._render_relevant_concepts(
            [concept], style_scopes={7: scope}, style_states={7: state},
        )
        assert concept.label not in text
        assert not trace["surfaced"]

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.concepts.concept_evidence_lineage import (
    SourceLineage, resolve_support, support_summary,
)
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.concepts.concept_view import ConceptView
from app.core.memory.memory_admission import admit_memory


CORPUS = json.loads((Path(__file__).parent / "fixtures" / "reasoning_l55.json").read_text())


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_l55_evidence_contract(case):
    sources = [
        SourceLineage(
            roots=frozenset(source["roots"]), category=source["category"],
            complete=source.get("complete", True),
        )
        for source in case["sources"]
        for _ in range(source.get("repeat", 1))
    ]
    result = support_summary(sources)
    assert result["mode"] == "shadow"
    for key, expected in case["expected"].items():
        assert result[key] == expected
    assert support_summary(reversed(sources)) == result


def _view(memories, edges, *, clusters=()):
    concepts = {concept_id: Concept(label="Synthetic boundary", kind="boundary", confidence=0.9)
                for concept_id in range(1, 5)}
    store = SimpleNamespace(
        get=concepts.get,
        evidence_of=lambda concept_id: [edge for edge in edges if int(edge.dst_id) == concept_id],
    )
    return ConceptView(
        store, memory_store=SimpleNamespace(get=memories.get),
        topic_graph=SimpleNamespace(
            cluster_id_for=lambda memory_id: next(
                (index for index, cluster in enumerate(clusters)
                 if memory_id in cluster.member_ids),
                None,
            ),
            cluster_member_ids=lambda cluster_id: clusters[cluster_id].member_ids,
        ),
    ), concepts


def _edge(source_id, destination=1, source_type="memory", polarity=1):
    return ConceptEdge(source_type, str(source_id), "concept", str(destination),
                       "evidence", polarity=polarity)


def _memory(message_id=None, *, metadata=None, kind="fact", provenance="inferred"):
    return SimpleNamespace(source_message_id=message_id, metadata=metadata or {},
                           kind=kind, provenance=provenance)


def test_repeated_extraction_uses_server_validated_window():
    rows = [SimpleNamespace(id=message_id, session_id="synthetic", role="user", content=text)
            for message_id, text in [(1, "Keep replies brief."), (2, "Use short answers.")]]
    memories = {}
    for memory_id in range(10):
        row = rows[memory_id % 2]
        admission = admit_memory(
            {"content": row.content, "evidence": [{"message_id": row.id, "quote": row.content}]},
            rows, session_key="synthetic", writer="extractor",
        )
        memories[memory_id] = _memory(
            admission.source_message_id, metadata=admission.metadata,
            provenance=admission.provenance,
        )
    view, concepts = _view(memories, [_edge(memory_id) for memory_id in memories])
    before = repr(concepts)
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 1
    assert report["direct_source_count"] == 10
    assert report["complete"]
    assert repr(concepts) == before


def test_meta_concepts_and_summary_share_roots_without_new_votes():
    memories = {1: _memory(10), 2: _memory(10), 3: _memory(20),
                4: _memory(kind="topic_digest", metadata={"source_ids": [1, 2, 3]})}
    edges = [_edge(1, 2), _edge(2, 3), _edge(2, source_type="concept"),
             _edge(3, source_type="concept"), _edge(4)]
    view, _ = _view(memories, edges)
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 2
    assert report["root_count"] == 2
    assert report["complete"]


def test_cycle_cannot_self_certify_and_keeps_other_support():
    view, _ = _view({1: _memory(10)}, [_edge(2, source_type="concept"),
                                     _edge(1, 2, "concept"), _edge(1)])
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 1
    assert report["issues"] == {"cycle": 1}
    assert not report["complete"]


def test_cluster_alias_and_digest_do_not_double_support():
    cluster = SimpleNamespace(representative_id=2, member_ids=[1, 2])
    view, _ = _view({1: _memory(10), 2: _memory(10)},
                    [_edge(1, source_type="cluster"), _edge(1)], clusters=[cluster])
    assert view.evidence_independence(1)["known_support_groups"] == 1


def test_unknown_deleted_and_opposing_evidence_are_not_support():
    view, _ = _view({1: _memory(), 2: _memory(20)},
                    [_edge(1), _edge(2, polarity=-1), _edge(3)])
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 0
    assert report["unknown_sources"] == 2
    assert report["issues"] == {"missing_memory": 1}


def test_assistant_statement_is_reflection_not_testimony():
    admission = admit_memory(
        {"kind": "self", "content": "I prefer quiet."},
        [SimpleNamespace(id=4, session_id="synthetic", role="assistant",
                         content="I prefer quiet.")],
        session_key="synthetic", writer="inline",
    )
    view, _ = _view({1: _memory(4, metadata=admission.metadata, kind="self")}, [_edge(1)])
    assert view.evidence_independence(1)["known_support_groups"] == 0
    assert view.evidence_independence(1)["excluded_reflections"] == 1


def test_bounded_and_disabled_resolution():
    view, _ = _view({1: _memory(10)}, [_edge(1)])
    report = view.evidence_independence(1, max_nodes=1)
    assert report["known_support_groups"] == 0
    assert report["issues"] == {"node_limit": 1}
    assert not report["complete"]
    assert ConceptView(None).evidence_independence(1)["issues"] == {"missing_concept": 1}


def test_external_sources_are_citations_not_new_votes_per_summary():
    memories = {
        1: _memory(kind="knowledge", metadata={"source_url": "https://example.org/fact"}),
        2: _memory(kind="knowledge", metadata={"source_urls": ["https://example.org/fact#part"]}),
        3: _memory(kind="knowledge", metadata={"source_url": "file:///not-an-observation"}),
    }
    view, _ = _view(memories, [_edge(memory_id) for memory_id in memories])
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 1
    assert report["unknown_sources"] == 1
    assert report["by_category"] == {"external_observation": 3}


def test_empty_citations_do_not_turn_an_input_window_into_support():
    admission = admit_memory(
        {"content": "A speculative preference."},
        [SimpleNamespace(id=1, session_id="synthetic", role="user", content="Hello.")],
        session_key="synthetic", writer="extractor",
    )
    view, _ = _view({1: _memory(1, metadata=admission.metadata)}, [_edge(1)])
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 0
    assert report["unknown_sources"] == 1


def test_explicit_boundary_admission_is_unchanged_after_one_statement():
    content = "Do not use nicknames."
    admission = admit_memory(
        {"content": content},
        [SimpleNamespace(id=1, session_id="synthetic", role="user", content=content)],
        session_key="synthetic", writer="inline",
    )
    assert admission.accepted
    assert admission.provenance == "stated"
    assert admission.tier == "long_term"
    view, concepts = _view({1: _memory(1, metadata=admission.metadata,
                                      provenance=admission.provenance)}, [_edge(1)])
    before = repr(concepts)
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 1
    assert report["by_category"] == {"testimony": 1}
    assert repr(concepts) == before


def test_public_facade_has_no_learning_store_dependency():
    from app.core.session.memory_facade_mixin import MemoryFacadeMixin

    report = MemoryFacadeMixin().concept_evidence_independence(1)
    assert report["mode"] == "shadow"
    assert report["issues"] == {"missing_concept": 1}


def test_depth_limit_is_unknown_not_fabricated_support():
    store = SimpleNamespace(get=lambda concept_id: Concept(label="Synthetic abstraction"),
                            evidence_of=lambda concept_id: [_edge(concept_id + 1,
                                                                  concept_id, "concept")])
    report = resolve_support(store, None, None, 1, max_depth=2)
    assert report["visited_nodes"] == 3
    assert report["issues"] == {"depth_limit": 1}
    assert report["known_support_groups"] == 0


def test_malformed_import_remains_unknown():
    view, _ = _view({1: _memory(metadata=["invalid"])}, [_edge(1)])
    report = view.evidence_independence(1)
    assert report["known_support_groups"] == 0
    assert report["issues"] == {"invalid_metadata": 1}
